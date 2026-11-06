"""users.json concurrency on Windows 1.3.4.1 (Codex R1 revision review P1, owner 2026-11-06).

Before: only the sign-in strike helpers held a lock; registration, invitation acceptance, approval, user create/
update/delete and the administrator password reset each read users.json, changed their copy and saved it whole, and
every save went through one shared users.json.tmp. Two overlapping writers could save a stale list over each other's
change, or replace users.json from the other's half-written temporary file. Now every writer holds one users.json
lock from its read to its save, each save has a temporary file of its own, and the Windows service runs one worker
(installer/tests/test_windows_single_worker_1341.py). These tests drive the real routes concurrently, with the read
slowed down so an unserialized read-modify-write would lose changes.
"""
import ast
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database

MANAGER = {"id": "admin-1", "display_name": "Admin One", "email": "admin@example.test", "role": "administrator",
           "enabled": True, "super_admin": True, "site_ids": ["home"], "camera_ids": []}  # approves requests
NEW_PASSWORD = "brand-new-password-1"


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
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "USER_INVITES_FILE", tmp_path / "user_invites.json")
    monkeypatch.setattr(main, "MAX_LOGIN_ATTEMPTS", 1000)
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    with override_target(sqlite_path=tmp_path / "users_concurrency.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


@pytest.fixture()
def slow_reads(monkeypatch):
    """Every users.json read pauses before returning, widening the window between a writer's read and its save."""
    real_load = main.load_users

    def slow_load():
        users = real_load()
        time.sleep(0.01)
        return users
    monkeypatch.setattr(main, "load_users", slow_load)
    return real_load


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"{user_id}@example.test", "role": "viewer",
                 "enabled": True, "site_ids": ["home"], "camera_ids": [], "password_hash": main.hash_password("old-password-1"),
                 "failed_login_attempts": 0, "locked_until": None}, **extra)


def _by_id(users):
    return {user["id"]: user for user in users}


def test_every_writer_route_running_at_once_loses_no_change(client, slow_reads):
    edits = [f"edit-{n}" for n in range(6)]
    resets = [f"reset-{n}" for n in range(3)]
    deletes = [f"delete-{n}" for n in range(3)]
    pending = [f"pending-{n}" for n in range(3)]
    strikes = "striker"
    main.save_users([dict(MANAGER)] + [_user(uid) for uid in edits + resets + deletes + [strikes]]
                    + [_user(uid, enabled=False, invitation_status="pending") for uid in pending])
    main.save_user_invites([{"id": f"inv-{n}", "email": f"invitee-{n}@example.test", "role": "viewer",
                             "status": "pending", "all_cameras": True} for n in range(3)])
    cookies = {main.SESSION_COOKIE_NAME: main.create_session("admin-1")}

    def invite_token(n):
        return main.session_serializer.dumps({"invite_id": f"inv-{n}"}, salt="anyaicam-user-invite")

    calls = (
        [lambda n=n: client.post("/api/users", cookies=cookies, json={
            "display_name": f"Created {n}", "email": f"created-{n}@example.test", "password": "created-password-1",
            "role": "viewer"}) for n in range(4)]
        + [lambda uid=uid: client.put(f"/api/users/{uid}", cookies=cookies, json={"display_name": f"Renamed {uid}"})
           for uid in edits]
        + [lambda uid=uid: client.put(f"/api/users/{uid}/password", cookies=cookies, json={"password": NEW_PASSWORD})
           for uid in resets]
        + [lambda uid=uid: client.delete(f"/api/users/{uid}", cookies=cookies) for uid in deletes]
        + [lambda uid=uid: client.post(f"/api/business-users/{uid}/approve", cookies=cookies) for uid in pending]
        + [lambda n=n: client.post("/api/user-invites/accept", json={
            "token": invite_token(n), "display_name": f"Invitee {n}", "password": "invitee-password-1"})
           for n in range(3)]
        + [lambda n=n: client.post("/register", data={
            "display_name": f"Registrant {n}", "email": f"registrant-{n}@example.test",
            "password": "registrant-password-1", "requested_role": "customer_owner"},
            follow_redirects=False) for n in range(3)]
        + [lambda: client.post("/login", data={"email": f"{strikes}@example.test", "password": "wrong-password-1",
                                               "next_url": "/"}, follow_redirects=False) for _ in range(8)]
    )
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda call: call(), calls))
    assert all(response.status_code < 500 for response in responses), [r.text[:200] for r in responses if r.status_code >= 500]

    users = _by_id(main.load_users())
    emails = {user["email"] for user in users.values()}
    assert {f"created-{n}@example.test" for n in range(4)} <= emails
    assert all(users[uid]["display_name"] == f"Renamed {uid}" for uid in edits)
    assert all(main.verify_password(NEW_PASSWORD, users[uid]["password_hash"]) for uid in resets)
    assert not set(deletes) & set(users)
    assert all(users[uid]["enabled"] is True and users[uid]["invitation_status"] == "active" for uid in pending)
    assert {f"invitee-{n}@example.test" for n in range(3)} <= emails
    assert {f"registrant-{n}@example.test" for n in range(3)} <= emails
    assert users[strikes]["failed_login_attempts"] == 8
    assert users["admin-1"]["role"] == "administrator" and users["admin-1"]["enabled"] is True
    assert sorted(path.name for path in main.USERS_FILE.parent.glob("users.json*.tmp")) == []


def test_concurrent_saves_never_share_a_temporary_file(client, monkeypatch):
    written = []
    real_write = Path.write_text

    def spy(self, *args, **kwargs):
        if self.name.startswith(main.USERS_FILE.name):
            written.append(self.name)
        return real_write(self, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", spy)
    lists = [[_user(f"u-{n}")] for n in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(main.save_users, lists))
    assert len(written) == 16 and len(set(written)) == 16
    assert all(name != "users.json.tmp" and name.endswith(".tmp") for name in written)
    stored = json.loads(main.USERS_FILE.read_text(encoding="utf-8"))
    assert stored in lists  # one complete write, never a mixture
    assert list(main.USERS_FILE.parent.glob("users.json*.tmp")) == []


def test_a_failed_save_leaves_users_json_and_no_temporary_file(client, monkeypatch):
    main.save_users([_user("kept")])
    before = main.USERS_FILE.read_bytes()

    def refuse(self, target):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError):
        main.save_users([_user("lost")])
    assert main.USERS_FILE.read_bytes() == before
    assert list(main.USERS_FILE.parent.glob("users.json*.tmp")) == []


def test_every_users_json_writer_in_main_holds_the_shared_lock():
    # Static guard for future writers: any function that calls save_users() must be a @_users_file_writer, or make
    # each of its load_users()/save_users() calls inside a `with _USERS_FILE_LOCK:` (or _LEGACY_LOCK_STATE) block.
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    locks = {"_USERS_FILE_LOCK", "_LEGACY_LOCK_STATE"}

    def calls(node, name):
        return [inner for inner in ast.walk(node) if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == name]

    unguarded = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name == "save_users":
            continue
        if not calls(node, "save_users"):
            continue
        if any(getattr(decorator, "id", None) == "_users_file_writer" for decorator in node.decorator_list):
            continue
        locked = set()
        for block in ast.walk(node):
            if isinstance(block, ast.With) and any(getattr(item.context_expr, "id", None) in locks for item in block.items):
                locked.update(id(call) for call in calls(block, "save_users") + calls(block, "load_users"))
        if any(id(call) not in locked for call in calls(node, "save_users") + calls(node, "load_users")):
            unguarded.append(node.name)
    assert unguarded == []
    assert main._LEGACY_LOCK_STATE is main._USERS_FILE_LOCK


def test_registration_hashes_the_password_outside_the_users_lock(client, monkeypatch):
    # The public /register route must never hold the shared users.json lock for a bcrypt hash.
    held = []
    real_hash = main.hash_password

    def spy(password):
        held.append(main._USERS_FILE_LOCK._is_owned())
        return real_hash(password)
    monkeypatch.setattr(main, "hash_password", spy)
    response = client.post("/register", data={"display_name": "Registrant", "email": "registrant@example.test",
                                              "password": "registrant-password-1", "requested_role": "customer_owner"},
                           follow_redirects=False)
    assert response.status_code == 200, response.text[:300]
    assert held == [False]
    assert any(user["email"] == "registrant@example.test" for user in main.load_users())


def test_the_writer_routes_keep_their_authorization(client):
    # The lock wraps the routes; it never replaces their own permission checks.
    main.save_users([dict(MANAGER), _user("viewer-1")])
    viewer = {main.SESSION_COOKIE_NAME: main.create_session("viewer-1")}
    before = main.USERS_FILE.read_bytes()
    responses = [
        client.post("/api/users", cookies=viewer, json={"display_name": "X Y", "email": "x@example.test",
                                                        "password": "created-password-1"}),
        client.put("/api/users/admin-1", cookies=viewer, json={"enabled": False}),
        client.put("/api/users/admin-1/password", cookies=viewer, json={"password": NEW_PASSWORD}),
        client.delete("/api/users/admin-1", cookies=viewer),
    ]
    assert all(response.status_code in (401, 403) or response.json().get("status") == "error" for response in responses)
    assert main.USERS_FILE.read_bytes() == before
