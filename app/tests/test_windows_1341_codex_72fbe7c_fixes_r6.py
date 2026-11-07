"""Fixes for the Codex read-only review of 72fbe7c (FIX REQUIRED), owner-approved 2026-11-07.

Finding 1 (Medium), before: create_session() treated an explicit session_generation=None as "no snapshot" and read the
account again. Both sign-in paths pass the counter from the users.json read the password was checked against, so on an
account whose counter is stored as null, a sign-in still checking the old password when an administrator's reset
lands picked up the reset's new counter and kept a working session. Now only an omitted argument reads the account;
any value passed, null included, is stored as it is and refused.

Finding 2 (Medium), before: a reset on an account whose counter was damaged restarted it at 1, which could match an
earlier generation-1 session still in sessions.json (a failed purge) or put back from an older copy. Now a damaged
counter moves to a fresh value that no earlier session carries.
"""
import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r6-password-1"
NEW_PASSWORDS = ["r6-new-password-1", "r6-new-password-2", "r6-new-password-3", "r6-new-password-4"]
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": []}
DAMAGED = ["1", -1, True, None, 1.5]
DAMAGED_IDS = ["text", "negative", "bool", "null", "float"]


@pytest.fixture(autouse=True)
def _isolated_recordings(tmp_path, monkeypatch):
    # Never read or write the shared recordings root (C:\app\recordings).
    from recordings_isolation import isolate_recordings
    return isolate_recordings(monkeypatch, tmp_path)


def test_recordings_derived_paths_are_isolated_for_this_module(_isolated_recordings):
    from recordings_isolation import assert_isolated
    assert_isolated(_isolated_recordings)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name, file_name in (("USERS_FILE", "users.json"), ("SESSIONS_FILE", "sessions.json"),
                            ("USER_INVITES_FILE", "user_invites.json")):
        monkeypatch.setattr(main, name, tmp_path / file_name)
    monkeypatch.setattr(main, "MAX_LOGIN_ATTEMPTS", 1000)
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    with override_target(sqlite_path=tmp_path / "codex_r6.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _fresh():
    return TestClient(main.app, raise_server_exceptions=False)


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"{user_id}@example.test", "role": "viewer",
                 "enabled": True, "site_ids": ["home"], "camera_ids": [], "password_hash": main.hash_password(PASSWORD),
                 "failed_login_attempts": 0, "locked_until": None}, **extra)


def _cookies(user_id):
    return {main.SESSION_COOKIE_NAME: main.create_session(user_id)}


def _signed_in_as(token):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [
        (b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())]})
    user = main.authenticated_user(request)
    return user["id"] if user else None


def _sign_in(email, password=PASSWORD):
    return _fresh().post("/login", data={"email": email, "password": password, "next_url": "/"}, follow_redirects=False)


def _account(user_id):
    return next(user for user in main.load_users() if user["id"] == user_id)


def _reset(client, user_id, password, cookies):
    return client.put(f"/api/users/{user_id}/password", cookies=cookies, json={"password": password}).json()


def _damage(user_id, value):
    users = main.load_users()
    next(user for user in users if user["id"] == user_id)["session_generation"] = value
    main.save_users(users)


# ---------------------------------------------------------------- finding 1 an explicit null snapshot is not re-read

def test_an_explicit_null_snapshot_is_stored_as_it_is_and_refused(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1"))["status"] == "complete"
    assert _account("viewer-1")["session_generation"] == 1  # a valid counter that a re-read would hand out
    token = main.create_session("viewer-1", session_generation=None)
    stored = json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))
    record = stored[main.session_serializer.loads(token)["session_id"]]
    assert "session_generation" in record and record["session_generation"] is None
    assert _signed_in_as(token) is None


def test_a_sign_in_on_a_null_counter_account_mid_check_when_the_reset_lands_gets_no_working_session(client, monkeypatch):
    # Codex 72fbe7c finding 1: the account's counter is null, the sign-in has checked the old password against that
    # read, the reset repairs the counter, then the sign-in creates its session.
    main.save_users([dict(ADMIN), _user("viewer-1", session_generation=None)])
    admin = _cookies("admin-1")
    real_verify = main.verify_password
    resets = []

    def verify_then_reset(password, password_hash):
        matched = real_verify(password, password_hash)
        if not resets:
            resets.append(_reset(client, "viewer-1", NEW_PASSWORDS[0], admin))
        return matched
    monkeypatch.setattr(main, "verify_password", verify_then_reset)
    late = _sign_in("viewer-1@example.test", PASSWORD)
    assert resets == [{"status": "complete", "message": "Password updated."}]
    assert late.status_code == 303
    assert _signed_in_as(late.cookies[main.SESSION_COOKIE_NAME]) is None
    monkeypatch.setattr(main, "verify_password", real_verify)
    fresh = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0])
    assert fresh.status_code == 303 and _signed_in_as(fresh.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"


def test_an_omitted_snapshot_still_takes_the_counter_as_it_is_now(client):
    main.save_users([dict(ADMIN), _user("viewer-1", session_generation=3)])
    assert _signed_in_as(main.create_session("viewer-1")) == "viewer-1"


def test_both_sign_in_paths_pass_their_own_snapshot_counter():
    # Static guard: every production create_session() call passes session_generation=<account>.get(
    # "session_generation", 0) from the account record it checked the password against, never None and never a
    # fresh read. The two callers are POST /login and the users.json branch of POST /api/portal-login.
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    callers = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name == "create_session":
            continue
        for call in ast.walk(function):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "create_session":
                callers.setdefault(function.name, []).append(call)
    assert sorted(callers) == ["_portal_login", "login_submit"]
    for name, calls in callers.items():
        assert len(calls) == 1, name
        keyword = next(item for item in calls[0].keywords if item.arg == "session_generation")
        value = keyword.value
        assert isinstance(value, ast.Call) and value.func.attr == "get", name
        assert [ast.literal_eval(arg) for arg in value.args] == ["session_generation", 0], name
        account = value.func.value.id
        first_arg = calls[0].args[0]
        assert isinstance(first_arg, ast.Subscript) and first_arg.value.id == account, name


# ---------------------------------------------------------------- finding 2 a damaged counter never restarts at a used value

def _issued_generations(client, admin):
    """An account taken through generations 0, 1 and 2. Returns the generation-1 session, another account's session,
    and a sessions.json copy holding both."""
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("viewer-2")])
    other = main.create_session("viewer-2")
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], admin)["status"] == "complete"
    generation_one = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0]).cookies[main.SESSION_COOKIE_NAME]
    assert _signed_in_as(generation_one) == "viewer-1"
    kept_copy = main.SESSIONS_FILE.read_bytes()
    assert _reset(client, "viewer-1", NEW_PASSWORDS[1], admin)["status"] == "complete"
    assert _account("viewer-1")["session_generation"] == 2 and _signed_in_as(generation_one) is None
    return generation_one, other, kept_copy


@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
def test_a_reset_on_a_damaged_counter_never_revives_an_earlier_session_from_a_restored_copy(client, damaged):
    admin = _cookies("admin-1")
    generation_one, other, kept_copy = _issued_generations(client, admin)
    _damage("viewer-1", damaged)
    assert _reset(client, "viewer-1", NEW_PASSWORDS[2], admin)["status"] == "complete"
    recovered = _account("viewer-1")["session_generation"]
    assert main._session_generation_value(recovered) is not None and recovered not in (0, 1, 2)
    main.SESSIONS_FILE.write_bytes(kept_copy)  # an older sessions.json put back, holding the generation-1 session
    assert _signed_in_as(generation_one) is None
    assert _signed_in_as(other) == "viewer-2"  # another account's session is untouched
    # The account works again from the recovered counter, and the next reset still refuses what came before.
    signed_in = _sign_in("viewer-1@example.test", NEW_PASSWORDS[2])
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"
    assert _reset(client, "viewer-1", NEW_PASSWORDS[3], admin)["status"] == "complete"
    assert _account("viewer-1")["session_generation"] == recovered + 1
    assert _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) is None


@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
def test_a_reset_on_a_damaged_counter_never_revives_an_earlier_session_left_by_a_failed_purge(client, monkeypatch, damaged):
    admin = _cookies("admin-1")
    generation_one, other, kept_copy = _issued_generations(client, admin)
    main.SESSIONS_FILE.write_bytes(kept_copy)  # the generation-1 record is in sessions.json when the next reset runs
    _damage("viewer-1", damaged)

    def unreadable():
        raise main.SessionStoreUnreadable("sessions.json could not be read: PermissionError")
    with monkeypatch.context() as during:
        during.setattr(main, "_load_sessions_for_update", unreadable)
        assert _reset(client, "viewer-1", NEW_PASSWORDS[2], admin)["status"] == "complete"
    stored = json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))
    assert stored[main.session_serializer.loads(generation_one)["session_id"]]["session_generation"] == 1  # still there
    assert _signed_in_as(generation_one) is None


def test_a_valid_counter_still_counts_up_by_one(client):
    main.save_users([dict(ADMIN), _user("viewer-1", session_generation=7)])
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1"))["status"] == "complete"
    assert _account("viewer-1")["session_generation"] == 8
