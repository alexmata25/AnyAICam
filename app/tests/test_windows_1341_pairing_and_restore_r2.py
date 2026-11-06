"""Mobile pairing abuse cases and an all-or-nothing configuration restore (Codex four-store review FIX REQUIRED, owner
2026-11-06).

Pairing, before: any signed-in account could claim any live six-digit code, with no limit on attempts and no cap on
live codes, so a low-privilege account could guess another account's code and pair a device to it. Now a code pairs
only for the account that created it (another account gets the same answer as a wrong code), failed claims are limited
per account (5) and per client address (20) in a 10-minute window, each account has one live code (a new one
supersedes the old), and the appliance at most MOBILE_PAIRING_MAX_ACTIVE_CODES.

Restore, before: each file in the archive replaced its target in turn with no validation, so a malformed member was
written straight over a live file, and a failure part-way left a partial restore. Now the whole archive is validated
first, and the swap rolls back on a failure.
"""
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database

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
    with override_target(sqlite_path=tmp_path / "pairing_restore.db"):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _fresh():
    return TestClient(main.app, raise_server_exceptions=False)


def _user(user_id, **extra):
    return dict({"id": user_id, "display_name": f"User {user_id}", "email": f"{user_id}@example.test", "role": "viewer",
                 "enabled": True, "site_ids": ["home"], "camera_ids": []}, **extra)


def _cookies(user_id):
    return {main.SESSION_COOKIE_NAME: main.create_session(user_id)}


def _new_code(client, cookies, name="Phone"):
    response = client.post("/api/mobile/pairing", cookies=cookies, json={"device_name": name})
    assert response.status_code == 200, response.text[:300]
    return response.json()["pairing"]["code"]


def _claim(client, cookies, code, name="Phone"):
    return client.post("/api/mobile/pairing/claim", cookies=cookies, json={"code": code, "device_name": name})


def _wrong(code):
    return f"{(int(code) + 1) % 1000000:06d}"


def _audit_actions(action):
    from partner_db import connection
    with connection() as db:
        return db.execute("SELECT entity_id FROM audit_logs WHERE action=?", (action,)).fetchall()


# ------------------------------------------------------------------- pairing ownership

def test_a_code_pairs_only_for_the_account_that_created_it(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin, viewer = _cookies("admin-1"), _cookies("viewer-1")
    code = _new_code(client, admin)

    refused = _claim(client, viewer, code, "Intruder phone")
    assert refused.status_code == 400 and refused.json()["detail"] == "Pairing code is invalid or expired."  # no hint
    assert main.load_mobile_devices() == {}
    assert len(_audit_actions("mobile_pairing.claim_refused")) == 1
    # The owner's code is untouched by the refused attempt.
    paired = _claim(client, admin, code, "Admin phone")
    assert paired.status_code == 200 and paired.json()["device"]["user_id"] == "admin-1"


def test_a_claimed_code_cannot_be_replayed(client):
    main.save_users([dict(ADMIN)])
    admin = _cookies("admin-1")
    code = _new_code(client, admin)
    assert _claim(client, admin, code).status_code == 200
    assert _claim(client, admin, code).status_code == 400
    assert len(main.load_mobile_devices()) == 1


# ------------------------------------------------------------------- claim-attempt limits

def test_failed_claims_hit_the_account_limit_and_even_a_correct_code_waits(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin, viewer = _cookies("admin-1"), _cookies("viewer-1")
    admin_code = _new_code(client, admin)
    own_code = _new_code(client, viewer)
    guesses = [_claim(client, viewer, _wrong(admin_code) if n % 2 else admin_code) for n in range(main.PAIRING_CLAIM_MAX_FAILURES)]
    assert [response.status_code for response in guesses] == [400] * main.PAIRING_CLAIM_MAX_FAILURES

    limited = _claim(client, viewer, own_code)
    assert limited.status_code == 429
    assert len(_audit_actions("mobile_pairing.claim_limited")) == 1
    assert main.load_mobile_devices() == {}
    assert _claim(client, admin, admin_code).status_code == 200  # another account is not limited by this one

    for key in list(main._pairing_claim_failures):  # the window passes
        main._pairing_claim_failures[key] = [moment - main.PAIRING_CLAIM_WINDOW_SECONDS for moment in main._pairing_claim_failures[key]]
    assert _claim(client, viewer, own_code).status_code == 200


def test_concurrent_guesses_never_get_past_the_limit(client):
    main.save_users([dict(ADMIN), _user("viewer-1")])
    admin, viewer = _cookies("admin-1"), _cookies("viewer-1")
    target = _new_code(client, admin)
    candidates = [f"{n:06d}" for n in range(30) if f"{n:06d}" != target][:20]
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda code: _claim(_fresh(), viewer, code), candidates))
    assert sorted(response.status_code for response in responses) == [400] * 5 + [429] * 15
    assert main.load_mobile_devices() == {}


def test_failed_claims_are_also_limited_per_client_address(client):
    accounts = [f"viewer-{n}" for n in range(5)]
    main.save_users([dict(ADMIN)] + [_user(uid) for uid in accounts])
    admin_code = _new_code(client, _cookies("admin-1"))
    per_account = main.PAIRING_CLAIM_MAX_FAILURES
    assert per_account * 4 == main.PAIRING_CLAIM_MAX_FAILURES_PER_ADDRESS
    for uid in accounts[:4]:
        cookies = _cookies(uid)
        assert [_claim(client, cookies, _wrong(admin_code)).status_code for _ in range(per_account)] == [400] * per_account
    fifth = _cookies(accounts[4])
    assert _claim(client, fifth, _wrong(admin_code)).status_code == 429  # its first attempt, from the same address


def test_a_rotated_forwarded_for_header_does_not_reset_the_address_limit(client):
    # The per-address count is the connection's verified address (request.client.host), never the raw
    # X-Forwarded-For header, which any client can set to a new value on every request.
    accounts = [f"viewer-{n}" for n in range(5)]
    main.save_users([dict(ADMIN)] + [_user(uid) for uid in accounts])
    admin_code = _new_code(client, _cookies("admin-1"))
    per_account = main.PAIRING_CLAIM_MAX_FAILURES
    spoofed = iter(f"203.0.113.{n}" for n in range(1, 200))

    def claim(cookies):
        return client.post("/api/mobile/pairing/claim", cookies=cookies, headers={"X-Forwarded-For": next(spoofed)},
                           json={"code": _wrong(admin_code), "device_name": "Phone"})
    for uid in accounts[:4]:
        cookies = _cookies(uid)
        assert [claim(cookies).status_code for _ in range(per_account)] == [400] * per_account
    assert not [key for key in main._pairing_claim_failures if key.startswith("address:203.0.113.")]
    assert claim(_cookies(accounts[4])).status_code == 429


# ------------------------------------------------------------------- live-code cap and superseding

def test_a_new_code_supersedes_the_accounts_earlier_code(client):
    main.save_users([dict(ADMIN)])
    admin = _cookies("admin-1")
    first = _new_code(client, admin, "First")
    second = _new_code(client, admin, "Second")
    live = main.cleanup_mobile_pairing_codes(main.load_mobile_pairing_codes())
    assert list(live) == [second]
    assert _claim(client, admin, first).status_code == 400
    assert _claim(client, admin, second).status_code == 200


def test_concurrent_code_requests_leave_one_live_code_per_account(client):
    main.save_users([dict(ADMIN)])
    admin = _cookies("admin-1")
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda n: _fresh().post("/api/mobile/pairing", cookies=admin, json={"device_name": f"P{n}"}), range(6)))
    assert all(response.status_code == 200 for response in responses)
    live = main.cleanup_mobile_pairing_codes(main.load_mobile_pairing_codes())
    assert len(live) == 1
    assert _claim(client, admin, next(iter(live))).status_code == 200


def test_the_appliance_caps_live_pairing_codes(client, monkeypatch):
    monkeypatch.setattr(main, "MOBILE_PAIRING_MAX_ACTIVE_CODES", 3)
    accounts = [f"viewer-{n}" for n in range(4)]
    main.save_users([dict(ADMIN)] + [_user(uid) for uid in accounts])
    for uid in accounts[:3]:
        _new_code(client, _cookies(uid))
    refused = client.post("/api/mobile/pairing", cookies=_cookies(accounts[3]), json={"device_name": "Phone"})
    assert refused.status_code == 429
    assert len(main.cleanup_mobile_pairing_codes(main.load_mobile_pairing_codes())) == 3
    # An account that already holds a live code may replace it at the cap.
    assert client.post("/api/mobile/pairing", cookies=_cookies(accounts[0]), json={"device_name": "New"}).status_code == 200


# ------------------------------------------------------------------- configuration restore

@pytest.fixture()
def restore(client, monkeypatch, tmp_path):
    assert tmp_path in main.RESTORE_STAGING_FOLDER.parents  # redirected by isolate_recordings, never the shared root
    main.RESTORE_STAGING_FOLDER.mkdir(parents=True, exist_ok=True)
    rules = tmp_path / "alert_rules.json"
    # CONFIG_BACKUP_FILES is built at import; point it at this test's files (alert_rules.json does not exist yet).
    monkeypatch.setattr(main, "CONFIG_BACKUP_FILES", [rules, main.USERS_FILE, main.USER_INVITES_FILE])
    main.save_users([dict(ADMIN)])
    main.save_user_invites([{"id": "inv-old", "email": "old@example.test", "status": "pending"}])
    cookies = _cookies("admin-1")
    before = {"users": main.USERS_FILE.read_bytes(), "invites": main.USER_INVITES_FILE.read_bytes()}

    def post(members):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            for name, data in members:
                bundle.writestr(name, data)
        return archive.getvalue()

    def send(payload):
        return _fresh().post("/api/operations/restore", cookies=cookies,
                             files={"backup_file": ("restore.zip", payload, "application/zip")}).json()

    def unchanged():
        return (main.USERS_FILE.read_bytes() == before["users"] and main.USER_INVITES_FILE.read_bytes() == before["invites"]
                and not rules.exists())

    def leftovers():
        return sorted(path.name for path in tmp_path.iterdir() if path.name.endswith((".restore", ".rollback")))

    restored_users = json.dumps([dict(ADMIN), _user("restored-1")])
    return {"post": post, "send": send, "unchanged": unchanged, "leftovers": leftovers, "rules": rules,
            "users": restored_users, "before": before}


def _good_members(restore):
    return [("config/alert_rules.json", json.dumps({"rules": []})), ("config/users.json", restore["users"]),
            ("config/user_invites.json", "[]")]


def test_a_valid_archive_restores_every_file_and_leaves_nothing_behind(restore):
    result = restore["send"](restore["post"](_good_members(restore)))
    assert result["status"] == "complete" and result["restored"] == ["alert_rules.json", "users.json", "user_invites.json"]
    assert {user["id"] for user in main.load_users()} == {"admin-1", "restored-1"}
    assert main.load_user_invites() == [] and json.loads(restore["rules"].read_text()) == {"rules": []}
    assert restore["leftovers"]() == []


@pytest.mark.parametrize("bad_member, reason", [
    (("config/alert_rules.json", "{not json"), "alert_rules.json is not valid JSON"),
    (("config/users.json", json.dumps({"id": "admin-1"})), "users.json must hold a list of records"),
    (("config/users.json", json.dumps([{"id": "x"}])), "users.json holds a user without an id or email"),
    (("config/user_invites.json", json.dumps(["not a record"])), "user_invites.json must hold a list of records"),
    (("config/alert_rules.json", b"\xff\xfe binary"), "alert_rules.json is not UTF-8 text"),
])
def test_a_malformed_member_refuses_the_whole_archive_and_changes_nothing(restore, bad_member, reason):
    members = [member for member in _good_members(restore) if member[0] != bad_member[0]] + [bad_member]
    result = restore["send"](restore["post"](members))
    assert result["status"] == "error" and "nothing was changed" in result["message"] and reason in result["message"]
    assert restore["unchanged"]() and restore["leftovers"]() == []


def test_a_duplicated_member_refuses_the_archive(restore):
    members = _good_members(restore) + [("config/extra/users.json", restore["users"])]
    result = restore["send"](restore["post"](members))
    assert result["status"] == "error" and "more than once" in result["message"]
    assert restore["unchanged"]()


def test_a_corrupted_archive_member_refuses_the_archive(restore):
    payload = bytearray(restore["post"](_good_members(restore)))
    marker = b'"restored-1"'
    offset = bytes(payload).index(marker)
    payload[offset + 1] = ord("X")  # stored (uncompressed) data no longer matches its CRC-32
    result = restore["send"](bytes(payload))
    assert result["status"] == "error" and "could not be read from the archive" in result["message"]
    assert restore["unchanged"]() and restore["leftovers"]() == []


def test_a_failure_mid_swap_puts_every_file_back(restore, monkeypatch):
    real_replace = Path.replace

    def fail_third(self, target):
        if self.name.endswith(".restore") and Path(target).name == "user_invites.json":
            raise OSError("synthetic disk failure on the third file")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", fail_third)
    result = restore["send"](restore["post"](_good_members(restore)))
    assert result["status"] == "error" and "synthetic disk failure" in result["message"]
    assert restore["unchanged"]()  # users.json put back, alert_rules.json (new) removed, invites never replaced
    assert restore["leftovers"]() == []


def test_a_file_that_cannot_be_put_back_keeps_its_copy_and_is_named(restore, monkeypatch):
    real_replace = Path.replace

    def fail_swap_and_rollback(self, target):
        if self.name.endswith(".restore") and Path(target).name == "user_invites.json":
            raise OSError("synthetic disk failure on the third file")
        if self.name.endswith(".rollback") and Path(target).name == "users.json":
            raise OSError("synthetic rollback failure")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", fail_swap_and_rollback)
    result = restore["send"](restore["post"](_good_members(restore)))
    assert result["status"] == "error" and "could not put back users.json" in result["message"]
    kept = [name for name in restore["leftovers"]() if name.endswith(".rollback")]
    assert len(kept) == 1 and kept[0].startswith("users.json.") and kept[0] in result["message"]
    assert (main.USERS_FILE.parent / kept[0]).read_bytes() == restore["before"]["users"]  # the pre-restore copy is safe
    assert not restore["rules"].exists()  # the other swapped file was still put back
    assert main.USER_INVITES_FILE.read_bytes() == restore["before"]["invites"]
