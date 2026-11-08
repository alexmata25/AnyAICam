"""Fix for the Codex read-only review of 9873afc (FIX REQUIRED, one Low finding), owner-approved 2026-11-07.

Before: the password-reset classification ran a regex over request.url.path. Starlette rebuilds that URL from the
decoded path and parses it again, so a decoded "?" or "#" ends the path there. A PUT to /api/users/viewer%3F1/password
reaches the reset for the account "viewer?1", but was classified by "/api/users/viewer", so its authentication could
save the legacy users.json migration before the reset was refused. The other way round, a PUT to
.../password/%23extra (not a reset: 404) was classified as one.

Now the classification asks the router itself (same routes, order and matching, on the decoded path), including the
slash redirect it answers with, followed the way a client requests the Location again.
"""
import json

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r10-password-1"
NEW_PASSWORD = "r10-new-password-1"
LOCAL_ADMIN_EMAIL = "local-admin@example.test"
LOCAL_ADMIN_PASSWORD = "r10-local-admin-environment-password"
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": [], "password_hash": "",
         "failed_login_attempts": 0, "locked_until": None, "invitation_status": "active"}
TRIGGERS = ["missing-email", "empty-hash"]
DELIMITERS = [("%3F", "?"), ("%23", "#"), ("%3f", "?")]
DELIMITER_IDS = ["question-mark", "hash", "lower-case"]
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
    with override_target(sqlite_path=tmp_path / "codex_r10.db"):
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
    return TestClient(main.app, raise_server_exceptions=False, cookies={main.SESSION_COOKIE_NAME: token})


def _signed_in_as(token):
    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [
        (b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())]})
    user = main.authenticated_user(request)
    return user["id"] if user else None


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


def _spy_on_load_modes(monkeypatch):
    """The read-only state seen by every users.json load: True while the request is classified as the reset."""
    modes = []
    real_load = main.load_users
    monkeypatch.setattr(main, "load_users",
                        lambda *args, **kwargs: modes.append(main._USERS_LOAD_READ_ONLY.get()) or real_load(*args,
                                                                                                             **kwargs))
    return modes


# ---------------------------------------------------------------- classification follows the router, both ways

@pytest.mark.parametrize("method, path, status, expected", [
    ("PUT", "/api/users/local-admin/password", 200, True),
    ("PUT", "/api/users/local-admin/password/", 307, True),  # r9: the slash redirect to the reset
    ("PUT", "/api/users/local-admin/password//", 307, True),
    ("PUT", "/api/users/viewer%3F1/password", 200, True),  # reaches the reset for "viewer?1"
    ("PUT", "/api/users/viewer%231/password", 200, True),  # reaches the reset for "viewer#1"
    ("PUT", "/api/users/viewer%3f1/password", 200, True),
    ("PUT", "/api/users/local%2Dadmin/password", 200, True),  # an encoded ordinary character
    ("PUT", "/api/users/local-admin/password/%23extra", 404, False),  # no route; was classified
    ("PUT", "/api/users/local-admin/password%3Fx", 404, False),  # no route; was classified
    ("PUT", "/api/users/local-admin/password%23", 404, False),  # no route; was classified
    ("PUT", "/api/users/viewer%3F1/password/", 307, False),  # redirected to /api/users/viewer?1/password: not a reset
    ("PUT", "/api/users/local-admin/passwords", 404, False),
    ("PUT", "/api/users/local-admin", None, False),  # the account update route
    ("GET", "/api/users/local-admin/password", 405, False),
    ("GET", "/api/users", 200, False),
])
def test_a_request_is_classified_as_the_reset_exactly_when_the_router_delivers_it_there(client, monkeypatch, method,
                                                                                         path, status, expected):
    _write_users_file([dict(ADMIN), _user("local-admin", session_generation=None),
                       _user("viewer?1", session_generation=None), _user("viewer#1", session_generation=None)])
    browser = _browser(_admin_token())
    modes = _spy_on_load_modes(monkeypatch)
    response = browser.request(method, path, json={"password": NEW_PASSWORD} if method == "PUT" else None,
                               follow_redirects=False)
    if status is not None:
        assert response.status_code == status
    assert modes  # the request's own authentication loaded users.json
    assert set(modes) == {expected}


# ---------------------------------------------------------------- an encoded "?" or "#" in the account id

@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
@pytest.mark.parametrize("encoded, decoded", DELIMITERS, ids=DELIMITER_IDS)
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_refused_reset_of_an_account_id_with_an_encoded_delimiter_writes_nothing(client, monkeypatch, trigger,
                                                                                   encoded, decoded, damaged):
    account = f"viewer{decoded}1"
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger), _user(account, session_generation=damaged)])
    earlier = main.create_session(account, session_generation=0)
    token = _admin_token()
    users_before, sessions_before, audit_before = (main.USERS_FILE.read_bytes(), main.SESSIONS_FILE.read_bytes(),
                                                   _audit_bytes())
    saves, sign_outs = _spy_on_writes(monkeypatch)
    response = _browser(token).put(f"/api/users/viewer{encoded}1/password", json={"password": NEW_PASSWORD})
    assert response.status_code == 200 and response.json()["status"] == "error"
    assert main.USERS_FILE.read_bytes() == users_before  # byte-identical: no migration, password or account change
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert saves == [] and sign_outs == []
    entries = _new_audit_entries(audit_before)  # one accurate failed entry, for the account the router delivered
    assert [(entry["action"], entry["resource"], entry["outcome"], entry["user_id"]) for entry in entries] == [
        ("update_password", f"user:{account}", "failed", "admin-1")]
    assert NEW_PASSWORD not in json.dumps(entries)
    assert "damaged" in response.json()["message"]
    assert _file_account(account)["session_generation"] == damaged
    on_disk = _file_account("local-admin")
    assert ("email" not in on_disk) if trigger == "missing-email" else (on_disk["password_hash"] == "")
    assert _signed_in_as(earlier) is None and _signed_in_as(token) == "admin-1"


@pytest.mark.parametrize("encoded", ["%3F", "%23"], ids=["question-mark", "hash"])
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_reset_of_a_missing_account_with_an_encoded_delimiter_writes_nothing(client, monkeypatch, trigger, encoded):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    users_before = main.USERS_FILE.read_bytes()
    saves, _ = _spy_on_writes(monkeypatch)
    response = _browser(_admin_token()).put(f"/api/users/ghost{encoded}1/password", json={"password": NEW_PASSWORD})
    assert response.json() == {"status": "error", "message": "User not found."}
    assert main.USERS_FILE.read_bytes() == users_before
    assert saves == []


def test_a_valid_reset_of_an_account_id_with_an_encoded_delimiter_still_works(client):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email"), _user("viewer#1", session_generation=4)])
    earlier = main.create_session("viewer#1", session_generation=4)
    audit_before = _audit_bytes()
    response = _browser(_admin_token()).put("/api/users/viewer%231/password", json={"password": NEW_PASSWORD})
    assert response.json() == {"status": "complete", "message": "Password updated."}
    assert _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL  # the migration is saved with the reset
    assert _file_account("viewer#1")["session_generation"] == 5
    assert main.verify_password(NEW_PASSWORD, _file_account("viewer#1")["password_hash"])
    assert _signed_in_as(earlier) is None
    resets = [entry for entry in _new_audit_entries(audit_before) if entry["action"] == "update_password"]
    assert [(entry["resource"], entry["outcome"], entry["user_id"]) for entry in resets] == [
        ("user:viewer#1", "success", "admin-1")]


# ---------------------------------------------------------------- requests that are not a reset are ordinary

@pytest.mark.parametrize("path", ["/api/users/local-admin/password/%23extra", "/api/users/local-admin/password%3Fx"],
                         ids=["hash-after-slash", "question-mark-suffix"])
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_request_that_is_not_a_reset_saves_the_migration_as_any_ordinary_request(client, trigger, path):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    response = _browser(_admin_token()).put(path, json={"password": NEW_PASSWORD})
    assert response.status_code == 404
    on_disk = _file_account("local-admin")
    if trigger == "missing-email":
        assert on_disk["email"] == LOCAL_ADMIN_EMAIL
    else:
        assert main.verify_password(LOCAL_ADMIN_PASSWORD, on_disk["password_hash"])
    assert main._USERS_LOAD_READ_ONLY.get() is False


# ---------------------------------------------------------------- the router's own decisions, on decoded paths

def _scope(path, method="PUT"):
    """A request scope as the server builds it: the path already percent-decoded once."""
    return {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "headers": [(b"host", b"testserver")]}


@pytest.mark.parametrize("path, method, expected", [
    ("/api/users/viewer?1/password", "PUT", True),
    ("/api/users/viewer#1/password", "PUT", True),
    ("/api/users/local-admin/password///", "PUT", True),
    # sent as %252F: the redirect Location keeps %2F, which the server decodes, so the client lands on the reset of "a"
    ("/api/users/a%2Fpassword/", "PUT", True),
    # sent as %252F..%252F: after the redirect the decoded path has a literal ".." segment, which matches no route
    ("/api/users/local-admin%2F..%2Fviewer-1/password/", "PUT", False),
    ("/api/users/local-admin/password/#extra", "PUT", False),
    ("/api/users/local-admin/password?x", "PUT", False),
    ("/api/users/viewer?1/password/", "PUT", False),
    ("/api/users/local-admin/password", "PATCH", False),
])
def test_the_classification_is_the_routers_own_decision(path, method, expected):
    assert main._request_reaches_password_reset(_scope(path, method)) is expected


def test_a_redirect_chain_longer_than_clients_follow_is_treated_as_the_reset(monkeypatch):
    monkeypatch.setattr(main, "_router_outcome", lambda scope: ("redirect", "http://testserver/elsewhere/"))
    assert main._request_reaches_password_reset(_scope("/elsewhere")) is True


@pytest.mark.parametrize("path, expected", [
    ("/api/users/a/../b/password", "/api/users/b/password"),
    ("/api/users/./a/password", "/api/users/a/password"),
    ("/a/b/..", "/a/"),
    ("/a/.", "/a/"),
    ("/../a", "/a"),
    ("/api/users/a%2F..%2Fb/password", "/api/users/a%2F..%2Fb/password"),
])
def test_dot_segments_are_removed_as_clients_do_before_following_a_redirect(path, expected):
    assert main._remove_dot_segments(path) == expected
