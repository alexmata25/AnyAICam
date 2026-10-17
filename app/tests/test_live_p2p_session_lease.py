"""1.2.7 P2P/WireGuard audit, defect 2: a P2P viewer's session was never leased. Only the relay playlist fetch extends a
session, so a P2P view expired at the 30-minute mark and a later P2P drop could not resume the relay
(record_relay_viewer_activity() requires a live 'requested' session). POST /keepalive extends the lease while P2P plays,
without becoming relay demand (the relay upload must still stop while P2P works)."""
from datetime import datetime, timedelta

import partner_portal
from database_backend import override_target
from tests.test_live_view_p2p_signaling import (  # noqa: F401  (fixtures)
    _owner_cookie, _start_session, client, db_path, p2p_routes_enabled_for_test,
)


def _row(db_path, session_id):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            return dict(db.execute("SELECT * FROM live_view_sessions WHERE id=?", (session_id,)).fetchone())


def _set(db_path, sql, params):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            db.execute(sql, params)


def _keepalive(client, session_id, cookies):
    return client.post(f"/api/customer/live/sessions/{session_id}/keepalive", cookies=cookies)


def test_a_p2p_view_past_the_original_window_stays_leased_and_can_still_resume_the_relay(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    soon = (datetime.now() + timedelta(seconds=60)).isoformat()  # 29 minutes into the view
    _set(db_path, "UPDATE live_view_sessions SET expires_at=? WHERE id=?", (soon, session_id))
    assert _keepalive(client, session_id, cookies).status_code == 200
    leased = datetime.fromisoformat(_row(db_path, session_id)["expires_at"])
    assert leased > datetime.now() + timedelta(minutes=25)

    import live_relay_idle_sweep
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            live = db.execute(
                "SELECT 1 FROM live_view_sessions WHERE id=? AND state='requested' AND expires_at>?",
                (session_id, (datetime.now() + timedelta(minutes=5)).isoformat()),
            ).fetchone()
    assert live, "after the original 30-minute window the session must still be live for a relay resume"
    assert live_relay_idle_sweep.SESSION_LEASE_SECONDS >= 600


def test_the_keepalive_is_not_relay_demand(client, db_path):
    """last_seen_at is the relay heartbeat; a P2P keepalive must never refresh it, or P2P would hold the AWS relay."""
    session_id, cookies = _start_session(client, db_path)
    _set(db_path, "UPDATE live_view_sessions SET last_seen_at=NULL WHERE id=?", (session_id,))
    assert _keepalive(client, session_id, cookies).status_code == 200
    assert _row(db_path, session_id)["last_seen_at"] is None


def test_the_keepalive_never_shortens_a_longer_lease(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    later = (datetime.now() + timedelta(hours=2)).isoformat()
    _set(db_path, "UPDATE live_view_sessions SET expires_at=? WHERE id=?", (later, session_id))
    assert _keepalive(client, session_id, cookies).status_code == 200
    assert _row(db_path, session_id)["expires_at"] == later


def test_an_expired_or_stopped_session_is_not_revived(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    past = (datetime.now() - timedelta(seconds=5)).isoformat()
    _set(db_path, "UPDATE live_view_sessions SET expires_at=? WHERE id=?", (past, session_id))
    assert _keepalive(client, session_id, cookies).status_code == 409
    assert _row(db_path, session_id)["expires_at"] == past

    session_id, cookies = _start_session(client, db_path)
    _set(db_path, "UPDATE live_view_sessions SET state='stopped' WHERE id=?", (session_id,))
    assert _keepalive(client, session_id, cookies).status_code == 409


def test_another_customer_cannot_lease_the_session(client, db_path):
    session_id, _ = _start_session(client, db_path)
    other = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    assert _keepalive(client, session_id, other).status_code == 404


def test_another_user_of_the_same_customer_cannot_lease_the_session(client, db_path):
    session_id, _ = _start_session(client, db_path)
    _set(db_path, "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                  "VALUES('user-a2','viewer-a2@example.test','customer_viewer','cust-a','x','all','2026-10-17')", ())
    viewer = {partner_portal.SESSION_COOKIE: partner_portal._token("viewer-a2@example.test", "customer_viewer", None, "cust-a", None)}
    assert _keepalive(client, session_id, viewer).status_code == 404


def test_a_revoked_live_permission_ends_the_lease(client, db_path):
    _set(db_path, "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                  "VALUES('user-v','viewer-v@example.test','customer_viewer','cust-a','x','selected','2026-10-17')", ())
    _set(db_path, "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-v','cam-a',1)", ())
    viewer = {partner_portal.SESSION_COOKIE: partner_portal._token("viewer-v@example.test", "customer_viewer", None, "cust-a", None)}
    started = client.post("/api/customer/cameras/cam-a/live/start", cookies=viewer)
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    assert _keepalive(client, session_id, viewer).status_code == 200
    _set(db_path, "UPDATE customer_camera_permissions SET can_live=0 WHERE user_id='user-v' AND camera_id='cam-a'", ())
    # 404, not 403: an inaccessible session is indistinguishable from someone else's (Codex 70db3a4)
    assert _keepalive(client, session_id, viewer).status_code == 404


def test_both_pages_lease_the_session_only_while_their_own_p2p_connection_plays():
    import live_view_page
    source = open(live_view_page.__file__, encoding="utf-8").read()
    assert "keepLiveSessionLeased(sessionId,()=>!stopped&&p2pConnection===result.pc);" in source
    assert "keepLiveSessionLeased(tile.sessionId,()=>!tile.stopped&&tile.p2pConnection===result.pc);" in source
    assert "if (!isActive()) { clearInterval(timer); return; }" in source
