"""P2P pending-signal long-poll (2026-09-24).

The appliance used to poll GET /api/appliance/live/p2p/pending every ~1s,
24/7 (~60,800 requests/day for one appliance on staging, each paying a
full PBKDF2 appliance authentication). The cloud now holds a ?wait=N
request open until a browser offer/ICE candidate arrives for that
appliance (or N seconds pass), and the appliance re-polls immediately
only when the cloud confirms it honored the wait.

Cloud: live_view_p2p.py (notifier + long-poll route). Edge:
webrtc_publisher.py (request shape, fallback delay, worker loop). No
real appliance, MediaMTX, network, or device is involved.
"""

import asyncio
import secrets
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_p2p_pending_long_poll_import.db"):
    import live_view_p2p
    import live_view_sessions
    import partner_portal
    import webrtc_publisher as wp
    from partner_db import connection, initialize_database, password_hash

NOW = "2026-09-24T00:00:00"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "p2p_long_poll.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (NOW,))
            for suffix in ("a", "b"):
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (f"cust-{suffix}", "partner-1", "C", f"{suffix}@example.test", "active", NOW))
                db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{suffix}", f"cust-{suffix}", "Main", NOW))
                db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (f"appl-{suffix}", f"cust-{suffix}", f"site-{suffix}", f"AIC-{suffix}", NOW))
                db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{suffix}", f"appl-{suffix}", password_hash(f"secret-{suffix}"), NOW))
                db.execute(
                    "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
                    "VALUES(?,?,?,?,1,?,'configured','Camera 1',?)", (f"cam-{suffix}", f"cust-{suffix}", f"site-{suffix}", f"appl-{suffix}", f"urn:uuid:{suffix}", NOW)
                )
                db.execute(
                    "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                    "VALUES(?,?,'customer_owner',?,'x','all',?)", (f"user-{suffix}", f"owner-{suffix}@example.test", f"cust-{suffix}", NOW)
                )
    return path


@pytest.fixture()
def app():
    application = FastAPI()
    live_view_sessions.register_live_view_session_routes(application)
    live_view_p2p.register_live_view_p2p_customer_routes(application)
    live_view_p2p.register_live_view_p2p_appliance_routes(application)
    return application


def _appliance_headers(suffix="a", credential=None):
    return {
        "X-Appliance-Id": f"appl-{suffix}",
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential or f'secret-{suffix}'}",
    }


def _cookies(suffix="a"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(f"owner-{suffix}@example.test", "customer_owner", None, f"cust-{suffix}", None)}


def _start_session(app, db_path, suffix="a"):
    with override_target(sqlite_path=str(db_path)):
        response = TestClient(app).post(f"/api/customer/cameras/cam-{suffix}/live/start", cookies=_cookies(suffix))
    assert response.status_code == 200
    return response.json()["session_id"]


def _submit_offer(app, db_path, session_id, suffix="a"):
    with override_target(sqlite_path=str(db_path)):
        response = TestClient(app).post(f"/api/customer/live/sessions/{session_id}/p2p/offer", cookies=_cookies(suffix), json={"sdp": "v=0 fake-offer"})
    assert response.status_code == 200


def _poll(app, db_path, *, wait=None, suffix="a", credential=None):
    path = "/api/appliance/live/p2p/pending" + (f"?wait={wait}" if wait is not None else "")
    started = time.monotonic()
    with override_target(sqlite_path=str(db_path)):
        response = TestClient(app).get(path, headers=_appliance_headers(suffix, credential))
    return response, time.monotonic() - started


def _poll_in_background(app, db_path, **kwargs):
    result = {}

    def run():
        result["response"], result["elapsed"] = _poll(app, db_path, **kwargs)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


# ------------------------------------------------------------------ cloud


def test_a_plain_poll_still_returns_immediately_for_older_appliances(app, db_path):
    response, elapsed = _poll(app, db_path)
    assert response.status_code == 200
    assert response.json() == {"pending": []}  # no long_poll_seconds echo
    assert elapsed < 5


def test_a_long_poll_returns_at_once_when_a_signal_is_already_pending(app, db_path):
    session_id = _start_session(app, db_path)
    _submit_offer(app, db_path, session_id)
    response, elapsed = _poll(app, db_path, wait=20)
    body = response.json()
    assert elapsed < 5
    assert [(item["session_id"], item["kind"]) for item in body["pending"]] == [(session_id, "offer")]
    assert body["long_poll_seconds"] == 20


def test_a_long_poll_with_nothing_pending_is_held_until_the_wait_elapses(app, db_path):
    response, elapsed = _poll(app, db_path, wait=1.0)
    assert response.json() == {"pending": [], "long_poll_seconds": 1.0}
    assert 0.9 <= elapsed < 5


def test_an_offer_submitted_during_a_long_poll_wakes_it_promptly(app, db_path, monkeypatch):
    # Re-check made very slow, so only the in-process wake-up can explain a prompt return.
    monkeypatch.setattr(live_view_p2p, "PENDING_RECHECK_SECONDS", 60.0)
    session_id = _start_session(app, db_path)
    thread, result = _poll_in_background(app, db_path, wait=15)
    time.sleep(0.5)
    _submit_offer(app, db_path, session_id)
    thread.join(timeout=20)
    assert result["elapsed"] < 5
    assert [item["kind"] for item in result["response"].json()["pending"]] == ["offer"]


def test_a_signal_written_by_another_worker_process_is_found_by_the_recheck(app, db_path, monkeypatch):
    """No in-process notify (as with uvicorn --workers>1): the waiter's own
    periodic re-check still delivers it long before the wait expires."""
    monkeypatch.setattr(live_view_p2p, "PENDING_RECHECK_SECONDS", 0.3)
    session_id = _start_session(app, db_path)
    thread, result = _poll_in_background(app, db_path, wait=15)
    time.sleep(0.5)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "INSERT INTO live_view_p2p_signaling(id,session_id,kind,payload_json,created_at,consumed_at) VALUES(?,?,?,?,?,NULL)",
                ("sig-direct", session_id, "offer", '{"sdp": "v=0"}', NOW),
            )
    thread.join(timeout=20)
    assert result["elapsed"] < 5
    assert [item["kind"] for item in result["response"].json()["pending"]] == ["offer"]


def test_another_appliances_offer_neither_wakes_nor_leaks_into_this_long_poll(app, db_path, monkeypatch):
    monkeypatch.setattr(live_view_p2p, "PENDING_RECHECK_SECONDS", 60.0)
    session_a = _start_session(app, db_path, "a")
    thread, result = _poll_in_background(app, db_path, wait=1.5, suffix="b")
    time.sleep(0.3)
    _submit_offer(app, db_path, session_a, "a")
    thread.join(timeout=10)
    assert result["response"].json()["pending"] == []
    assert result["elapsed"] >= 1.3
    response, _ = _poll(app, db_path, suffix="a")
    assert [item["session_id"] for item in response.json()["pending"]] == [session_a]


def test_the_wait_is_capped(app, db_path):
    session_id = _start_session(app, db_path)
    _submit_offer(app, db_path, session_id)  # pending already, so this returns at once
    response, _ = _poll(app, db_path, wait=999)
    assert response.json()["long_poll_seconds"] == live_view_p2p.MAX_PENDING_WAIT_SECONDS


def test_waiters_are_released_after_the_request_returns(app, db_path):
    _poll(app, db_path, wait=0.5)
    assert live_view_p2p.pending_signal_notifier.waiter_count("appl-a") == 0


def test_a_bad_credential_is_rejected_before_any_waiting(app, db_path):
    response, elapsed = _poll(app, db_path, wait=20, credential="wrong")
    assert response.status_code == 403
    assert elapsed < 5
    assert live_view_p2p.pending_signal_notifier.waiter_count("appl-a") == 0


# ------------------------------------------------------------------- edge


def test_the_publisher_requests_a_long_poll_with_a_longer_http_timeout(monkeypatch):
    monkeypatch.setattr(wp, "LONG_POLL_SECONDS", 20.0)
    monkeypatch.setattr(wp, "LONG_POLL_HTTP_TIMEOUT_SECONDS", 35.0)
    path, timeout = wp._pending_poll_request()
    assert path == "/api/appliance/live/p2p/pending?wait=20"
    assert timeout > 20


def test_long_poll_can_be_disabled_to_restore_plain_polling(monkeypatch):
    monkeypatch.setattr(wp, "LONG_POLL_SECONDS", 0.0)
    assert wp._pending_poll_request() == ("/api/appliance/live/p2p/pending", 10.0)


@pytest.mark.parametrize(
    ("response", "honored"),
    [({"pending": [], "long_poll_seconds": 20}, True), ({"pending": []}, False), (None, False)],
)
def test_bridge_tick_reports_whether_the_cloud_honored_the_long_poll(monkeypatch, response, honored):
    monkeypatch.setattr(wp, "LONG_POLL_SECONDS", 20.0)
    calls = []
    monkeypatch.setattr(wp, "_control_plane_get", lambda path, timeout=10: calls.append((path, timeout)) or response)
    assert asyncio.run(wp._bridge_tick(lambda n: "rtsp://u:p@h:554/x")) is honored
    assert calls and "wait=20" in calls[0][0] and calls[0][1] > 20


def test_no_sleep_after_an_honored_long_poll_but_scan_seconds_otherwise(monkeypatch):
    monkeypatch.setattr(wp, "SCAN_SECONDS", 1.0)
    assert wp._delay_after_poll(True) == 0.0
    assert wp._delay_after_poll(False) == 1.0


def _run_worker_for(seconds, monkeypatch, *, honored):
    monkeypatch.setattr(wp, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(wp, "LIVE_P2P_ENABLED", True)
    monkeypatch.setattr(wp, "SCAN_SECONDS", 0.5)
    monkeypatch.setattr(wp, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(wp, "_ensure_mediamtx_running", lambda: None)
    monkeypatch.setattr(wp, "stop_mediamtx", lambda: None)
    # CONFIG_REFRESH_SECONDS alone does not skip the first refresh: the
    # worker compares against time.monotonic() (time since boot), so
    # stub the refresh itself -- no network/MediaMTX call in this test.
    monkeypatch.setattr(wp, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(wp, "sync_camera_paths", lambda camera_url_fn: None)
    ticks = {"n": 0}

    async def fake_bridge_tick(camera_url_fn):
        ticks["n"] += 1
        await asyncio.sleep(0.05)  # stands in for the cloud holding the request
        return honored

    monkeypatch.setattr(wp, "_bridge_tick", fake_bridge_tick)

    async def scenario():
        task = asyncio.ensure_future(wp.webrtc_publisher_worker(lambda n: "rtsp://u:p@h:554/x"))
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    return ticks["n"]


def test_worker_repolls_straight_away_after_an_honored_long_poll(monkeypatch):
    assert _run_worker_for(0.6, monkeypatch, honored=True) >= 5


def test_worker_falls_back_to_scan_seconds_against_an_older_cloud(monkeypatch):
    assert _run_worker_for(0.6, monkeypatch, honored=False) <= 2
