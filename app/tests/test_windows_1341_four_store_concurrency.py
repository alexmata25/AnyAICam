"""Four security stores on Windows 1.3.4.1: concurrency and failure injection (owner-approved remediation, 2026-11-06).

Before: sessions.json, mobile_devices.json, mobile_pairing_codes.json and user_invites.json were each read, changed and
saved whole with no lock, through one fixed <store>.json.tmp. A sign-in that overlapped a sign-out could save the
removed session back; a heartbeat that overlapped a revoke could save the device back as active; two claims of one
single-use pairing code could both pair a device; an invite cancel that overlapped another invite change could be lost.
And an unreadable sessions.json read as empty, so the next sign-in saved over it.

Now each store's writers hold a lock from read to save (sessions and mobile have their own; invitations share the
users.json lock), each save has its own temporary file, and a sessions.json writer never saves over a store it could
not read. These tests drive the real routes concurrently, with reads slowed so an unserialized read-modify-write
would lose a change, and inject read and write failures.
"""
import ast
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "store-password-1"
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
                            ("USER_INVITES_FILE", "user_invites.json"), ("MOBILE_DEVICES_FILE", "mobile_devices.json"),
                            ("MOBILE_PAIRING_CODES_FILE", "mobile_pairing_codes.json")):
        monkeypatch.setattr(main, name, tmp_path / file_name)
    monkeypatch.setattr(main, "MAX_LOGIN_ATTEMPTS", 1000)
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    with override_target(sqlite_path=tmp_path / "four_store.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _fresh():
    # One client per concurrent call: a shared client's cookie jar would carry one sign-in's cookie into another call.
    return TestClient(main.app, raise_server_exceptions=False)


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"{user_id}@example.test", "role": "viewer",
                 "enabled": True, "site_ids": ["home"], "camera_ids": [], "password_hash": main.hash_password(PASSWORD),
                 "failed_login_attempts": 0, "locked_until": None}, **extra)


def _session_id(token):
    return main.session_serializer.loads(token)["session_id"]


def _stored_sessions():
    return json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))


@pytest.fixture()
def slow_reads(monkeypatch):
    """Every read of the four stores pauses before returning, widening the window between a writer's read and save."""
    real_sessions = main._read_sessions_file
    real_json = main.load_json_file
    real_invites = main.load_user_invites

    def slow_sessions():
        data = real_sessions()
        time.sleep(0.01)
        return data

    def slow_json(path, default):
        data = real_json(path, default)
        if path in (main.MOBILE_DEVICES_FILE, main.MOBILE_PAIRING_CODES_FILE):
            time.sleep(0.01)
        return data

    def slow_invites():
        data = real_invites()
        time.sleep(0.01)
        return data
    monkeypatch.setattr(main, "_read_sessions_file", slow_sessions)
    monkeypatch.setattr(main, "load_json_file", slow_json)
    monkeypatch.setattr(main, "load_user_invites", slow_invites)


# ---------------------------------------------------------------- sessions.json

def test_a_sign_out_is_never_undone_by_concurrent_sign_ins(client, slow_reads):
    leavers = [f"leaver-{n}" for n in range(6)]
    joiners = [f"joiner-{n}" for n in range(6)]
    main.save_users([dict(ADMIN)] + [_user(uid) for uid in leavers + joiners])
    leaving_tokens = [main.create_session(uid) for uid in leavers]
    kept_token = main.create_session("admin-1")

    calls = (
        [lambda token=token: _fresh().post("/logout", cookies={main.SESSION_COOKIE_NAME: token}, follow_redirects=False)
         for token in leaving_tokens]
        + [lambda uid=uid: _fresh().post("/login", data={"email": f"{uid}@example.test", "password": PASSWORD, "next_url": "/"},
                                         follow_redirects=False) for uid in joiners]
    )
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda call: call(), calls))
    assert all(response.status_code == 303 for response in responses), [(r.status_code, r.text[:200]) for r in responses]

    stored = _stored_sessions()
    assert not {_session_id(token) for token in leaving_tokens} & set(stored)  # every sign-out held
    joined = [response.cookies.get(main.SESSION_COOKIE_NAME) for response in responses[len(leaving_tokens):]]
    assert all(joined) and {_session_id(token) for token in joined} <= set(stored)  # and every sign-in kept
    assert _session_id(kept_token) in stored
    for token in leaving_tokens:  # a signed-out cookie no longer opens the VMS
        assert _fresh().get("/api/users", cookies={main.SESSION_COOKIE_NAME: token}, follow_redirects=False).status_code in (303, 401, 403)
    assert list(main.SESSIONS_FILE.parent.glob("sessions.json*.tmp")) == []


def test_a_damaged_session_store_is_kept_and_audited_never_silently_overwritten(client):
    main.save_users([dict(ADMIN)])
    damaged = b'{"half-written": '
    main.SESSIONS_FILE.write_bytes(damaged)
    # A damaged store signs nobody in.
    assert main.load_sessions() == {}

    response = _fresh().post("/login", data={"email": "admin@example.test", "password": PASSWORD, "next_url": "/"},
                             follow_redirects=False)
    assert response.status_code == 303, response.text[:300]
    kept = list(main.SESSIONS_FILE.parent.glob("sessions.json.unreadable-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == damaged  # moved aside intact, not overwritten
    assert set(_stored_sessions()) == {_session_id(response.cookies[main.SESSION_COOKIE_NAME])}
    from partner_db import connection
    with connection() as db:
        rows = db.execute("SELECT details_json FROM audit_logs WHERE action='sessions.store_unreadable'").fetchall()
    assert len(rows) == 1 and kept[0].name in rows[0]["details_json"]


def test_an_unreadable_session_store_refuses_the_write_and_is_left_unchanged(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    existing = main.create_session("viewer-1")
    before = main.SESSIONS_FILE.read_bytes()
    real_read = Path.read_bytes

    def locked_file(self):
        if self == main.SESSIONS_FILE:
            raise PermissionError(13, "synthetic sharing violation")
        return real_read(self)
    monkeypatch.setattr(Path, "read_bytes", locked_file)

    with pytest.raises(main.SessionStoreUnreadable):
        main.create_session("admin-1")
    sign_in = _fresh().post("/login", data={"email": "admin@example.test", "password": PASSWORD, "next_url": "/"},
                            follow_redirects=False)
    sign_out = _fresh().post("/logout", cookies={main.SESSION_COOKIE_NAME: existing}, follow_redirects=False)
    assert sign_in.status_code == 500 and main.SESSION_COOKIE_NAME not in sign_in.cookies
    assert sign_out.status_code == 500  # a sign-out that could not be recorded is never reported as done
    monkeypatch.setattr(Path, "read_bytes", real_read)
    assert main.SESSIONS_FILE.read_bytes() == before
    assert list(main.SESSIONS_FILE.parent.glob("sessions.json.unreadable-*")) == []
    assert _session_id(existing) in _stored_sessions()


# ------------------------------------------------- mobile_devices.json, mobile_pairing_codes.json

def _pair(client, cookies, code, name="Phone"):
    return client.post("/api/mobile/pairing/claim", cookies=cookies, json={"code": code, "device_name": name})


def test_one_pairing_code_claimed_at_once_pairs_exactly_one_device(client, slow_reads):
    main.save_users([dict(ADMIN)])
    cookies = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    code = main.create_mobile_pairing_code(dict(ADMIN), "Phone")["code"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda n: _pair(_fresh(), cookies, code, f"Phone {n}"), range(8)))
    assert sorted(response.status_code for response in responses) == [200] + [400] * 7
    assert len(main.load_mobile_devices()) == 1
    assert code not in main.cleanup_mobile_pairing_codes(main.load_mobile_pairing_codes())


def test_a_failed_device_save_leaves_the_pairing_code_spent(client, monkeypatch):
    main.save_users([dict(ADMIN)])
    cookies = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    code = main.create_mobile_pairing_code(dict(ADMIN), "Phone")["code"]
    real_save = main.save_mobile_devices

    def refuse(devices):
        raise OSError("synthetic device store failure")
    monkeypatch.setattr(main, "save_mobile_devices", refuse)
    assert _pair(_fresh(), cookies, code).status_code == 500
    monkeypatch.setattr(main, "save_mobile_devices", real_save)
    assert _pair(_fresh(), cookies, code).status_code == 400  # never reusable
    assert main.load_mobile_devices() == {}


def test_a_revoke_is_never_undone_by_concurrent_heartbeats(client, slow_reads):
    main.save_users([dict(ADMIN)])
    cookies = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    devices = []
    for n in range(4):
        code = main.create_mobile_pairing_code(dict(ADMIN), f"Phone {n}")["code"]
        devices.append(_pair(client, cookies, code).json()["device"]["id"])
    revoked, kept = devices[:2], devices[2:]

    calls = ([lambda device=device: _fresh().put(f"/api/mobile/devices/{device}", cookies=cookies, json={"revoked": True})
              for device in revoked]
             + [lambda device=device: _fresh().post(f"/api/mobile/devices/{device}/heartbeat", cookies=cookies)
                for device in devices for _ in range(5)])
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda call: call(), calls))
    assert all(response.status_code in (200, 404) for response in responses), [r.status_code for r in responses]

    stored = main.load_mobile_devices()
    assert all(stored[device]["revoked"] is True for device in revoked)
    assert all(stored[device]["revoked"] is False for device in kept)
    for device in revoked:
        assert client.post(f"/api/mobile/devices/{device}/heartbeat", cookies=cookies).status_code == 404
    assert list(main.MOBILE_DEVICES_FILE.parent.glob("mobile_*.json*.tmp")) == []


def test_device_management_keeps_its_owner_check(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    viewer = {main.SESSION_COOKIE_NAME: main.create_session("viewer-1")}
    code = main.create_mobile_pairing_code(dict(ADMIN), "Admin phone")["code"]
    device = _pair(client, admin, code).json()["device"]["id"]
    before = main.MOBILE_DEVICES_FILE.read_bytes()
    assert client.put(f"/api/mobile/devices/{device}", cookies=viewer, json={"revoked": True}).status_code == 403
    assert main.MOBILE_DEVICES_FILE.read_bytes() == before


# ---------------------------------------------------------------- user_invites.json

@pytest.fixture()
def quiet_email(monkeypatch):
    held = []

    def send(invite, request=None):
        held.append(main._USERS_FILE_LOCK._is_owned())
        return True, "Invitation email sent."
    monkeypatch.setattr(main, "send_user_invitation_email", send)
    return held


def test_an_invite_cancel_is_never_lost_to_concurrent_invite_changes(client, slow_reads, quiet_email):
    main.save_users([dict(ADMIN)])
    cookies = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    for n in range(8):
        assert client.post("/api/user-invites", cookies=cookies, json={"email": f"existing-{n}@example.test"}).json()["status"] == "complete"
    invites = {invite["email"]: invite for invite in main.load_user_invites()}
    cancelled = [invites[f"existing-{n}@example.test"] for n in range(4)]
    resent = [invites[f"existing-{n}@example.test"] for n in range(4, 8)]

    calls = ([lambda invite=invite: _fresh().delete(f"/api/user-invites/{invite['id']}", cookies=cookies) for invite in cancelled]
             + [lambda invite=invite: _fresh().post(f"/api/user-invites/{invite['id']}/resend", cookies=cookies) for invite in resent]
             + [lambda n=n: _fresh().post("/api/user-invites", cookies=cookies, json={"email": f"new-{n}@example.test"})
                for n in range(6)]
             + [lambda: _fresh().get("/api/user-invites", cookies=cookies) for _ in range(4)])
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda call: call(), calls))
    assert all(response.status_code == 200 and response.json()["status"] == "complete" for response in responses), \
        [r.text[:200] for r in responses]

    stored = {invite["email"]: invite for invite in main.load_user_invites()}
    assert all(stored[invite["email"]]["status"] == "cancelled" for invite in cancelled)
    assert all(stored[invite["email"]]["status"] == "pending" for invite in resent)
    assert {f"new-{n}@example.test" for n in range(6)} <= set(stored)
    for invite in cancelled:  # a cancelled invitation can no longer be accepted
        accepted = _fresh().post("/api/user-invites/accept", json={"token": invite["token"], "display_name": "Late Invitee",
                                                                    "password": "invitee-password-1"})
        assert accepted.json() == {"status": "error", "message": "Invitation is no longer available."}
    assert not any(user["email"].startswith("existing-") for user in main.load_users())
    assert quiet_email and not any(quiet_email)  # every invitation email was sent after the lock was released
    assert list(main.USER_INVITES_FILE.parent.glob("user_invites.json*.tmp")) == []


def test_invitation_routes_keep_their_authorization(client, quiet_email):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    viewer = {main.SESSION_COOKIE_NAME: main.create_session("viewer-1")}
    invite_id = client.post("/api/user-invites", cookies=admin, json={"email": "target@example.test"}).json()["invite"]["id"]
    before = main.USER_INVITES_FILE.read_bytes()
    responses = [client.post("/api/user-invites", cookies=viewer, json={"email": "other@example.test"}),
                 client.post(f"/api/user-invites/{invite_id}/resend", cookies=viewer),
                 client.delete(f"/api/user-invites/{invite_id}", cookies=viewer)]
    assert all(response.json() == {"status": "error", "message": "Administrator permission required."} for response in responses)
    assert main.USER_INVITES_FILE.read_bytes() == before


def test_the_configuration_restore_holds_the_users_lock(client, monkeypatch, tmp_path):
    assert tmp_path in main.RESTORE_STAGING_FOLDER.parents  # redirected by isolate_recordings, never the shared root
    main.RESTORE_STAGING_FOLDER.mkdir(parents=True, exist_ok=True)
    main.save_users([dict(ADMIN)])
    cookies = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    restored_users = [dict(ADMIN), _user("restored-1")]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("config/users.json", json.dumps(restored_users))
        bundle.writestr("config/user_invites.json", json.dumps([]))
    held = {}
    real_replace = Path.replace

    def spy(self, target):
        if self.name.endswith(".restore"):
            held[Path(target).name] = main._USERS_FILE_LOCK._is_owned()
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", spy)
    response = client.post("/api/operations/restore", cookies=cookies,
                           files={"backup_file": ("restore.zip", archive.getvalue(), "application/zip")})
    assert response.json()["status"] == "complete", response.text[:300]
    assert held == {"users.json": True, "user_invites.json": True}
    assert {user["id"] for user in main.load_users()} == {"admin-1", "restored-1"}


# ---------------------------------------------------------------- all four stores

STORES = [("save_sessions", "SESSIONS_FILE", {"s": {"user_id": "u"}}),
          ("save_user_invites", "USER_INVITES_FILE", [{"id": "i"}]),
          ("save_mobile_devices", "MOBILE_DEVICES_FILE", {"d": {"revoked": False}}),
          ("save_mobile_pairing_codes", "MOBILE_PAIRING_CODES_FILE", {"123456": {"claimed": False}})]


@pytest.mark.parametrize("save, attribute, sample", STORES)
def test_concurrent_saves_never_share_a_temporary_file(client, monkeypatch, save, attribute, sample):
    path = getattr(main, attribute)
    written = []
    real_write = Path.write_text

    def spy(self, *args, **kwargs):
        if self.name.startswith(path.name) and self != path:
            written.append(self.name)
        return real_write(self, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", spy)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: getattr(main, save)(sample), range(16)))
    assert len(written) == 16 and len(set(written)) == 16
    assert f"{path.stem}.json.tmp" not in written
    assert json.loads(path.read_text(encoding="utf-8")) == sample
    assert list(path.parent.glob(f"{path.name}*.tmp")) == []


@pytest.mark.parametrize("save, attribute, sample", STORES)
def test_a_failed_save_leaves_the_store_and_no_temporary_file(client, monkeypatch, save, attribute, sample):
    path = getattr(main, attribute)
    getattr(main, save)(sample)
    before = path.read_bytes()

    def refuse(self, target):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError):
        getattr(main, save)({} if isinstance(sample, dict) else [])
    assert path.read_bytes() == before
    assert list(path.parent.glob(f"{path.name}*.tmp")) == []


def _main_tree():
    return ast.parse(Path(main.__file__).read_text(encoding="utf-8"))


def _calls(node, names):
    return [inner for inner in ast.walk(node) if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) in names]


def _with_blocks(node, lock):
    return [block for block in ast.walk(node) if isinstance(block, ast.With)
            and any(getattr(item.context_expr, "id", None) == lock for item in block.items)]


@pytest.mark.parametrize("lock, writes, reads, decorator", [
    ("_SESSIONS_FILE_LOCK", {"save_sessions"}, {"_load_sessions_for_update", "load_sessions"}, None),
    ("_MOBILE_STORE_LOCK", {"save_mobile_devices", "save_mobile_pairing_codes"}, {"load_mobile_devices", "load_mobile_pairing_codes"}, None),
    ("_USERS_FILE_LOCK", {"save_user_invites"}, {"load_user_invites"}, "_users_file_writer"),
])
def test_every_writer_holds_its_store_lock_from_read_to_save(lock, writes, reads, decorator):
    # Static guard for future writers: a function that saves one of these stores makes each of its reads and saves of
    # that store inside a `with <lock>:` block (or, for invitations, is a @_users_file_writer).
    unguarded = []
    for node in ast.walk(_main_tree()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name in writes or not _calls(node, writes):
            continue
        if decorator and any(getattr(item, "id", None) == decorator for item in node.decorator_list):
            continue
        guarded = {id(call) for block in _with_blocks(node, lock) for call in _calls(block, writes | reads)}
        if any(id(call) not in guarded for call in _calls(node, writes | reads)):
            unguarded.append(node.name)
    assert unguarded == []


def test_the_session_and_mobile_locks_never_take_another_store_lock():
    # Lock order: _USERS_FILE_LOCK -> _SESSIONS_FILE_LOCK or _MOBILE_STORE_LOCK, never the reverse. Inside the two
    # leaf locks nothing may take the users lock (directly or through an identity lookup) or the other leaf lock.
    forbidden = {"load_users", "save_users", "current_user", "authenticated_user", "_mobile_devices_identity",
                 "load_user_invites", "save_user_invites"}
    leaves = {"_SESSIONS_FILE_LOCK": forbidden | {"load_mobile_devices", "save_mobile_devices",
                                                  "load_mobile_pairing_codes", "save_mobile_pairing_codes"},
              "_MOBILE_STORE_LOCK": forbidden | {"load_sessions", "save_sessions", "create_session", "destroy_session"}}
    offending = []
    for node in ast.walk(_main_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for lock, names in leaves.items():
                for block in _with_blocks(node, lock):
                    offending += [(node.name, call.func.id) for call in _calls(block, names)]
    assert offending == []
    # The one function that reaches a sessions.json writer's lock with more than plain file work under it is the
    # damaged-store path, and that only adds a structured log line and an audit_logs row (SQL, no store lock).
    helper = next(node for node in ast.walk(_main_tree()) if isinstance(node, ast.FunctionDef) and node.name == "_load_sessions_for_update")
    assert {call.func.id for call in _calls(helper, {"structured_log", "auth_audit", "_read_sessions_file"})} == \
        {"structured_log", "auth_audit", "_read_sessions_file"}
    assert not _calls(helper, forbidden)
