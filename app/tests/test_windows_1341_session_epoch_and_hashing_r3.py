"""Session epoch, session-file errors and password hashing outside the users.json lock (P3, owner 2026-11-06).

N1-A, before: a damaged sessions.json was moved aside intact; if it was repaired and put back, every unexpired session
in it signed people in again. Now each session records the epoch it was created in, a new epoch is written before a
damaged store is moved aside, and only sessions from the current epoch are accepted.

N2, before: _read_sessions_file() checked SESSIONS_FILE.exists() outside its error handling, and Path.exists() lets a
PermissionError escape as a bare OSError (a 500 for readers). Now every I/O error is SessionStoreUnreadable.

N3-A, before: user creation, the administrator password reset and invitation acceptance hashed the password (bcrypt,
about 0.25 s) while holding the users.json lock, which every request's authentication waits on. Now the hash is
computed first and the lock covers only the read-to-save.
"""
import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from database_backend import override_target
from partner_db import initialize_database

PASSWORD = "epoch-password-1"
NEW_PASSWORD = "brand-new-password-1"
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
    with override_target(sqlite_path=tmp_path / "epoch_hashing.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _fresh():
    return TestClient(main.app, raise_server_exceptions=False)


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"{user_id}@example.test", "role": "viewer",
                 "enabled": True, "site_ids": ["home"], "camera_ids": [], "password_hash": main.hash_password(PASSWORD),
                 "failed_login_attempts": 0, "locked_until": None}, **extra)


def _signed_in_as(token):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [
        (b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())]})
    user = main.authenticated_user(request)
    return user["id"] if user else None


def _devices_status(token):
    # A real route behind the authentication middleware: 200 only for a signed-in user.
    return _fresh().get("/api/mobile/devices", cookies={main.SESSION_COOKIE_NAME: token}, follow_redirects=False).status_code


def _sign_in(email):
    return _fresh().post("/login", data={"email": email, "password": PASSWORD, "next_url": "/"}, follow_redirects=False)


def _stored_sessions():
    return json.loads(main.SESSIONS_FILE.read_text(encoding="utf-8"))


def _epoch_file():
    return main.SESSIONS_FILE.with_name("sessions_epoch.json")


def _audit_reasons(action):
    from partner_db import connection
    with connection() as db:
        rows = db.execute("SELECT details_json FROM audit_logs WHERE action=?", (action,)).fetchall()
    return [json.loads(row["details_json"]).get("reason") for row in rows]


# ---------------------------------------------------------------- N1-A session epoch

def test_a_repaired_and_restored_session_file_cannot_reactivate_its_sessions(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("signer")])
    old = main.create_session("viewer-1")
    assert _signed_in_as(old) == "viewer-1" and _devices_status(old) == 200
    before_damage = main.SESSIONS_FILE.read_bytes()

    main.SESSIONS_FILE.write_bytes(before_damage[:-3])  # damaged: a half-written file
    assert _signed_in_as(old) is None
    signed_in = _sign_in("signer@example.test")
    assert signed_in.status_code == 303, signed_in.text[:300]
    new = signed_in.cookies[main.SESSION_COOKIE_NAME]
    assert _epoch_file().exists() and _audit_reasons("sessions.epoch_advanced") == ["damaged_sessions_json"]
    assert len(list(main.SESSIONS_FILE.parent.glob("sessions.json.unreadable-*"))) == 1

    # Someone repairs the kept copy and puts its sessions back beside the current ones.
    main.SESSIONS_FILE.write_text(json.dumps({**json.loads(before_damage), **_stored_sessions()}), encoding="utf-8")
    assert _signed_in_as(old) is None and _devices_status(old) != 200
    assert _signed_in_as(new) == "signer" and _devices_status(new) == 200
    # Replacing the whole file with the pre-damage copy does not bring the old session back either.
    main.SESSIONS_FILE.write_bytes(before_damage)
    assert _signed_in_as(old) is None


def test_sessions_from_before_any_quarantine_still_work_after_the_update(client):
    # No epoch file and sessions without an epoch (the store as earlier builds wrote it): nobody is signed out.
    main.save_users([dict(ADMIN), _user("viewer-1")])
    legacy = main.create_session("viewer-1")
    assert not _epoch_file().exists()
    assert all("epoch" not in session for session in _stored_sessions().values())
    assert _signed_in_as(legacy) == "viewer-1" and _devices_status(legacy) == 200


def test_a_damaged_epoch_file_signs_everyone_out_and_sign_in_works_again(client):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("signer")])
    _epoch_file().write_text(json.dumps({"epoch": "a" * 32}), encoding="utf-8")
    earlier = main.create_session("viewer-1")
    assert _stored_sessions()[main.session_serializer.loads(earlier)["session_id"]]["epoch"] == "a" * 32
    assert _signed_in_as(earlier) == "viewer-1"

    _epoch_file().write_bytes(b'{"epo')
    assert _signed_in_as(earlier) is None  # fail closed
    replaced = json.loads(_epoch_file().read_text(encoding="utf-8"))["epoch"]
    assert replaced and replaced != "a" * 32
    assert _audit_reasons("sessions.epoch_advanced") == ["damaged_epoch_file"]
    signed_in = _sign_in("signer@example.test")
    assert signed_in.status_code == 303
    assert _signed_in_as(signed_in.cookies[main.SESSION_COOKIE_NAME]) == "signer"


def test_an_unreadable_epoch_file_refuses_sessions_and_sign_ins_and_changes_nothing(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("signer")])
    _epoch_file().write_text(json.dumps({"epoch": "b" * 32}), encoding="utf-8")
    existing = main.create_session("viewer-1")
    sessions_before, epoch_before = main.SESSIONS_FILE.read_bytes(), _epoch_file().read_bytes()
    real_read = Path.read_bytes

    def locked_file(self):
        if self == _epoch_file():
            raise PermissionError(13, "synthetic sharing violation")
        return real_read(self)
    monkeypatch.setattr(Path, "read_bytes", locked_file)

    assert _signed_in_as(existing) is None and _devices_status(existing) != 200
    sign_in = _sign_in("signer@example.test")
    assert sign_in.status_code == 500 and main.SESSION_COOKIE_NAME not in sign_in.cookies
    monkeypatch.setattr(Path, "read_bytes", real_read)
    assert main.SESSIONS_FILE.read_bytes() == sessions_before and _epoch_file().read_bytes() == epoch_before
    assert _signed_in_as(existing) == "viewer-1"  # a passing I/O error signs nobody out for good


def test_an_epoch_that_cannot_be_written_leaves_the_damaged_store_in_place(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("signer")])
    damaged = b'{"half-written": '
    main.SESSIONS_FILE.write_bytes(damaged)
    real_write = main._write_json_atomically

    def failing_write(path, data):
        if path == _epoch_file():
            raise OSError(28, "synthetic disk full")
        return real_write(path, data)
    monkeypatch.setattr(main, "_write_json_atomically", failing_write)

    sign_in = _sign_in("signer@example.test")
    assert sign_in.status_code == 500 and main.SESSION_COOKIE_NAME not in sign_in.cookies
    assert main.SESSIONS_FILE.read_bytes() == damaged
    assert list(main.SESSIONS_FILE.parent.glob("sessions.json.unreadable-*")) == []
    assert not _epoch_file().exists()


# ---------------------------------------------------------------- N2 session-file existence and filesystem errors

def test_a_session_file_that_cannot_even_be_checked_is_unreadable_not_a_crash(client, monkeypatch):
    main.save_users([dict(ADMIN), _user("viewer-1"), _user("signer")])
    existing = main.create_session("viewer-1")
    before = main.SESSIONS_FILE.read_bytes()
    real_stat, real_read = Path.stat, Path.read_bytes

    def denied_stat(self, *args, **kwargs):
        if self == main.SESSIONS_FILE:
            raise PermissionError(13, "synthetic access denied")  # what Path.exists() would let escape
        return real_stat(self, *args, **kwargs)

    def denied_read(self):
        if self == main.SESSIONS_FILE:
            raise PermissionError(13, "synthetic access denied")
        return real_read(self)
    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(Path, "read_bytes", denied_read)

    with pytest.raises(main.SessionStoreUnreadable):
        main._read_sessions_file()
    assert main.load_sessions() == {}
    assert _signed_in_as(existing) is None
    assert _devices_status(existing) < 500  # signed out, never a server error
    sign_in = _sign_in("signer@example.test")
    assert sign_in.status_code == 500 and main.SESSION_COOKIE_NAME not in sign_in.cookies  # refused, nothing saved
    monkeypatch.setattr(Path, "stat", real_stat)
    monkeypatch.setattr(Path, "read_bytes", real_read)
    assert main.SESSIONS_FILE.read_bytes() == before
    assert list(main.SESSIONS_FILE.parent.glob("sessions.json.unreadable-*")) == []


def test_a_missing_session_file_still_reads_as_no_sessions(client):
    main.save_users([dict(ADMIN), _user("signer")])
    assert not main.SESSIONS_FILE.exists()
    assert main._read_sessions_file() == {} and main.load_sessions() == {}
    assert _sign_in("signer@example.test").status_code == 303


# ---------------------------------------------------------------- N3-A password hashing outside the users.json lock

@pytest.fixture()
def hash_spy(monkeypatch):
    held = []
    real_hash = main.hash_password

    def spy(password):
        held.append(main._USERS_FILE_LOCK._is_owned())
        return real_hash(password)
    monkeypatch.setattr(main, "hash_password", spy)
    return held


def _invite(invite_id="inv-1", email="invitee@example.test"):
    main.save_user_invites([{"id": invite_id, "email": email, "role": "viewer", "status": "pending", "all_cameras": True}])
    return main.session_serializer.dumps({"invite_id": invite_id, "email": email}, salt="anyaicam-user-invite")


def test_user_creation_hashes_outside_the_lock_and_the_account_signs_in(client, hash_spy):
    main.save_users([dict(ADMIN)])
    admin = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    response = client.post("/api/users", cookies=admin, json={"display_name": "Created One", "email": "Created@example.test",
                                                               "password": PASSWORD, "role": "viewer"})
    assert response.json()["status"] == "complete" and "password_hash" not in response.json()["user"]
    assert hash_spy == [False]
    assert _sign_in("created@example.test").status_code == 303
    duplicate = client.post("/api/users", cookies=admin, json={"display_name": "Again", "email": "created@example.test",
                                                                "password": PASSWORD, "role": "viewer"})
    assert duplicate.json() == {"status": "error", "message": "That email is already in use."}


def test_the_password_reset_hashes_outside_the_lock_and_clears_the_lockout(client, hash_spy):
    main.save_users([dict(ADMIN), _user("viewer-1", failed_login_attempts=4, locked_until="2999-01-01T00:00:00")])
    hash_spy.clear()  # _user() hashes its fixture password
    admin = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    response = client.put("/api/users/viewer-1/password", cookies=admin, json={"password": NEW_PASSWORD})
    assert response.json() == {"status": "complete", "message": "Password updated."}
    assert hash_spy == [False]
    viewer = next(user for user in main.load_users() if user["id"] == "viewer-1")
    assert main.verify_password(NEW_PASSWORD, viewer["password_hash"])
    assert viewer["failed_login_attempts"] == 0 and viewer["locked_until"] is None
    missing = client.put("/api/users/nobody/password", cookies=admin, json={"password": NEW_PASSWORD})
    assert missing.json() == {"status": "error", "message": "User not found."}


def test_invitation_acceptance_hashes_outside_the_lock_and_the_account_signs_in(client, hash_spy):
    main.save_users([dict(ADMIN)])
    token = _invite()
    response = client.post("/api/user-invites/accept", json={"token": token, "display_name": "Invitee", "password": PASSWORD})
    assert response.json() == {"status": "complete", "message": "Account created. You can now sign in."}
    assert hash_spy == [False]
    assert main.load_user_invites()[0]["status"] == "accepted"
    account = next(user for user in main.load_users() if user["email"] == "invitee@example.test")
    assert account["enabled"] is True and account["invitation_status"] == "active"
    assert _sign_in("invitee@example.test").status_code == 303
    replay = client.post("/api/user-invites/accept", json={"token": token, "display_name": "Invitee", "password": PASSWORD})
    assert replay.json() == {"status": "error", "message": "Invitation is no longer available."}


def test_refused_requests_never_reach_the_hash_or_the_file(client, hash_spy):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    hash_spy.clear()  # _user() hashes its fixture password
    before = main.USERS_FILE.read_bytes()
    viewer = {main.SESSION_COOKIE_NAME: main.create_session("viewer-1")}
    responses = [
        client.post("/api/users", cookies=viewer, json={"display_name": "X Y", "email": "x@example.test", "password": PASSWORD}),
        client.put("/api/users/admin-1/password", cookies=viewer, json={"password": NEW_PASSWORD}),
        client.post("/api/user-invites/accept", json={"token": "not-a-token", "display_name": "X Y", "password": PASSWORD}),
    ]
    assert all(response.status_code in (401, 403) or response.json().get("status") == "error" for response in responses)
    assert hash_spy == []
    assert main.USERS_FILE.read_bytes() == before


def test_concurrent_duplicate_creates_and_accepts_still_make_exactly_one_account(client):
    main.save_users([dict(ADMIN)])
    admin = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}
    token = _invite()

    def create(_):
        return _fresh().post("/api/users", cookies=admin, json={"display_name": "Same One", "email": "same@example.test",
                                                                 "password": PASSWORD, "role": "viewer"}).json()

    def accept(_):
        return _fresh().post("/api/user-invites/accept", json={"token": token, "display_name": "Invitee",
                                                                "password": PASSWORD}).json()
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(create, range(6)))
        accepted = list(pool.map(accept, range(5)))
    assert sorted(item["status"] for item in created) == ["complete"] + ["error"] * 5
    assert sorted(item["status"] for item in accepted) == ["complete"] + ["error"] * 4
    emails = [user["email"] for user in main.load_users()]
    assert emails.count("same@example.test") == 1 and emails.count("invitee@example.test") == 1


def test_the_three_routes_hash_outside_and_write_inside_the_users_lock():
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    routes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
              and node.name in {"create_user", "change_user_password", "accept_user_invite"}}
    assert set(routes) == {"create_user", "change_user_password", "accept_user_invite"}

    def calls(node, name):
        return [inner for inner in ast.walk(node) if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == name]
    for name, node in routes.items():
        assert not any(getattr(decorator, "id", None) == "_users_file_writer" for decorator in node.decorator_list), name
        locked = [block for block in ast.walk(node) if isinstance(block, ast.With)
                  and any(getattr(item.context_expr, "id", None) == "_USERS_FILE_LOCK" for item in block.items)]
        assert len(locked) == 1, name
        inside = {id(call) for call in ast.walk(locked[0]) if isinstance(call, ast.Call)}
        assert calls(node, "hash_password") and not [call for call in calls(node, "hash_password") if id(call) in inside], name
        assert all(id(call) in inside for call in calls(node, "load_users") + calls(node, "save_users")), name
