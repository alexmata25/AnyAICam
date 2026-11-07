"""Fix for the Codex read-only review of 5b628f3 (FIX REQUIRED, one Low finding), owner-approved 2026-11-07.

Before: a password reset on an account whose session counter (users.json session_generation) was damaged moved it to
a random value at or above 2**62. A later damaged-counter reset could draw the same value again, so a session issued
after the first recovery, put back from an older sessions.json or left by a failed clean-up, could work again.

Now the reset fails closed: when the stored counter is not a non-negative integer, the reset is refused before any
account change. The password, the account record, sessions.json and other accounts are untouched; the audit log keeps
every earlier entry and gains one "failed" entry naming the administrator. Every session of the damaged account stays
refused. A valid counter still advances by one and revokes every earlier session.
"""
import inspect
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r7-password-1"
NEW_PASSWORDS = ["r7-new-password-1", "r7-new-password-2", "r7-new-password-3", "r7-new-password-4"]
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": []}
DAMAGED = ["1", -1, True, None, 1.5, [1], {"value": 1}]
DAMAGED_IDS = ["text", "negative", "bool", "null", "float", "list", "object"]


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
    with override_target(sqlite_path=tmp_path / "codex_r7.db"):
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


def _audit_bytes():
    return main.AUDIT_LOG_FILE.read_bytes() if main.AUDIT_LOG_FILE.exists() else b""


def _new_audit_entries(before):
    after = _audit_bytes()
    assert after.startswith(before)  # every earlier entry is kept exactly as it was
    return [json.loads(line) for line in after[len(before):].decode("utf-8").splitlines()]


# ---------------------------------------------------------------- a damaged counter: the reset is refused, nothing changes

def _damaged_account_with_sessions(damaged):
    """viewer-1 has a damaged counter, a failed-attempt count and a lockout time a reset would clear, and sessions
    from generations 0 and 1 in sessions.json; viewer-2 is a normal account with a working session."""
    main.save_users([dict(ADMIN),
                     _user("viewer-1", session_generation=damaged, failed_login_attempts=2,
                           locked_until="2026-01-01T00:00:00"),
                     _user("viewer-2", session_generation=4)])
    earlier = [main.create_session("viewer-1", session_generation=0),
               main.create_session("viewer-1", session_generation=1)]
    other = main.create_session("viewer-2")
    assert _signed_in_as(other) == "viewer-2"
    assert [_signed_in_as(token) for token in earlier] == [None, None]  # a damaged counter refuses every session
    return earlier, other


@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
def test_a_reset_on_a_damaged_counter_is_refused_and_changes_nothing(client, monkeypatch, damaged):
    earlier, other = _damaged_account_with_sessions(damaged)
    admin = _cookies("admin-1")
    users_before, sessions_before, audit_before = (main.USERS_FILE.read_bytes(), main.SESSIONS_FILE.read_bytes(),
                                                   _audit_bytes())
    sign_outs = []
    real_sign_out = main._sign_out_user_sessions
    monkeypatch.setattr(main, "_sign_out_user_sessions",
                        lambda *args, **kwargs: sign_outs.append(args) or real_sign_out(*args, **kwargs))

    result = _reset(client, "viewer-1", NEW_PASSWORDS[0], admin)

    assert result["status"] == "error" and "not changed" in result["message"]
    assert main.USERS_FILE.read_bytes() == users_before  # password, counter, lockout fields and other accounts
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert sign_outs == []
    account = _account("viewer-1")
    assert main.verify_password(PASSWORD, account["password_hash"])
    assert not main.verify_password(NEW_PASSWORDS[0], account["password_hash"])
    entries = _new_audit_entries(audit_before)
    assert [(entry["action"], entry["resource"], entry["outcome"], entry["user_id"]) for entry in entries] == [
        ("update_password", "user:viewer-1", "failed", "admin-1")]
    assert NEW_PASSWORDS[0] not in json.dumps(entries)
    # Every session of the damaged account stays refused; other sessions are untouched.
    assert [_signed_in_as(token) for token in earlier] == [None, None]
    assert _signed_in_as(other) == "viewer-2" and _signed_in_as(admin[main.SESSION_COOKIE_NAME]) == "admin-1"
    assert _sign_in("viewer-1@example.test", NEW_PASSWORDS[0]).status_code == 401
    still_old = _sign_in("viewer-1@example.test", PASSWORD)
    assert still_old.status_code == 303 and _signed_in_as(still_old.cookies[main.SESSION_COOKIE_NAME]) is None


def test_repeated_resets_on_a_damaged_counter_are_each_refused(client):
    earlier, other = _damaged_account_with_sessions(None)
    admin = _cookies("admin-1")
    users_before, sessions_before, audit_before = (main.USERS_FILE.read_bytes(), main.SESSIONS_FILE.read_bytes(),
                                                   _audit_bytes())
    results = [_reset(client, "viewer-1", password, admin)["status"] for password in NEW_PASSWORDS]
    assert results == ["error"] * len(NEW_PASSWORDS)
    assert main.USERS_FILE.read_bytes() == users_before and main.SESSIONS_FILE.read_bytes() == sessions_before
    assert [entry["outcome"] for entry in _new_audit_entries(audit_before)] == ["failed"] * len(NEW_PASSWORDS)
    assert [_signed_in_as(token) for token in earlier] == [None, None] and _signed_in_as(other) == "viewer-2"


def test_a_damaged_counter_on_another_account_does_not_block_a_valid_reset(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("viewer-2", session_generation="3")])
    damaged_record = _account("viewer-2")
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1"))["status"] == "complete"
    assert _account("viewer-1")["session_generation"] == 1
    assert _account("viewer-2") == damaged_record


def test_no_random_replacement_counter_remains():
    assert not hasattr(main, "_SESSION_GENERATION_RECOVERY_FLOOR")
    source = inspect.getsource(main._next_session_generation) + inspect.getsource(main.change_user_password)
    assert "randbelow" not in source and "random" not in source


# ---------------------------------------------------------------- a valid counter: the reset advances it and revokes

@pytest.mark.parametrize("initial", [None, 0, 7], ids=["missing", "zero", "seven"])
def test_a_reset_on_a_valid_counter_advances_it_by_one_and_revokes_earlier_sessions(client, initial):
    extra = {} if initial is None else {"session_generation": initial}
    main.save_users([dict(ADMIN), _user("viewer-1", **extra), _user("viewer-2")])
    earlier = _sign_in("viewer-1@example.test").cookies[main.SESSION_COOKIE_NAME]
    other = main.create_session("viewer-2")
    kept_copy = main.SESSIONS_FILE.read_bytes()
    assert _signed_in_as(earlier) == "viewer-1"
    audit_before = _audit_bytes()
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1")) == {
        "status": "complete", "message": "Password updated."}
    assert _account("viewer-1")["session_generation"] == (initial or 0) + 1
    assert _signed_in_as(earlier) is None
    main.SESSIONS_FILE.write_bytes(kept_copy)  # an older sessions.json put back
    assert _signed_in_as(earlier) is None and _signed_in_as(other) == "viewer-2"
    resets = [entry for entry in _new_audit_entries(audit_before) if entry["action"] == "update_password"]
    assert [(entry["outcome"], entry["user_id"]) for entry in resets] == [("success", "admin-1")]
    signed_in = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0])
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"


def test_repeated_valid_resets_count_up_and_each_revokes_every_earlier_generation(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = _cookies("admin-1")
    password, issued, copies = PASSWORD, [], []
    for generation, new_password in enumerate(NEW_PASSWORDS[:3]):
        token = _sign_in("viewer-1@example.test", password).cookies[main.SESSION_COOKIE_NAME]
        assert _signed_in_as(token) == "viewer-1"
        issued.append(token)
        copies.append(main.SESSIONS_FILE.read_bytes())
        assert _reset(client, "viewer-1", new_password, admin)["status"] == "complete"
        assert _account("viewer-1")["session_generation"] == generation + 1
        password = new_password
    for copy in copies:  # no older sessions.json brings back any earlier generation
        main.SESSIONS_FILE.write_bytes(copy)
        assert [_signed_in_as(token) for token in issued] == [None] * len(issued)


def test_a_sign_in_mid_check_when_a_valid_reset_lands_gets_no_working_session(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("viewer-1", session_generation=5)])
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
    assert late.status_code == 303 and _signed_in_as(late.cookies[main.SESSION_COOKIE_NAME]) is None
    assert _account("viewer-1")["session_generation"] == 6
