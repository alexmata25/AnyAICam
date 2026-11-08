"""Fix for the Codex read-only review of abf368b (FIX REQUIRED, one Low finding), owner-approved 2026-11-07.

Before: a password-reset request made every users.json load read-only only when its path was exactly
/api/users/<id>/password. A PUT to the same path with trailing slashes passed through authentication, which loads
users.json and could save the legacy-account migration (or the defaults for a missing or unreadable file), before the
router (redirect_slashes) answered 307 to the canonical path, whose reset was then refused after users.json had already
changed.

Now the trailing-slash forms the router redirects to the reset are classified as the reset too. The read-only state
belongs to the reset request only: it never reaches an overlapping or later request, including after an error.
"""
import json
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r9-password-1"
NEW_PASSWORDS = ["r9-new-password-1", "r9-new-password-2"]
LOCAL_ADMIN_EMAIL = "local-admin@example.test"
LOCAL_ADMIN_PASSWORD = "r9-local-admin-environment-password"
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": [], "password_hash": "",
         "failed_login_attempts": 0, "locked_until": None, "invitation_status": "active"}
TRIGGERS = ["missing-email", "empty-hash"]
SLASHES = ["/", "//"]
SLASH_IDS = ["one-slash", "two-slashes"]
DAMAGED = [None, "1"]
DAMAGED_IDS = ["null", "text"]


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
    with override_target(sqlite_path=tmp_path / "codex_r9.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


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


def _admin_token():
    # An explicit snapshot: create_session() does not read users.json, so nothing migrates before the request.
    return main.create_session("admin-1", session_generation=0)


def _browser(token):
    """A client holding the session cookie in its own cookie jar, which is what it sends again after a redirect."""
    return TestClient(main.app, raise_server_exceptions=False, cookies={main.SESSION_COOKIE_NAME: token})


def _signed_in_as(token):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [
        (b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())]})
    user = main.authenticated_user(request)
    return user["id"] if user else None


def _sign_in(email, password=PASSWORD):
    return TestClient(main.app, raise_server_exceptions=False).post(
        "/login", data={"email": email, "password": password, "next_url": "/"}, follow_redirects=False)


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


def _snapshot():
    return (main.USERS_FILE.read_bytes() if main.USERS_FILE.exists() else None,
            main.SESSIONS_FILE.read_bytes() if main.SESSIONS_FILE.exists() else None, _audit_bytes())


# ---------------------------------------------------------------- the trailing-slash forms are the reset as well

@pytest.mark.parametrize("path, expected", [
    ("/api/users/local-admin/password", True),
    ("/api/users/local-admin/password/", True),
    ("/api/users/local-admin/password//", True),
    ("/api/users/local-admin/passwords", False),
    ("/api/users/local-admin/password/x", False),
    ("/api/users//password", False),
    ("/api/users/a/b/password", False),
])
def test_the_reset_path_classification_covers_every_form_the_router_redirects(path, expected):
    # r10: the classification is the router's own decision (main._request_reaches_password_reset), same cases.
    scope = {"type": "http", "method": "PUT", "path": path, "root_path": "", "query_string": b"", "scheme": "http",
             "server": ("testserver", 80), "headers": [(b"host", b"testserver")]}
    assert main._request_reaches_password_reset(scope) is expected


@pytest.mark.parametrize("slash", SLASHES, ids=SLASH_IDS)
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_trailing_slash_request_itself_is_redirected_without_writing_users_json(client, monkeypatch, trigger,
                                                                                   slash):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger, session_generation=None)])
    browser = _browser(_admin_token())
    users_before, sessions_before, audit_before = _snapshot()
    saves, _ = _spy_on_writes(monkeypatch)
    response = browser.put(f"/api/users/local-admin/password{slash}", json={"password": NEW_PASSWORDS[0]},
                           follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/api/users/local-admin/password")
    assert main.USERS_FILE.read_bytes() == users_before  # no migration saved by the redirected request
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert saves == [] and _new_audit_entries(audit_before) == []


@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
@pytest.mark.parametrize("slash", SLASHES, ids=SLASH_IDS)
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_full_redirect_and_refusal_sequence_leaves_everything_unchanged(client, monkeypatch, trigger, slash,
                                                                          damaged):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger, session_generation=damaged)])
    earlier = main.create_session("local-admin", session_generation=0)
    token = _admin_token()
    users_before, sessions_before, audit_before = _snapshot()
    saves, sign_outs = _spy_on_writes(monkeypatch)
    response = _browser(token).put(f"/api/users/local-admin/password{slash}", json={"password": NEW_PASSWORDS[0]})
    assert [step.status_code for step in response.history] == [307]
    assert response.status_code == 200 and response.json()["status"] == "error"
    assert main.USERS_FILE.read_bytes() == users_before  # byte-identical: no migration, password or account change
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert saves == [] and sign_outs == []
    entries = _new_audit_entries(audit_before)  # one failed entry for the whole sequence, none for the redirect
    assert [(entry["action"], entry["resource"], entry["outcome"], entry["user_id"]) for entry in entries] == [
        ("update_password", "user:local-admin", "failed", "admin-1")]
    assert NEW_PASSWORDS[0] not in json.dumps(entries)
    on_disk = _file_account("local-admin")
    assert on_disk["session_generation"] == damaged
    assert ("email" not in on_disk) if trigger == "missing-email" else (on_disk["password_hash"] == "")
    assert _signed_in_as(earlier) is None and _signed_in_as(token) == "admin-1"


def test_a_redirected_valid_reset_still_saves_the_migration_and_revokes_earlier_sessions(client):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email"), _user("viewer-1", session_generation=4)])
    earlier = main.create_session("viewer-1", session_generation=4)
    audit_before = _audit_bytes()
    response = _browser(_admin_token()).put("/api/users/viewer-1/password/", json={"password": NEW_PASSWORDS[0]})
    assert [step.status_code for step in response.history] == [307]
    assert response.json() == {"status": "complete", "message": "Password updated."}
    assert _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL
    assert _file_account("viewer-1")["session_generation"] == 5
    assert _signed_in_as(earlier) is None
    resets = [entry for entry in _new_audit_entries(audit_before) if entry["action"] == "update_password"]
    assert [(entry["outcome"], entry["user_id"]) for entry in resets] == [("success", "admin-1")]
    signed_in = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0])
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"


# ---------------------------------------------------------------- a missing or unreadable users.json

@pytest.mark.parametrize("slash", [""] + SLASHES, ids=["canonical"] + SLASH_IDS)
def test_a_reset_request_never_writes_the_defaults_for_a_missing_users_json(client, slash):
    main.USERS_FILE.unlink(missing_ok=True)  # this test's own temporary users.json, if start-up wrote one
    _browser(main.create_session("admin-1", session_generation=0)).put(
        f"/api/users/admin-1/password{slash}", json={"password": NEW_PASSWORDS[0]})
    assert not main.USERS_FILE.exists()
    main.load_users()  # an ordinary load still writes the defaults, as before
    assert main.USERS_FILE.exists()


@pytest.mark.parametrize("slash", [""] + SLASHES, ids=["canonical"] + SLASH_IDS)
def test_a_reset_request_never_overwrites_an_unreadable_users_json(client, slash):
    main.USERS_FILE.write_text("{not json", encoding="utf-8")
    audit_before = _audit_bytes()
    _browser(main.create_session("admin-1", session_generation=0)).put(
        f"/api/users/admin-1/password{slash}", json={"password": NEW_PASSWORDS[0]})
    assert main.USERS_FILE.read_text(encoding="utf-8") == "{not json"
    assert [entry for entry in _new_audit_entries(audit_before) if entry.get("outcome") == "success"] == []


# ---------------------------------------------------------------- the read-only state never leaks

def test_the_read_only_state_does_not_outlive_a_reset_that_fails_with_an_error(client, monkeypatch):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email"), _user("viewer-1")])
    browser = _browser(_admin_token())
    users_before = main.USERS_FILE.read_bytes()

    def broken(user):
        raise RuntimeError("synthetic: unexpected failure inside the reset")
    monkeypatch.setattr(main, "_next_session_generation", broken)
    failed = browser.put("/api/users/viewer-1/password/", json={"password": NEW_PASSWORDS[0]})
    assert failed.status_code == 500
    assert main.USERS_FILE.read_bytes() == users_before
    assert main._USERS_LOAD_READ_ONLY.get() is False
    ordinary = browser.get("/api/users")  # the same client, after the error: migration is saved as before
    assert ordinary.status_code == 200 and _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL


def test_an_overlapping_ordinary_request_still_saves_the_migration(client, monkeypatch):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email"), _user("viewer-1", session_generation=None)])
    token = _admin_token()
    users_before = main.USERS_FILE.read_bytes()
    entered, go = threading.Event(), threading.Event()
    real_hash = main.hash_password

    def held_hash(password):  # the reset pauses after its first loads, outside the users.json lock
        entered.set()
        assert go.wait(60)
        return real_hash(password)
    monkeypatch.setattr(main, "hash_password", held_hash)
    results = {}
    reset = threading.Thread(target=lambda: results.setdefault("reset", _browser(token).put(
        "/api/users/viewer-1/password/", json={"password": NEW_PASSWORDS[0]})))
    reset.start()
    try:
        assert entered.wait(60)
        assert main.USERS_FILE.read_bytes() == users_before  # the reset's own loads so far wrote nothing
        ordinary = _browser(token).get("/api/users")  # while the reset is still in flight
        assert ordinary.status_code == 200 and _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL
        migrated = main.USERS_FILE.read_bytes()
    finally:
        go.set()
        reset.join(60)
    assert results["reset"].json()["status"] == "error"  # viewer-1's counter is damaged
    assert main.USERS_FILE.read_bytes() == migrated  # the refused reset wrote nothing after the ordinary save
