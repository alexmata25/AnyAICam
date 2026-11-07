"""Fixes for the Codex read-only review of a0a74ea (FIX REQUIRED), owner-approved 2026-11-06.

F1, before: user creation and the administrator password reset checked the administrator's permission, hashed the
password outside the users.json lock, then wrote without checking again, so an administrator disabled or demoted during
the hash could still create an account or reset a password. Now the identity is resolved again under the lock.

F2, before: a restore registered its temporary files only after writing them (a partial write was left behind), and a
cleanup failure in `finally` replaced the restore's own result or error. Now each file is tracked before it is
written, removed on its own, and a cleanup failure is reported beside the result, never instead of it.

F3, before: a spent or cancelled invitation (and a duplicate email, a missing user) was refused only after the bcrypt
hash, so a retained invitation link could make the server hash repeatedly. Now those are refused before the hash.

F4, before: a successful pairing claim cleared the account's failures, so an account could fail, claim its own code and
fail again without ever reaching the limit. Now failures count for the whole window.

Audit addresses, before: request_device_context() recorded the raw X-Forwarded-For header. Now the verified address.

Password reset, before: an administrator's reset left the user's existing sessions valid. Now they are refused,
including a sign-in that was already checking the old password and a sessions.json copy put back later.
"""
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r4-password-1"
NEW_PASSWORD = "r4-brand-new-password-1"
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": []}


@pytest.fixture(autouse=True)
def _isolated_recordings(tmp_path, monkeypatch):
    # Never read or write the shared recordings root (C:\app\recordings).
    from recordings_isolation import isolate_recordings
    monkeypatch.setattr(main, "_pairing_claim_failures", {})
    return isolate_recordings(monkeypatch, tmp_path)


def test_recordings_derived_paths_are_isolated_for_this_module(_isolated_recordings):
    from recordings_isolation import assert_isolated
    assert_isolated(_isolated_recordings)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name, file_name in (("USERS_FILE", "users.json"), ("SESSIONS_FILE", "sessions.json"),
                            ("USER_INVITES_FILE", "user_invites.json"), ("MOBILE_DEVICES_FILE", "mobile_devices.json"),
                            ("MOBILE_PAIRING_CODES_FILE", "mobile_pairing_codes.json")):
        monkeypatch.setattr(main, name, tmp_path / file_name)
    monkeypatch.setattr(main, "MAX_LOGIN_ATTEMPTS", 1000)
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    with override_target(sqlite_path=tmp_path / "codex_r4.db"):
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


@pytest.fixture()
def hash_spy(monkeypatch):
    calls = []
    real_hash = main.hash_password

    def spy(password):
        calls.append(main._USERS_FILE_LOCK._is_owned())
        return real_hash(password)
    monkeypatch.setattr(main, "hash_password", spy)
    return calls


# ---------------------------------------------------------------- F1 administrator rechecked under the lock

def _while_hashing(monkeypatch, change):
    """Runs change() while the route is hashing the password: after its first permission check, before its write."""
    real_hash = main.hash_password

    def hash_then_change(password):
        hashed = real_hash(password)
        change()
        return hashed
    monkeypatch.setattr(main, "hash_password", hash_then_change)


def _disable_admin():
    users = main.load_users()
    next(user for user in users if user["id"] == "admin-1")["enabled"] = False
    main.save_users(users)


def _demote_admin():
    users = main.load_users()
    next(user for user in users if user["id"] == "admin-1")["role"] = "viewer"
    main.save_users(users)


@pytest.mark.parametrize("change", [_disable_admin, _demote_admin], ids=["disabled", "demoted"])
def test_an_administrator_changed_during_the_hash_cannot_create_a_user(client, monkeypatch, change):
    main.save_users([dict(ADMIN)])
    admin = _cookies("admin-1")
    _while_hashing(monkeypatch, change)
    response = client.post("/api/users", cookies=admin, json={"display_name": "Late One", "email": "late@example.test",
                                                               "password": PASSWORD, "role": "administrator"})
    assert response.json() == {"status": "error", "message": "Administrator permission required."}
    assert not [user for user in main.load_users() if user["email"] == "late@example.test"]


@pytest.mark.parametrize("change", [_disable_admin, _demote_admin], ids=["disabled", "demoted"])
def test_an_administrator_changed_during_the_hash_cannot_reset_a_password(client, monkeypatch, change):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    before = _account("viewer-1")["password_hash"]
    admin = _cookies("admin-1")
    _while_hashing(monkeypatch, change)
    response = client.put("/api/users/viewer-1/password", cookies=admin, json={"password": NEW_PASSWORD})
    assert response.json() == {"status": "error", "message": "Administrator permission required."}
    assert _account("viewer-1")["password_hash"] == before and "sessions_valid_after" not in _account("viewer-1")


def test_an_unchanged_administrator_still_creates_and_resets(client, hash_spy):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    hash_spy.clear()  # _user() hashes its fixture password
    admin = _cookies("admin-1")
    created = client.post("/api/users", cookies=admin, json={"display_name": "New One", "email": "new@example.test",
                                                              "password": PASSWORD, "role": "viewer"})
    assert created.json()["status"] == "complete"
    reset = client.put("/api/users/viewer-1/password", cookies=admin, json={"password": NEW_PASSWORD})
    assert reset.json() == {"status": "complete", "message": "Password updated."}
    assert hash_spy == [False, False]  # still hashed outside the lock


# ---------------------------------------------------------------- F3 refused before the hash

def _invite(invite_id="inv-1", email="invitee@example.test", status="pending"):
    main.save_user_invites([{"id": invite_id, "email": email, "role": "viewer", "status": status, "all_cameras": True}])
    return main.session_serializer.dumps({"invite_id": invite_id, "email": email}, salt="anyaicam-user-invite")


def _accept(client, token):
    return client.post("/api/user-invites/accept", json={"token": token, "display_name": "Invitee", "password": PASSWORD}).json()


def test_a_spent_invitation_is_refused_before_the_hash_every_time(client, hash_spy):
    main.save_users([dict(ADMIN)])
    token = _invite()
    assert _accept(client, token)["status"] == "complete"
    assert hash_spy == [False]
    for _ in range(5):
        assert _accept(client, token) == {"status": "error", "message": "Invitation is no longer available."}
    assert hash_spy == [False]  # no further hashes


def test_a_cancelled_invitation_and_a_taken_email_are_refused_before_the_hash(client, hash_spy):
    main.save_users([dict(ADMIN), _user("taken")])
    hash_spy.clear()
    assert _accept(client, _invite(status="cancelled")) == {"status": "error", "message": "Invitation is no longer available."}
    taken = _invite(invite_id="inv-2", email="taken@example.test")
    assert _accept(client, taken) == {"status": "error", "message": "An account already exists for this email."}
    assert hash_spy == []
    assert main.load_user_invites()[0]["status"] == "pending"  # a refusal changes nothing


def test_a_duplicate_email_and_a_missing_user_are_refused_before_the_hash(client, hash_spy):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    hash_spy.clear()
    admin = _cookies("admin-1")
    duplicate = client.post("/api/users", cookies=admin, json={"display_name": "Again", "email": "VIEWER-1@example.test",
                                                                "password": PASSWORD, "role": "viewer"})
    assert duplicate.json() == {"status": "error", "message": "That email is already in use."}
    missing = client.put("/api/users/nobody/password", cookies=admin, json={"password": NEW_PASSWORD})
    assert missing.json() == {"status": "error", "message": "User not found."}
    assert hash_spy == []


def test_an_invitation_spent_during_the_hash_is_still_refused_under_the_lock(client, monkeypatch):
    main.save_users([dict(ADMIN)])
    token = _invite()

    def spend():
        invites = main.load_user_invites()
        invites[0]["status"] = "accepted"
        main.save_user_invites(invites)
    _while_hashing(monkeypatch, spend)
    assert _accept(client, token) == {"status": "error", "message": "Invitation is no longer available."}
    assert not [user for user in main.load_users() if user["email"] == "invitee@example.test"]


# ---------------------------------------------------------------- F4 pairing failures kept for the window

def _new_code(client, cookies, name="Phone"):
    response = client.post("/api/mobile/pairing", cookies=cookies, json={"device_name": name})
    assert response.status_code == 200, response.text[:300]
    return response.json()["pairing"]["code"]


def _claim(client, cookies, code, headers=None):
    return client.post("/api/mobile/pairing/claim", cookies=cookies, headers=headers or {},
                       json={"code": code, "device_name": "Phone"})


def _wrong(code):
    return f"{(int(code) + 1) % 1000000:06d}"


def test_a_successful_claim_does_not_clear_earlier_failures(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin, viewer = _cookies("admin-1"), _cookies("viewer-1")
    admin_code = _new_code(client, admin)
    limit = main.PAIRING_CLAIM_MAX_FAILURES
    assert [_claim(client, viewer, _wrong(admin_code)).status_code for _ in range(limit - 1)] == [400] * (limit - 1)
    assert _claim(client, viewer, _new_code(client, viewer)).status_code == 200  # its own code, inside the window
    assert len(main._pairing_claim_failures["account:viewer-1"]) == limit - 1  # still counted
    assert _claim(client, viewer, _wrong(admin_code)).status_code == 400  # the limit-th failure
    assert _claim(client, viewer, _new_code(client, viewer)).status_code == 429  # even its own correct code waits


# ---------------------------------------------------------------- audit addresses from the verified client

def test_the_recorded_address_is_the_verified_client_not_the_forwarded_header():
    request = Request({"type": "http", "method": "POST", "path": "/", "client": ("198.51.100.7", 50000),
                       "headers": [(b"x-forwarded-for", b"203.0.113.99, 10.0.0.1"), (b"user-agent", b"probe")]})
    context = main.request_device_context(request)
    assert context["ip_address"] == "198.51.100.7" and context["user_agent"] == "probe"
    assert main.request_device_context(Request({"type": "http", "method": "GET", "path": "/", "headers": []}))["ip_address"] == ""


def test_a_paired_device_records_the_verified_address(client):
    main.save_users([dict(ADMIN)])
    admin = _cookies("admin-1")
    paired = _claim(client, admin, _new_code(client, admin), headers={"X-Forwarded-For": "203.0.113.50"})
    assert paired.status_code == 200
    assert paired.json()["device"]["paired_from_ip"] == "testclient"  # TestClient's connection address
    assert "203.0.113.50" not in json.dumps(main.load_mobile_devices())


# ---------------------------------------------------------------- password reset signs the user out

def test_a_password_reset_signs_the_user_out_everywhere_and_nobody_else(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("viewer-2")])
    first, second = main.create_session("viewer-1"), main.create_session("viewer-1", remember_me=True)
    other, admin = main.create_session("viewer-2"), main.create_session("admin-1")
    kept_copy = main.SESSIONS_FILE.read_bytes()
    reset = client.put("/api/users/viewer-1/password", cookies={main.SESSION_COOKIE_NAME: admin}, json={"password": NEW_PASSWORD})
    assert reset.json() == {"status": "complete", "message": "Password updated."}
    assert _signed_in_as(first) is None and _signed_in_as(second) is None
    assert _signed_in_as(other) == "viewer-2" and _signed_in_as(admin) == "admin-1"
    stored = json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))
    assert not [value for value in stored.values() if value.get("user_id") == "viewer-1"]
    # An older sessions.json put back can't bring them back: the refusal is recorded on the account in users.json.
    main.SESSIONS_FILE.write_bytes(kept_copy)
    assert _signed_in_as(first) is None and _signed_in_as(second) is None and _signed_in_as(other) == "viewer-2"
    # The new password signs in, and that session works.
    signed_in = _sign_in("viewer-1@example.test", NEW_PASSWORD)
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"
    assert _sign_in("viewer-1@example.test", PASSWORD).status_code == 401


def test_a_sign_in_already_under_way_when_the_reset_lands_gets_no_session(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = _cookies("admin-1")
    began = time.time()  # the sign-in has read users.json and is checking the old password
    assert client.put("/api/users/viewer-1/password", cookies=admin, json={"password": NEW_PASSWORD}).json()["status"] == "complete"
    late = main.create_session("viewer-1", authenticated_at=began)
    assert _signed_in_as(late) is None
    assert _signed_in_as(main.create_session("viewer-1")) == "viewer-1"  # a sign-in that began after the reset


def test_a_session_record_without_a_sign_in_time_is_judged_by_its_creation_time(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("viewer-2")])
    old = main.create_session("viewer-1")
    untouched = main.create_session("viewer-2")
    stored = json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))
    for value in stored.values():  # records written before r4 have no authenticated_at
        value.pop("authenticated_at")
    main.SESSIONS_FILE.write_text(json.dumps(stored), encoding="utf-8")
    assert _signed_in_as(old) == "viewer-1" and _signed_in_as(untouched) == "viewer-2"  # no reset: unchanged
    kept_copy = main.SESSIONS_FILE.read_bytes()
    client.put("/api/users/viewer-1/password", cookies=_cookies("admin-1"), json={"password": NEW_PASSWORD})
    main.SESSIONS_FILE.write_bytes(kept_copy)
    assert _signed_in_as(old) is None and _signed_in_as(untouched) == "viewer-2"


def test_a_reset_still_signs_the_user_out_when_the_session_file_cannot_be_updated(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    session, admin = main.create_session("viewer-1"), _cookies("admin-1")
    real_save = main.save_sessions

    def refuse(sessions):
        raise OSError("synthetic: sessions.json is read-only")
    monkeypatch.setattr(main, "save_sessions", refuse)
    reset = client.put("/api/users/viewer-1/password", cookies=admin, json={"password": NEW_PASSWORD})
    assert reset.json() == {"status": "complete", "message": "Password updated."}
    monkeypatch.setattr(main, "save_sessions", real_save)
    assert _signed_in_as(session) is None
    assert main.verify_password(NEW_PASSWORD, _account("viewer-1")["password_hash"])


# ---------------------------------------------------------------- F2 restore cleanup and reporting

@pytest.fixture()
def restore(client, monkeypatch, tmp_path):
    assert tmp_path in main.RESTORE_STAGING_FOLDER.parents  # redirected by isolate_recordings, never the shared root
    main.RESTORE_STAGING_FOLDER.mkdir(parents=True, exist_ok=True)
    rules = tmp_path / "alert_rules.json"
    monkeypatch.setattr(main, "CONFIG_BACKUP_FILES", [rules, main.USERS_FILE, main.USER_INVITES_FILE])
    main.save_users([dict(ADMIN)])
    main.save_user_invites([{"id": "inv-old", "email": "old@example.test", "status": "pending"}])
    cookies = _cookies("admin-1")
    before = {"users": main.USERS_FILE.read_bytes(), "invites": main.USER_INVITES_FILE.read_bytes()}
    restored_users = json.dumps([dict(ADMIN), _user("restored-1")])
    members = [("config/alert_rules.json", json.dumps({"rules": []})), ("config/users.json", restored_users),
               ("config/user_invites.json", "[]")]

    def send():
        import io
        import zipfile
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            for name, data in members:
                bundle.writestr(name, data)
        return _fresh().post("/api/operations/restore", cookies=cookies,
                             files={"backup_file": ("restore.zip", archive.getvalue(), "application/zip")}).json()

    def unchanged():
        return (main.USERS_FILE.read_bytes() == before["users"] and main.USER_INVITES_FILE.read_bytes() == before["invites"]
                and not rules.exists())

    def leftovers():
        return sorted(path.name for path in tmp_path.iterdir() if path.name.endswith((".restore", ".rollback")))
    return {"send": send, "unchanged": unchanged, "leftovers": leftovers, "rules": rules, "before": before}


def test_a_partial_staging_write_is_removed_and_nothing_changes(restore, monkeypatch):
    real_write = Path.write_bytes

    def partial(self, data):
        if self.name.startswith("users.json.") and self.name.endswith(".restore"):
            real_write(self, data[:7])
            raise OSError("synthetic: disk full while staging")
        return real_write(self, data)
    monkeypatch.setattr(Path, "write_bytes", partial)
    result = restore["send"]()
    assert result["status"] == "error" and "disk full while staging" in result["message"]
    assert restore["unchanged"]() and restore["leftovers"]() == []


def test_a_partial_pre_restore_copy_is_removed_and_nothing_changes(restore, monkeypatch):
    real_copy = shutil.copy2

    def partial(source, destination, *args, **kwargs):
        if Path(destination).name.startswith("user_invites.json.") and str(destination).endswith(".rollback"):
            Path(destination).write_bytes(b"[")
            raise OSError("synthetic: copy failed part-way")
        return real_copy(source, destination, *args, **kwargs)
    monkeypatch.setattr(shutil, "copy2", partial)
    result = restore["send"]()
    assert result["status"] == "error" and "copy failed part-way" in result["message"]
    assert restore["unchanged"]() and restore["leftovers"]() == []


def _unlink_refusing(monkeypatch, suffix):
    real_unlink = Path.unlink

    def refuse(self, missing_ok=False):
        if self.name.endswith(suffix) and self.exists():
            raise PermissionError("synthetic: file in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", refuse)


def test_a_cleanup_failure_after_a_committed_restore_still_reports_it_restored(restore, monkeypatch):
    _unlink_refusing(monkeypatch, ".rollback")
    result = restore["send"]()
    assert result["status"] == "complete" and result["restored"] == ["alert_rules.json", "users.json", "user_invites.json"]
    assert {user["id"] for user in main.load_users()} == {"admin-1", "restored-1"} and main.load_user_invites() == []
    assert sorted(result["leftover"]) == [name for name in restore["leftovers"]() if name.endswith(".rollback")]
    assert len(result["leftover"]) == 2  # copies of users.json and user_invites.json (alert_rules.json was new)


def test_a_cleanup_failure_after_a_failed_swap_keeps_the_swap_error(restore, monkeypatch):
    real_replace = Path.replace

    def fail_third(self, target):
        if self.name.endswith(".restore") and Path(target).name == "user_invites.json":
            raise OSError("synthetic disk failure on the third file")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", fail_third)
    _unlink_refusing(monkeypatch, ".restore")
    result = restore["send"]()
    assert result["status"] == "error" and "synthetic disk failure on the third file" in result["message"]
    assert "temporary files left behind: user_invites.json." in result["message"]
    assert restore["unchanged"]()


def test_a_cleanup_failure_never_hides_a_failed_rollback(restore, monkeypatch):
    real_replace = Path.replace

    def fail_swap_and_rollback(self, target):
        if self.name.endswith(".restore") and Path(target).name == "user_invites.json":
            raise OSError("synthetic disk failure on the third file")
        if self.name.endswith(".rollback") and Path(target).name == "users.json":
            raise OSError("synthetic rollback failure")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", fail_swap_and_rollback)
    _unlink_refusing(monkeypatch, ".restore")
    result = restore["send"]()
    assert result["status"] == "error" and "could not put back users.json" in result["message"]
    kept = [name for name in restore["leftovers"]() if name.endswith(".rollback")]
    assert len(kept) == 1 and kept[0] in result["message"]
    assert (main.USERS_FILE.parent / kept[0]).read_bytes() == restore["before"]["users"]
    assert "temporary files left behind" in result["message"]
