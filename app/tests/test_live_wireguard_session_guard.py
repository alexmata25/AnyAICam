"""1.2.7 P2P/WireGuard audit, defects 3 and 5.
3: the WireGuard live fetch accepted ANY session row of the customer -- stopped, expired, another user's, or one whose
   viewer had lost can_live. It now requires the viewer's own live session and re-checks the live permission; its
   playlist fetches lease the session (expires_at only, never relay demand).
5: after a key rotation the OLDEST active peer's tunnel address was used; the appliance uses its newest key."""
from datetime import datetime, timedelta

import pytest

import live_view_wireguard
import partner_portal
import wireguard_remote
from database_backend import override_target
from test_live_view_wireguard import (  # noqa: F401  (fixtures)
    _enable_flags, _start_session, client, db_path,
)

MANIFEST = b"#EXTM3U\n#EXT-X-VERSION:6\n#EXTINF:2.0,\ncamera1_000000001.ts\n"


@pytest.fixture()
def wireguard_on(monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})
    calls = []
    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway",
                        lambda tunnel, port, path: calls.append(tunnel) or MANIFEST)
    return calls


def _db(db_path, sql, params=()):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            cursor = db.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()] if sql.lstrip().upper().startswith("SELECT") else None


def _playlist(client, session_id, cookies):
    return client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=cookies)


def test_a_live_own_session_still_plays(client, db_path, wireguard_on):
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    assert _playlist(client, session_id, cookies).status_code == 200


@pytest.mark.parametrize("change", [
    "UPDATE live_view_sessions SET state='stopped' WHERE id=?",
    "UPDATE live_view_sessions SET state='expired' WHERE id=?",
])
def test_a_stopped_or_expired_session_gets_no_video(client, db_path, wireguard_on, change):
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    _db(db_path, change, (session_id,))
    assert _playlist(client, session_id, cookies).status_code == 409
    segment = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/camera1_000000001.ts", cookies=cookies)
    assert segment.status_code == 409
    assert wireguard_on == []  # the appliance was never contacted


def test_a_session_past_its_lease_gets_no_video_even_before_the_lazy_sweep(client, db_path, wireguard_on):
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    _db(db_path, "UPDATE live_view_sessions SET expires_at=? WHERE id=?",
        ((datetime.now() - timedelta(seconds=1)).isoformat(), session_id))
    assert _playlist(client, session_id, cookies).status_code == 409


def test_another_user_of_the_same_customer_cannot_use_the_session(client, db_path, wireguard_on):
    session_id, _ = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    _db(db_path, "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                 "VALUES('user-a2','viewer-a2@example.test','customer_viewer','cust-a','x','all','2026-10-17')")
    viewer = {partner_portal.SESSION_COOKIE: partner_portal._token("viewer-a2@example.test", "customer_viewer", None, "cust-a", None)}
    assert _playlist(client, session_id, viewer).status_code == 404


def test_a_revoked_live_permission_cuts_the_wireguard_stream(client, db_path, wireguard_on):
    _db(db_path, "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                 "VALUES('user-v','viewer-v@example.test','customer_viewer','cust-a','x','selected','2026-10-17')")
    _db(db_path, "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-v','cam-a',1)")
    viewer = {partner_portal.SESSION_COOKIE: partner_portal._token("viewer-v@example.test", "customer_viewer", None, "cust-a", None)}
    started = client.post("/api/customer/cameras/cam-a/live/start", cookies=viewer)
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    assert _playlist(client, session_id, viewer).status_code == 200
    _db(db_path, "UPDATE customer_camera_permissions SET can_live=0 WHERE user_id='user-v' AND camera_id='cam-a'")
    assert _playlist(client, session_id, viewer).status_code == 404  # indistinguishable (Codex a0a305c)


def test_wireguard_playback_leases_the_session_without_holding_the_relay(client, db_path, wireguard_on):
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    soon = (datetime.now() + timedelta(seconds=90)).isoformat()
    _db(db_path, "UPDATE live_view_sessions SET expires_at=?,last_seen_at=NULL WHERE id=?", (soon, session_id))
    assert _playlist(client, session_id, cookies).status_code == 200
    row = _db(db_path, "SELECT expires_at,last_seen_at FROM live_view_sessions WHERE id=?", (session_id,))[0]
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now() + timedelta(minutes=25)
    assert row["last_seen_at"] is None


def test_the_newest_rotated_peer_is_used(client, db_path, wireguard_on):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            newer = wireguard_remote.enroll_peer(db, appliance_id="appl-a", customer_id="cust-a",
                                                 public_key="B" * 43 + "=", now=datetime(2026, 10, 1))
            peers = wireguard_remote.active_peers_for_appliance(db, "appl-a")
    assert len(peers) == 2 and peers[0]["tunnel_address"] != newer["tunnel_address"]
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    assert _playlist(client, session_id, cookies).status_code == 200
    assert wireguard_on == [newer["tunnel_address"]]


def test_a_same_customer_viewer_with_can_live_cannot_fetch_from_another_users_session(client, db_path, wireguard_on):
    session_id, _ = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    _db(db_path, "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                 "VALUES('user-g','viewer-g@example.test','customer_viewer','cust-a','x','selected','2026-10-17')")
    _db(db_path, "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-g','cam-a',1)")
    viewer = {partner_portal.SESSION_COOKIE: partner_portal._token("viewer-g@example.test", "customer_viewer", None, "cust-a", None)}
    assert _playlist(client, session_id, viewer).status_code == 404
    assert wireguard_on == []
