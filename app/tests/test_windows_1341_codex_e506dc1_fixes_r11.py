"""Fix for the Codex read-only review of e506dc1 (FIX REQUIRED, one Low finding), owner-approved 2026-11-08.

Before: when the router answered a request with a slash redirect, the classification followed the Location only with
its dot segments removed, as browsers and httpx do. A client may resend it as supplied (curl --path-as-is). A PUT to
/api/users/%2E/password/ is redirected to /api/users/./password, which the classification read as /api/users/password
(not a reset), so the redirected request's own authentication could save the legacy users.json migration; resent as
supplied, the Location reaches the reset for the account ".", which is then refused after users.json changed.

Now a redirect target is followed both as supplied and normalized, and the request is classified as the reset if any of
them reaches it (conservative).
"""
import json

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r11-password-1"
NEW_PASSWORD = "r11-new-password-1"
LOCAL_ADMIN_EMAIL = "local-admin@example.test"
LOCAL_ADMIN_PASSWORD = "r11-local-admin-environment-password"
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": [], "password_hash": "",
         "failed_login_attempts": 0, "locked_until": None, "invitation_status": "active"}
TRIGGERS = ["missing-email", "empty-hash"]
# (the first request, the Location resent as supplied, the account id that reaches the reset)
DOT_FORMS = [("%2E", "%2E", "."), ("%2e", "%2E", "."), ("%2E%2E", "%2E%2E", "..")]
DOT_IDS = ["dot", "lower-case-dot", "dot-dot"]
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
    with override_target(sqlite_path=tmp_path / "codex_r11.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"user{len(user_id)}@example.test",
                 "role": "viewer", "enabled": True, "super_admin": False, "site_ids": ["home"], "camera_ids": [],
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


def _snapshot():
    return main.USERS_FILE.read_bytes(), main.SESSIONS_FILE.read_bytes(), _audit_bytes()


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


def _redirect_then_resend_as_supplied(browser, first, resend):
    """The redirected PUT, then its Location resent as supplied (curl --path-as-is). httpx removes a literal "." or
    ".." segment from a URL, so the resend spells it %2E, which the server decodes to the same path."""
    redirected = browser.put(first, json={"password": NEW_PASSWORD}, follow_redirects=False)
    assert redirected.status_code == 307
    return redirected, browser.put(resend, json={"password": NEW_PASSWORD}, follow_redirects=False)


# ---------------------------------------------------------------- the redirected request is classified as the reset

@pytest.mark.parametrize("first, resend, account", DOT_FORMS, ids=DOT_IDS)
def test_a_slash_redirect_to_a_dot_segment_reset_is_classified_as_the_reset(client, monkeypatch, first, resend,
                                                                           account):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email")])
    browser = _browser(_admin_token())
    modes = _spy_on_load_modes(monkeypatch)
    response = browser.put(f"/api/users/{first}/password/", json={"password": NEW_PASSWORD}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith(f"/api/users/{account}/password")  # resent as supplied: the reset
    assert modes and set(modes) == {True}


@pytest.mark.parametrize("damaged", DAMAGED, ids=DAMAGED_IDS)
@pytest.mark.parametrize("first, resend, account", DOT_FORMS, ids=DOT_IDS)
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_redirect_and_a_verbatim_resend_refused_for_a_damaged_counter_change_nothing(client, monkeypatch, trigger,
                                                                                        first, resend, account,
                                                                                        damaged):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger), _user(account, session_generation=damaged)])
    earlier = main.create_session(account, session_generation=0)
    token = _admin_token()
    users_before, sessions_before, audit_before = _snapshot()
    saves, sign_outs = _spy_on_writes(monkeypatch)
    _, refused = _redirect_then_resend_as_supplied(_browser(token), f"/api/users/{first}/password/",
                                                   f"/api/users/{resend}/password")
    assert refused.status_code == 200 and "damaged" in refused.json()["message"]
    assert main.USERS_FILE.read_bytes() == users_before  # byte-identical: no migration, password or account change
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert saves == [] and sign_outs == []
    entries = _new_audit_entries(audit_before)  # one failed entry for the reset, none for the redirect
    assert [(entry["action"], entry["resource"], entry["outcome"], entry["user_id"]) for entry in entries] == [
        ("update_password", f"user:{account}", "failed", "admin-1")]
    assert NEW_PASSWORD not in json.dumps(entries)
    on_disk = _file_account("local-admin")
    assert ("email" not in on_disk) if trigger == "missing-email" else (on_disk["password_hash"] == "")
    assert _signed_in_as(earlier) is None and _signed_in_as(token) == "admin-1"


@pytest.mark.parametrize("first, resend, account", DOT_FORMS, ids=DOT_IDS)
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_redirect_and_a_verbatim_resend_for_a_missing_account_change_nothing(client, monkeypatch, trigger, first,
                                                                                resend, account):
    # The Codex scenario: no unusual stored account is needed.
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    token = _admin_token()
    users_before, sessions_before, audit_before = _snapshot()
    saves, sign_outs = _spy_on_writes(monkeypatch)
    _, refused = _redirect_then_resend_as_supplied(_browser(token), f"/api/users/{first}/password/",
                                                   f"/api/users/{resend}/password")
    assert refused.json() == {"status": "error", "message": "User not found."}
    assert main.USERS_FILE.read_bytes() == users_before
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert _new_audit_entries(audit_before) == []  # a missing account is refused before any audit entry
    assert saves == [] and sign_outs == []


def test_a_valid_reset_resent_as_supplied_still_works_and_saves_the_migration(client):
    _write_users_file([dict(ADMIN), _legacy_local_admin("missing-email"), _user(".", session_generation=4)])
    earlier = main.create_session(".", session_generation=4)
    audit_before = _audit_bytes()
    redirected, reset = _redirect_then_resend_as_supplied(_browser(_admin_token()), "/api/users/%2E/password/",
                                                          "/api/users/%2E/password")
    assert reset.json() == {"status": "complete", "message": "Password updated."}
    assert _file_account("local-admin")["email"] == LOCAL_ADMIN_EMAIL  # the migration is saved with the reset
    assert _file_account(".")["session_generation"] == 5
    assert main.verify_password(NEW_PASSWORD, _file_account(".")["password_hash"])
    assert _signed_in_as(earlier) is None
    resets = [entry for entry in _new_audit_entries(audit_before) if entry["action"] == "update_password"]
    assert [(entry["resource"], entry["outcome"], entry["user_id"]) for entry in resets] == [
        ("user:.", "success", "admin-1")]


# ---------------------------------------------------------------- the r10 missing-account case, all three stores

@pytest.mark.parametrize("encoded", ["%3F", "%23"], ids=["question-mark", "hash"])
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_an_encoded_delimiter_reset_of_a_missing_account_leaves_every_store_unchanged(client, monkeypatch, trigger,
                                                                                      encoded):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    main.create_session("local-admin", session_generation=0)
    token = _admin_token()
    users_before, sessions_before, audit_before = _snapshot()
    saves, sign_outs = _spy_on_writes(monkeypatch)
    response = _browser(token).put(f"/api/users/ghost{encoded}1/password", json={"password": NEW_PASSWORD})
    assert response.json() == {"status": "error", "message": "User not found."}
    assert main.USERS_FILE.read_bytes() == users_before
    assert main.SESSIONS_FILE.read_bytes() == sessions_before
    assert _new_audit_entries(audit_before) == []
    assert saves == [] and sign_outs == []


# ---------------------------------------------------------------- requests that reach no reset stay ordinary

@pytest.mark.parametrize("path, status", [
    ("/api/users/x/%2E%2E/password/", 404),  # literal ".." segment: no route, no redirect
    ("/api/users/viewer%3F1/password/", 307),  # redirected to /api/users/viewer?1/password: the account update route
    ("/api/users/local-admin/password/%23extra", 404),
])
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_a_request_that_reaches_no_reset_either_way_saves_the_migration_as_before(client, monkeypatch, trigger, path,
                                                                                 status):
    _write_users_file([dict(ADMIN), _legacy_local_admin(trigger)])
    modes = _spy_on_load_modes(monkeypatch)
    response = _browser(_admin_token()).put(path, json={"password": NEW_PASSWORD}, follow_redirects=False)
    assert response.status_code == status
    assert modes and set(modes) == {False}
    on_disk = _file_account("local-admin")
    if trigger == "missing-email":
        assert on_disk["email"] == LOCAL_ADMIN_EMAIL
    else:
        assert main.verify_password(LOCAL_ADMIN_PASSWORD, on_disk["password_hash"])


# ---------------------------------------------------------------- the classification on decoded scopes

def _scope(path, method="PUT"):
    """A request scope as the server builds it: the path already percent-decoded once."""
    return {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "headers": [(b"host", b"testserver")]}


@pytest.mark.parametrize("path, expected", [
    ("/api/users/./password/", True),  # reaches the reset for "." when resent as supplied
    ("/api/users/../password/", True),  # reaches the reset for ".." when resent as supplied
    ("/api/users/%2E/password/", True),  # Location keeps %2E; the server decodes it to "."
    ("/api/users/local-admin/password/", True),  # r9
    ("/api/users/viewer?1/password", True),  # r10
    ("/api/users/a%2Fpassword/", True),  # r10
    ("/api/users/x/../password/", False),  # no route, no redirect
    ("/api/users/viewer?1/password/", False),  # r10: the account update route either way
    ("/api/users/local-admin%2F..%2Fviewer-1/password/", False),  # r10: either way a literal ".." segment, no route
])
def test_the_classification_follows_a_redirect_both_as_supplied_and_normalized(path, expected):
    assert main._request_reaches_password_reset(_scope(path)) is expected


def test_a_redirect_tree_too_large_to_follow_is_treated_as_the_reset(monkeypatch):
    counter = iter(range(10 ** 6))
    monkeypatch.setattr(main, "_router_outcome",
                        lambda scope: ("redirect", f"http://testserver/a/./{next(counter)}/../b"))
    assert main._request_reaches_password_reset(_scope("/start")) is True


@pytest.mark.parametrize("path, expected", [
    ("/api/users/%2E/password", "/api/users/password"),
    ("/api/users/%2e%2E/password", "/api/password"),
    ("/api/users/.%2e/password", "/api/password"),
    ("/api/users/a%2F..%2Fb/password", "/api/users/a%2F..%2Fb/password"),
    ("/api/users/./a/password", "/api/users/a/password"),
])
def test_browsers_also_treat_an_encoded_dot_segment_as_a_dot_segment(path, expected):
    assert main._remove_dot_segments(path, encoded_dots=True) == expected
