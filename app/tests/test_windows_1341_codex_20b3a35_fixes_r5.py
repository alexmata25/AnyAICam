"""Fixes for the Codex read-only review of 20b3a35 (FIX REQUIRED), owner-approved 2026-11-07.

Finding 1, before: the password-reset sign-out compared a session's sign-in time with the reset time, so a clock
stepped backward let a sign-in already checking the old password keep its session. Now a reset advances a per-account
session counter in users.json, a sign-in records the counter from the same read it checked the password against, and a
session is accepted only while its counter is current. No clock is compared.

Finding 2, before: a session store that could not be read (SessionStoreUnreadable, a RuntimeError) during the reset's
sign-out turned a committed reset into a server error and skipped its audit entry. Now the reset still completes,
and its audit entry says the store could not be cleaned up.

Finding 3, before: the restore route deleted the uploaded archive in `finally`, so a failed deletion replaced the
restore's own result or error. Now it is removed after the outcome is settled and reported beside it.

Finding 4, before: an administrator resetting their own password signed their own session out before the audit entry
resolved the request again, so the entry named "anonymous". Now it names the administrator.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "r5-password-1"
NEW_PASSWORDS = ["r5-new-password-1", "r5-new-password-2", "r5-new-password-3"]
ADMIN = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
         "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": []}


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
    with override_target(sqlite_path=tmp_path / "codex_r5.db"):
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


def _reset_entries():
    return [entry for entry in main.load_audit_entries() if entry.get("action") == "update_password"]


@pytest.fixture()
def logged(monkeypatch):
    events = []
    real_log = main.structured_log

    def capture(event, *args, **kwargs):
        events.append((event, kwargs))
        return real_log(event, *args, **kwargs)
    monkeypatch.setattr(main, "structured_log", capture)
    return events


# ---------------------------------------------------------------- finding 1 per-account session counter

def test_repeated_resets_each_refuse_every_earlier_session_and_nobody_else(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("viewer-2")])
    admin, other = _cookies("admin-1"), main.create_session("viewer-2")
    earlier = [main.create_session("viewer-1")]
    password = PASSWORD
    for number, new_password in enumerate(NEW_PASSWORDS, start=1):
        kept_copy = main.SESSIONS_FILE.read_bytes()
        assert _reset(client, "viewer-1", new_password, admin) == {"status": "complete", "message": "Password updated."}
        assert _account("viewer-1")["session_generation"] == number
        assert "sessions_valid_after" not in _account("viewer-1")
        main.SESSIONS_FILE.write_bytes(kept_copy)  # an older sessions.json put back
        assert [_signed_in_as(token) for token in earlier] == [None] * len(earlier)
        assert _signed_in_as(other) == "viewer-2" and _signed_in_as(admin[main.SESSION_COOKIE_NAME]) == "admin-1"
        assert _sign_in("viewer-1@example.test", password).status_code == 401
        signed_in = _sign_in("viewer-1@example.test", new_password)
        assert signed_in.status_code == 303
        earlier.append(signed_in.cookies[main.SESSION_COOKIE_NAME])
        assert _signed_in_as(earlier[-1]) == "viewer-1"
        password = new_password
    assert "session_generation" not in _account("viewer-2") and "session_generation" not in _account("admin-1")


def test_a_clock_stepped_backward_does_not_let_a_sign_in_under_way_keep_its_session(client, monkeypatch):
    # Codex 20b3a35 finding 1: the sign-in reads the old password at time 200, the clock moves back, the reset lands
    # at time 100, then the sign-in creates its session. Only the counter decides, so the session is refused.
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = _cookies("admin-1")
    import time as time_module
    base = time_module.time()  # offsets from now, so the signed cookies never look as if issued in the future
    clock = iter([base + 200.0, base + 100.0, base + 50.0])
    now = {"value": next(clock)}
    monkeypatch.setattr(time_module, "time", lambda: now["value"])
    snapshot = _account("viewer-1")
    now["value"] = next(clock)
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], admin)["status"] == "complete"
    now["value"] = next(clock)
    late = main.create_session("viewer-1", session_generation=snapshot.get("session_generation", 0))
    assert _signed_in_as(late) is None
    assert _signed_in_as(main.create_session("viewer-1")) == "viewer-1"  # a sign-in after the reset, clock even earlier


def test_a_real_sign_in_checking_the_old_password_when_the_reset_lands_gets_no_working_session(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = _cookies("admin-1")
    real_verify = main.verify_password
    resets = []

    def verify_then_reset(password, password_hash):
        matched = real_verify(password, password_hash)
        if not resets:  # the administrator's reset lands while this sign-in is checking the old password
            resets.append(_reset(client, "viewer-1", NEW_PASSWORDS[0], admin))
        return matched
    monkeypatch.setattr(main, "verify_password", verify_then_reset)
    late = _sign_in("viewer-1@example.test", PASSWORD)
    assert resets == [{"status": "complete", "message": "Password updated."}]
    assert late.status_code == 303  # the old password matched the users.json read this sign-in started from
    assert _signed_in_as(late.cookies[main.SESSION_COOKIE_NAME]) is None
    account = _account("viewer-1")
    assert account["session_generation"] == 1 and real_verify(NEW_PASSWORDS[0], account["password_hash"])
    monkeypatch.setattr(main, "verify_password", real_verify)
    fresh = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0])
    assert fresh.status_code == 303 and _signed_in_as(fresh.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"


def test_accounts_and_sessions_from_before_the_counter_keep_working_until_a_reset(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    session = main.create_session("viewer-1")
    stored = json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))
    for value in stored.values():
        value.pop("session_generation")
    main.SESSIONS_FILE.write_text(json.dumps(stored), encoding="utf-8")
    assert "session_generation" not in _account("viewer-1") and _signed_in_as(session) == "viewer-1"


def test_an_account_reset_under_the_r4_clock_cutoff_refuses_its_r4_sessions_only(client):
    main.save_users([dict(ADMIN), _user("viewer-1", sessions_valid_after=1.0), _user("viewer-2")])
    r4_style = main.create_session("viewer-1")
    other = main.create_session("viewer-2")
    stored = json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))
    for value in stored.values():  # r4 records carried authenticated_at and no counter
        value.pop("session_generation")
        value["authenticated_at"] = 99999999999.0
    main.SESSIONS_FILE.write_text(json.dumps(stored), encoding="utf-8")
    assert _signed_in_as(r4_style) is None and _signed_in_as(other) == "viewer-2"
    signed_in = _sign_in("viewer-1@example.test")
    assert signed_in.status_code == 303 and _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1"))["status"] == "complete"
    assert "sessions_valid_after" not in _account("viewer-1") and _account("viewer-1")["session_generation"] == 1


@pytest.mark.parametrize("damaged", ["1", -1, True, None, 1.5], ids=["text", "negative", "bool", "null", "float"])
def test_a_damaged_session_counter_refuses_every_session_until_a_reset(client, damaged):
    main.save_users([dict(ADMIN), _user("viewer-1", session_generation=damaged)])
    assert _signed_in_as(main.create_session("viewer-1")) is None
    assert _sign_in("viewer-1@example.test").status_code == 303  # the password still matches; no session works
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1"))["status"] == "complete"
    assert _account("viewer-1")["session_generation"] == 1
    signed_in = _sign_in("viewer-1@example.test", NEW_PASSWORDS[0])
    assert _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "viewer-1"


# ---------------------------------------------------------------- finding 2 a session-store failure during the sign-out

# Each failure hits only the sign-out's read for update; the administrator's own request still authenticates.

def _unreadable_store(monkeypatch):
    real_read = main._read_sessions_file
    real_load = main._load_sessions_for_update

    def refuse():
        raise main.SessionStoreUnreadable("sessions.json could not be read: PermissionError")

    def load_while_unreadable():
        main._read_sessions_file = refuse
        try:
            return real_load()
        finally:
            main._read_sessions_file = real_read
    monkeypatch.setattr(main, "_load_sessions_for_update", load_while_unreadable)


def _quarantine_fails(monkeypatch):
    # The store reads as damaged and can't be moved aside. The quarantine advances the session epoch first, which
    # signs everyone out by design (P3 N1-A).
    real_read = main._read_sessions_file
    real_load = main._load_sessions_for_update
    real_replace = Path.replace

    def refuse(self, target):
        if self == main.SESSIONS_FILE:
            raise PermissionError("synthetic: sessions.json is in use")
        return real_replace(self, target)

    def load_while_damaged():
        main._read_sessions_file = lambda: None
        Path.replace = refuse
        try:
            return real_load()
        finally:
            main._read_sessions_file = real_read
            Path.replace = real_replace
    monkeypatch.setattr(main, "_load_sessions_for_update", load_while_damaged)


def _unexpected_error(monkeypatch):
    def broken():
        raise RuntimeError("synthetic: unexpected failure")
    monkeypatch.setattr(main, "_load_sessions_for_update", broken)


@pytest.mark.parametrize("failure", [_unreadable_store, _quarantine_fails, _unexpected_error],
                         ids=["unreadable", "quarantine-fails", "unexpected"])
def test_a_session_store_failure_cannot_hide_a_committed_reset_or_its_audit_entry(client, monkeypatch, logged, failure):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    session, admin = main.create_session("viewer-1"), _cookies("admin-1")
    kept_copy = main.SESSIONS_FILE.read_bytes()
    with monkeypatch.context() as during:
        failure(during)
        reset = _fresh().put("/api/users/viewer-1/password", cookies=admin, json={"password": NEW_PASSWORDS[0]})
    assert reset.status_code == 200 and reset.json() == {"status": "complete", "message": "Password updated."}
    account = _account("viewer-1")
    assert main.verify_password(NEW_PASSWORDS[0], account["password_hash"]) and account["session_generation"] == 1
    main.SESSIONS_FILE.write_bytes(kept_copy)
    assert _signed_in_as(session) is None
    if failure is not _quarantine_fails:
        assert _signed_in_as(admin[main.SESSION_COOKIE_NAME]) == "admin-1"
    entries = _reset_entries()
    assert len(entries) == 1 and entries[0]["outcome"] == "success" and entries[0]["user_id"] == "admin-1"
    assert "sessions.json could not be cleaned up" in entries[0]["detail"]
    assert [kwargs["reason"] for event, kwargs in logged if event == "security.user_sessions_sign_out_incomplete"] == [
        "password_reset_by_administrator"]


def test_a_clean_reset_audit_entry_has_no_cleanup_note(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    main.create_session("viewer-1")
    assert _reset(client, "viewer-1", NEW_PASSWORDS[0], _cookies("admin-1"))["status"] == "complete"
    assert [entry["detail"] for entry in _reset_entries()] == ["Password reset by administrator."]


# ---------------------------------------------------------------- finding 4 a self-reset is audited as the administrator

def test_an_administrator_resetting_their_own_password_is_audited_as_themselves(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = _cookies("admin-1")
    assert _reset(client, "admin-1", NEW_PASSWORDS[0], admin) == {"status": "complete", "message": "Password updated."}
    entries = _reset_entries()
    assert len(entries) == 1
    assert (entries[0]["user_id"], entries[0]["user_name"], entries[0]["role"]) == ("admin-1", "Admin One", "administrator")
    assert entries[0]["resource"] == "user:admin-1" and entries[0]["outcome"] == "success"
    assert _signed_in_as(admin[main.SESSION_COOKIE_NAME]) is None  # their own earlier session is signed out too


def test_a_refused_reset_is_still_audited_as_the_requester(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("viewer-2")])
    assert _reset(client, "viewer-2", NEW_PASSWORDS[0], _cookies("viewer-1"))["status"] == "error"
    entries = _reset_entries()
    assert [(entry["user_id"], entry["outcome"]) for entry in entries] == [("viewer-1", "denied")]


# ---------------------------------------------------------------- finding 3 the uploaded archive's removal

@pytest.fixture()
def restore(client, monkeypatch, tmp_path):
    assert tmp_path in main.RESTORE_STAGING_FOLDER.parents  # redirected by isolate_recordings, never the shared root
    main.RESTORE_STAGING_FOLDER.mkdir(parents=True, exist_ok=True)
    rules = tmp_path / "alert_rules.json"
    monkeypatch.setattr(main, "CONFIG_BACKUP_FILES", [rules, main.USERS_FILE, main.USER_INVITES_FILE])
    main.save_users([dict(ADMIN)])
    main.save_user_invites([])
    cookies = _cookies("admin-1")
    before = main.USERS_FILE.read_bytes()

    def archive(members):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_STORED) as bundle:
            for name, content in members:
                bundle.writestr(name, content)
        return data.getvalue()

    def send(payload):
        return _fresh().post("/api/operations/restore", cookies=cookies,
                             files={"backup_file": ("restore.zip", payload, "application/zip")}).json()

    good = archive([("config/alert_rules.json", json.dumps({"rules": []})),
                    ("config/users.json", json.dumps([dict(ADMIN), _user("restored-1")])),
                    ("config/user_invites.json", "[]")])

    def uploaded():
        return sorted(path.name for path in main.RESTORE_STAGING_FOLDER.iterdir() if path.name.endswith("_restore.zip"))
    return {"send": send, "good": good, "archive": archive, "uploaded": uploaded, "before": before}


def _archive_unlink_refused(monkeypatch):
    real_unlink = Path.unlink

    def refuse(self, missing_ok=False):
        if self.name.endswith("_restore.zip") and self.exists():
            raise PermissionError("synthetic: archive in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", refuse)


def test_a_restore_removes_its_uploaded_archive(restore):
    result = restore["send"](restore["good"])
    assert result["status"] == "complete" and "archive_left_behind" not in result and restore["uploaded"]() == []


def test_an_archive_removal_failure_after_a_committed_restore_still_reports_it_restored(restore, monkeypatch, logged):
    _archive_unlink_refused(monkeypatch)
    result = restore["send"](restore["good"])
    assert result["status"] == "complete" and result["restored"] == ["alert_rules.json", "users.json", "user_invites.json"]
    assert {user["id"] for user in main.load_users()} == {"admin-1", "restored-1"}
    assert result["archive_left_behind"] == restore["uploaded"]() and len(restore["uploaded"]()) == 1
    assert "leftover" not in result  # the restore's own temporary files were all removed
    assert [kwargs["leftover"] for event, kwargs in logged if event == "configuration_restore.cleanup_incomplete"] == [
        restore["uploaded"]()]


def test_an_archive_removal_failure_after_a_failed_restore_keeps_the_restore_error(restore, monkeypatch):
    real_replace = Path.replace

    def fail_swap(self, target):
        if self.name.endswith(".restore") and Path(target).name == "users.json":
            raise OSError("synthetic disk failure during the swap")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", fail_swap)
    _archive_unlink_refused(monkeypatch)
    result = restore["send"](restore["good"])
    assert result["status"] == "error" and "synthetic disk failure during the swap" in result["message"]
    assert result["archive_left_behind"] == restore["uploaded"]() and main.USERS_FILE.read_bytes() == restore["before"]


def test_an_archive_removal_failure_after_a_refused_restore_keeps_the_refusal(restore, monkeypatch):
    _archive_unlink_refused(monkeypatch)
    refused = restore["send"](restore["archive"]([("config/users.json", "{not json")]))
    assert refused["status"] == "error" and refused["message"].startswith("Restore refused, nothing was changed:")
    not_zip = restore["send"](b"not a zip archive")
    assert not_zip == {"status": "error", "message": "Restore archive is not a valid ZIP file.",
                       "archive_left_behind": not_zip["archive_left_behind"]}
    assert sorted(refused["archive_left_behind"] + not_zip["archive_left_behind"]) == restore["uploaded"]()
    assert main.USERS_FILE.read_bytes() == restore["before"]


def test_an_unexpected_restore_error_still_removes_the_uploaded_archive(restore, monkeypatch):
    def broken(archive_path):
        raise RuntimeError("synthetic: unexpected restore failure")
    monkeypatch.setattr(main, "restore_configuration_backup", broken)
    response = _fresh().post("/api/operations/restore", cookies=_cookies("admin-1"),
                             files={"backup_file": ("restore.zip", restore["good"], "application/zip")})
    assert response.status_code == 500 and restore["uploaded"]() == []
