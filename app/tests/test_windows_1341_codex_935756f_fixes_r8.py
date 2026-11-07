"""Fix for the Codex read-only review of 935756f (FIX REQUIRED, one Low finding), owner-approved 2026-11-07.

Before: load_users() migrates legacy account records (for example a local-admin record with no email, or with an empty
password hash while ANYAICAM_ADMIN_PASSWORD is set) and saves users.json whenever the migration changed something. An
administrator's password reset loads users.json several times before it reaches the damaged-counter check (the
request's authentication, the target lookup, the re-check under the lock), so a reset refused for a damaged session
counter could still rewrite users.json.

Now every users.json load in a password-reset request is read-only: the migration is applied in memory and never saved
there. A refused reset leaves users.json byte-identical; a reset that goes ahead saves the migrated list with its own
change. Every other request still saves the migration as before.
"""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r8-password-1"
NEW_PASSWORDS = ["r8-new-password-1", "r8-new-password-2"]
LOCAL_ADMIN_EMAIL = "local-admin@example.test"
LOCAL_ADMIN_PASSWORD = "r8-local-admin-environment-password"
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": [], "password_hash": "",
         "failed_login_attempts": 0, "locked_until": None, "invitation_status": "active"}
DAMAGED = [None, "1", -1]
DAMAGED_IDS = ["null", "text", "negative"]
TRIGGERS = ["missing-email", "empty-hash"]
LARGE = [2 ** 53 + 1, 2 ** 63 - 1, 2 ** 64, 10 ** 30]
LARGE_IDS = ["2**53+1", "2**63-1", "2**64", "10**30"]


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
    monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", LOCAL_ADMIN_EMAIL)
    monkeypatch.setenv("ANYAICAM_ADMIN_PASSWORD", LOCAL_ADMIN_PASSWORD)
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    with override_target(sqlite_path=tmp_path / "codex_r8.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _fresh():
    return TestClient(main.app, raise_server_exceptions=False)


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"{user_id}@example.test", "role": "viewer",
                 "enabled": True, "super_admin": False, "site_ids": ["home"], "camera_ids": [],
                 "password_hash": main.hash_password(PASSWORD), "failed_login_attempts": 0, "locked_until": None,
                 "invitation_status": "active"}, **extra)


def _legacy_local_admin(trigger, **extra):
    """A local-admin record that load_users() migrates and saves: no email, or an empty password hash while
    ANYAICAM_ADMIN_PASSWORD is set."""
    record = _user("local-admin", display_name="Local Admin", role="administrator", super_admin=True, **extra)
    if trigger == "missing-email":
        record.pop("email")
    else:
        record["email"] = LOCAL_ADMIN_EMAIL
        record["password_hash"] = ""
    return record


def _write_users_file(users):
    # Written directly, not through save_users() or load_users(), so the legacy record is still unmigrated on disk.
    main.USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def _file_account(user_id):
    return next(user for user in json.loads(main.USERS_FILE.read_text(encoding="utf-8")) if user["id"] == user_id)


def _admin_cookies():
    # An explicit snapshot: create_session() does not read users.json, so nothing migrates before the reset.
    return {main.SESSION_COOKIE_NAME: main.create_session("admin-1", session_generation=0)}


def _signed_in_as(token):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [
        (b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())]})
    user = main.authenticated_user(request)
    return user["id"] if user else None


def _sign_in(email, password=PASSWORD):
    return _fresh().post("/login", data={"email": email, "password": password, "next_url": "/"}, follow_redirects=False)


def _portal_sign_in(email, password=PASSWORD):
    return _fresh().post("/api/portal-login", json={"email": email, "password": password, "portal": "administrator"},
                         follow_redirects=False)


def _reset(client, user_id, password, cookies):
    return client.put(f"/api/users/{user_id}/password", cookies=cookies, json={"password": password}).json()


def _audit_bytes():
    return main.AUDIT_LOG_FILE.read_bytes() if main.AUDIT_LOG_FILE.exists() else b""


def _new_audit_entries(before):
    after = _audit_bytes()
    assert after.startswith(before)  # every earlier entry is kept exactly as it was
    return [json.loads(line) for line in after[len(before):].decode("utf-8").splitlines()]


def _spy_on_writes(monkeypatch):
    saves, sign_outs = [], []
    real_save, real_sign_out = main.save_users, main._sign_out_user_sessions
    monkeypatch.setattr(main, "save_users", lambda users: saves.append(len(users)) or real_save(users))
    monkeypatch.setattr(main, "_sign_out_user_sessions",
                        lambda *args, **kwargs: sign_outs.append(args) or real_sign_out(*args, **kwargs))
    return saves, sign_outs


# ---------------------------------------------------------------- the legacy record really migrates outside a reset

@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_legacy_local_admin_record_is_migrated_and_saved_by_an_ordinary_load(client, trigger):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    before = main.USERS_FILE.read_bytes()
    main.load_users()
    assert main.USERS_FILE.read_bytes() != before
    migrated = _file_account("local-admin")
    assert migrated["email"] == LOCAL_ADMIN_EMAIL and migrated["password_hash"]


def test_an_ordinary_authenticated_request_still_saves_the_migration(client):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email")])
    response = client.get("/api/users", cookies=_admin_cookies())
    assert response.status_code == 200
    assert _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL


# ---------------------------------------------------------------- a refused reset never migrates users.json

def _assert_refused_without_any_write(client, monkeypatch, target, users_before_reset):
    admin = _admin_cookies()
    users_before = main.USERS_FILE.read_bytes()
    assert users_before == users_before_reset
    sessions_before = main.SESSIONS_FILE.read_bytes()
    audit_before = _audit_bytes()
    saves, sign_outs = _spy_on_writes(monkeypatch)

    result = _reset(client, target, NEW_PASSWORDS[0], admin)

    assert result["status"] == "error" and "not changed" in result["message"]
    assert "authorized support" in result["message"]
    assert main.USERS_FILE.read_bytes() == users_before  # no migration, no password, no counter, no lockout change
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert saves == [] and sign_outs == []
    entries = _new_audit_entries(audit_before)
    assert [(entry["action"], entry["resource"], entry["outcome"], entry["user_id"]) for entry in entries] == [
        ("update_password", f"user:{target}", "failed", "admin-1")]
    assert NEW_PASSWORDS[0] not in json.dumps(entries)
    return admin


@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_refused_reset_of_a_damaged_legacy_local_admin_leaves_users_json_unchanged(client, monkeypatch, trigger,
                                                                                    damaged):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger, session_generation=damaged)])
    users_on_disk = main.USERS_FILE.read_bytes()
    admin = _assert_refused_without_any_write(client, monkeypatch, "local-admin", users_on_disk)
    on_disk = _file_account("local-admin")
    assert on_disk["session_generation"] == damaged
    assert ("email" not in on_disk) if trigger == "missing-email" else (on_disk["password_hash"] == "")
    assert _signed_in_as(admin[main.SESSION_COOKIE_NAME]) == "admin-1"


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_refused_reset_of_another_damaged_account_does_not_migrate_the_legacy_local_admin(client, monkeypatch,
                                                                                           trigger):
    # Codex 935756f scenario: a separately authenticated administrator resets a damaged account while users.json
    # holds a legacy local-admin record.
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger), _user("viewer-1", session_generation=None)])
    users_on_disk = main.USERS_FILE.read_bytes()
    _assert_refused_without_any_write(client, monkeypatch, "viewer-1", users_on_disk)
    main.load_users()  # the read-only load ended with the reset request: an ordinary load migrates again
    assert _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL


# ---------------------------------------------------------------- a reset that goes ahead still saves the migration

def test_a_valid_reset_saves_the_legacy_migration_with_its_own_change(client):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email"), _user("viewer-1")])
    earlier = main.create_session("viewer-1", session_generation=0)
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _admin_cookies()) == {
        "status": "complete", "message": "Password updated."}
    assert _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL
    viewer = _file_account("viewer-1")
    assert viewer["session_generation"] == 1 and main.verify_password(NEW_PASSWORDS[0], viewer["password_hash"])
    assert _signed_in_as(earlier) is None


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_valid_reset_of_the_legacy_local_admin_migrates_it_and_revokes_its_sessions(client, trigger):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    earlier = main.create_session("local-admin", session_generation=0)
    assert _reset(client, "local-admin", NEW_PASSWORDS[0], _admin_cookies())["status"] == "complete"
    record = _file_account("local-admin")
    assert record["email"] == LOCAL_ADMIN_EMAIL and record["session_generation"] == 1
    assert main.verify_password(NEW_PASSWORDS[0], record["password_hash"])
    assert _signed_in_as(earlier) is None
    signed_in = _sign_in(LOCAL_ADMIN_EMAIL, NEW_PASSWORDS[0])
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "local-admin"


# ---------------------------------------------------------------- very large counters

@pytest.mark.parametrize("large", LARGE, ids=LARGE_IDS)
def test_a_very_large_valid_counter_advances_by_one_and_revokes_earlier_sessions(client, large):
    _write_users_file([dict(ADMIN), _user("viewer-1", session_generation=large), _user("viewer-2")])
    earlier = _sign_in("viewer-1@example.test").cookies[main.SESSION_COOKIE_NAME]
    other = main.create_session("viewer-2", session_generation=0)
    assert _signed_in_as(earlier) == "viewer-1"
    kept_copy = main.SESSIONS_FILE.read_bytes()
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _admin_cookies())["status"] == "complete"
    assert _file_account("viewer-1")["session_generation"] == large + 1  # exact through JSON, no float rounding
    assert _signed_in_as(earlier) is None
    main.SESSIONS_FILE.write_bytes(kept_copy)  # an older sessions.json put back
    assert _signed_in_as(earlier) is None and _signed_in_as(other) == "viewer-2"
    signed_in = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0])
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"


# ---------------------------------------------------------------- the users.json branch of the portal sign-in

def test_a_portal_sign_in_mid_check_when_a_valid_reset_lands_gets_no_working_session(client, monkeypatch):
    _write_users_file([dict(ADMIN), _user("admin-2", role="administrator", super_admin=True, session_generation=3)])
    admin = _admin_cookies()
    real_verify = main.verify_password
    resets = []

    def verify_then_reset(password, password_hash):
        matched = real_verify(password, password_hash)
        if not resets:
            resets.append(_reset(client, "admin-2", NEW_PASSWORDS[0], admin))
        return matched
    monkeypatch.setattr(main, "verify_password", verify_then_reset)
    late = _portal_sign_in("admin-2@example.test")
    assert resets == [{"status": "complete", "message": "Password updated."}]
    assert late.status_code == 303 and _signed_in_as(late.cookies[main.SESSION_COOKIE_NAME]) is None
    monkeypatch.setattr(main, "verify_password", real_verify)
    assert _file_account("admin-2")["session_generation"] == 4
    fresh = _portal_sign_in("admin-2@example.test", NEW_PASSWORDS[0])
    assert fresh.status_code == 303 and _signed_in_as(fresh.cookies[main.SESSION_COOKIE_NAME]) == "admin-2"


def test_a_portal_sign_in_on_a_damaged_counter_gets_no_working_session(client):
    _write_users_file([dict(ADMIN), _user("admin-2", role="administrator", super_admin=True, session_generation=None)])
    signed_in = _portal_sign_in("admin-2@example.test")
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) is None
