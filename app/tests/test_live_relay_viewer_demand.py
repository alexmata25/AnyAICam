"""Live relay is strictly viewer-demand driven (2026-09-24).

Regression coverage for the idle-stop defect measured on staging (~4,000
relay segment uploads/hour around the clock with no viewer): the idle
sweep counted any 'requested' session as a viewer -- ignoring expires_at,
with expiry only ever applied lazily on start/stop -- and a viewer that
won over P2P (or crashed) held the relay open for the whole session.

Cloud side: live_relay_idle_sweep.py (demand, expiry, stop, re-stop,
resume) + live_view_sessions.py / live_playlist.py routes. Edge side:
live_relay_uploader.py's command reconciliation (one relay per camera,
nothing uploads without an active command). No real AWS, CloudFront,
appliance, or device is involved anywhere in this file.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_live_relay_viewer_demand_import.db"):
    import live_playlist
    import live_relay_idle_sweep as sweep
    import live_relay_uploader as lru
    import live_view_sessions
    import partner_portal
    from partner_db import connection, initialize_database

SEED_AT = "2026-09-24T00:00:00"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "relay_demand.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (SEED_AT,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active',?)", (SEED_AT,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main',?)", (SEED_AT,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (SEED_AT,))
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
                "VALUES('cam-1','cust-1','site-1','appl-1',1,'urn:uuid:fake','configured','Front Door',?)", (SEED_AT,)
            )
            for user_id, email in (("user-1", "owner@example.test"), ("user-2", "viewer2@example.test")):
                db.execute(
                    "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                    "VALUES(?,?,'customer_owner','cust-1','x','all',?)", (user_id, email, SEED_AT)
                )
    return path


def _insert_session(db_path, session_id, *, requested_at, state="requested", email="owner@example.test", last_seen_at=None, lease_seconds=1800):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,transport,"
                "requested_at,expires_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, "cust-1", "site-1", "cam-1", "user-1", email, "customer_owner", state, "not_configured",
                 requested_at.isoformat(), (requested_at + timedelta(seconds=lease_seconds)).isoformat(),
                 last_seen_at.isoformat() if last_seen_at else None),
            )


def _tick(db_path, now, activity=None):
    with override_target(sqlite_path=str(db_path)):
        sweep.run_idle_sweep_tick(now, segment_activity=activity or {})


def _heartbeat(db_path, now, email="owner@example.test"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return sweep.record_relay_viewer_activity(db, camera_id="cam-1", customer_id="cust-1", requested_by=email, now=now)


def _commands(db_path, command):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM appliance_commands WHERE command=? ORDER BY created_at", (command,)).fetchall()]


def _session_state(db_path, session_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return dict(db.execute("SELECT * FROM live_view_sessions WHERE id=?", (session_id,)).fetchone())


def _sessions_client(db_path):
    app = FastAPI()
    live_view_sessions.register_live_view_session_routes(app)
    return TestClient(app)


def _cookie(email="owner@example.test"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, "customer_owner", None, "cust-1", None)}


T0 = datetime(2026, 9, 24, 12, 0, 0)
GRACE = sweep.IDLE_GRACE_PERIOD_SECONDS


# ------------------------------------------------------------ no viewer


def test_no_viewer_and_no_uploads_means_nothing_is_started_or_tracked(db_path):
    _tick(db_path, T0)
    _tick(db_path, T0 + timedelta(seconds=GRACE + 1))
    assert _commands(db_path, "start_live_relay") == []
    assert _commands(db_path, "stop_live_relay") == []


def test_a_camera_uploading_with_no_viewer_at_all_is_stopped(db_path):
    """The appliance is uploading (e.g. a stale 'active' relay state that
    survived a restart) but this cloud has no session for the camera --
    it must still be stopped, not ignored because it has no session
    history."""
    uploading = {"cam-1": T0.timestamp()}
    _tick(db_path, T0, uploading)
    assert _commands(db_path, "stop_live_relay") == []
    _tick(db_path, T0 + timedelta(seconds=GRACE + 1), {"cam-1": (T0 + timedelta(seconds=GRACE)).timestamp()})
    stops = _commands(db_path, "stop_live_relay")
    assert len(stops) == 1 and json.loads(stops[0]["payload_json"]) == {"camera_number": 1, "camera_id": "cam-1"}


def test_edge_uploads_nothing_without_an_active_relay_command(monkeypatch):
    calls = []
    monkeypatch.setattr(lru, "_active_cameras", {})
    monkeypatch.setattr(lru, "_reconcile_relay_commands", lambda: None)
    monkeypatch.setattr(lru, "_relay_camera_once", lambda hls, number, camera_id: calls.append(number))
    asyncio.run(lru._relay_tick(asyncio.Semaphore(8), "/hls"))
    assert calls == []


# ----------------------------------------------------------- viewers start


def test_first_viewer_starts_the_relay(db_path):
    with override_target(sqlite_path=str(db_path)):
        response = _sessions_client(db_path).post("/api/customer/cameras/cam-1/live/start", cookies=_cookie())
    assert response.status_code == 200
    starts = _commands(db_path, "start_live_relay")
    assert len(starts) == 1 and starts[0]["appliance_id"] == "appl-1"


def test_second_viewer_reuses_the_same_relay_on_the_edge(tmp_path, monkeypatch):
    """Two viewers queue two start commands; the edge applies them to ONE
    relay entry for the camera and runs ONE upload loop per tick -- the
    single local HLS encoder is shared, never duplicated."""
    commands_file = tmp_path / "live_relay_commands.json"
    monkeypatch.setattr(lru, "RELAY_COMMANDS_FILE", commands_file)
    monkeypatch.setattr(lru, "_active_cameras", {})
    monkeypatch.setattr(lru, "_sessions", {})
    monkeypatch.setattr(lru, "_last_applied_relay_state", {})
    calls = []
    monkeypatch.setattr(lru, "_relay_camera_once", lambda hls, number, camera_id: calls.append((number, camera_id)))

    for _viewer in range(2):  # each viewer's start_live_relay rewrites the same desired state
        commands_file.write_text(json.dumps({"1": {"camera_id": "cam-1", "active": True}}), encoding="utf-8")
        lru._reconcile_relay_commands()
    assert lru.active_camera_numbers() == [1]

    asyncio.run(lru._relay_tick(asyncio.Semaphore(8), "/hls"))
    assert calls == [(1, "cam-1")]

    commands_file.write_text(json.dumps({"1": {"camera_id": "cam-1", "active": False}}), encoding="utf-8")
    calls.clear()
    asyncio.run(lru._relay_tick(asyncio.Semaphore(8), "/hls"))
    assert lru.active_camera_numbers() == [] and calls == []


def test_one_viewer_leaving_while_another_keeps_watching_does_not_stop_the_relay(db_path):
    with override_target(sqlite_path=str(db_path)):
        client = _sessions_client(db_path)
        first = client.post("/api/customer/cameras/cam-1/live/start", cookies=_cookie()).json()
        client.post("/api/customer/cameras/cam-1/live/start", cookies=_cookie("viewer2@example.test"))
        assert client.post(f"/api/customer/live/sessions/{first['session_id']}/stop", cookies=_cookie()).status_code == 200

    now = datetime.now()
    for step in range(0, 181, 10):  # three minutes of the remaining viewer watching relay video
        at = now + timedelta(seconds=step)
        _heartbeat(db_path, at, email="viewer2@example.test")
        _tick(db_path, at)
    assert _commands(db_path, "stop_live_relay") == []


def test_last_viewer_leaving_stops_the_relay_after_the_grace_period(db_path):
    with override_target(sqlite_path=str(db_path)):
        client = _sessions_client(db_path)
        session = client.post("/api/customer/cameras/cam-1/live/start", cookies=_cookie()).json()
        client.post(f"/api/customer/live/sessions/{session['session_id']}/stop", cookies=_cookie())

    now = datetime.now()
    _tick(db_path, now)
    _tick(db_path, now + timedelta(seconds=GRACE - 5))
    assert _commands(db_path, "stop_live_relay") == []
    _tick(db_path, now + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1
    _tick(db_path, now + timedelta(seconds=GRACE + 20))
    assert len(_commands(db_path, "stop_live_relay")) == 1  # one stop per idle cycle


# ------------------------------------------------ abandoned / stale viewers


def test_an_abandoned_viewer_that_stops_fetching_releases_the_relay(db_path):
    """Tab crashed or closed with no stop call: its session is still
    'requested' and unexpired, but its heartbeat goes stale."""
    _insert_session(db_path, "sess-1", requested_at=T0, last_seen_at=T0 + timedelta(seconds=100))
    idle_at = T0 + timedelta(seconds=100 + sweep.VIEWER_ACTIVITY_TIMEOUT_SECONDS + 1)
    _tick(db_path, idle_at)
    _tick(db_path, idle_at + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1


def test_an_expired_requested_session_no_longer_holds_the_relay_even_with_no_start_or_stop_calls(db_path):
    """The exact staging failure: sessions past expires_at stayed
    'requested' (expiry was only applied lazily by start/stop routes),
    so the relay was never stopped overnight."""
    _insert_session(db_path, "sess-1", requested_at=T0, last_seen_at=T0 + timedelta(minutes=29), lease_seconds=1800)
    late = T0 + timedelta(hours=3)
    _tick(db_path, late)
    assert _session_state(db_path, "sess-1")["state"] == "expired"
    _tick(db_path, late + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1


def test_a_viewer_that_won_over_p2p_releases_the_relay_after_the_startup_race(db_path):
    """P2P viewers never fetch the relay playlist, so once the P2P/relay
    race window has passed the relay has no demand."""
    _insert_session(db_path, "sess-1", requested_at=T0)
    _tick(db_path, T0 + timedelta(seconds=10))
    _tick(db_path, T0 + timedelta(seconds=GRACE + 5))
    assert _commands(db_path, "stop_live_relay") == []  # still inside the startup race
    after_race = T0 + timedelta(seconds=sweep.RELAY_STARTUP_GRACE_SECONDS + 1)
    _tick(db_path, after_race)
    _tick(db_path, after_race + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1


def test_an_actively_watching_viewer_keeps_the_relay_past_the_session_window(db_path):
    _insert_session(db_path, "sess-1", requested_at=T0, lease_seconds=1800)
    for minute in range(0, 45):  # 45 minutes of continuous watching, heartbeat each 20s
        for second in (0, 20, 40):
            at = T0 + timedelta(minutes=minute, seconds=second)
            _heartbeat(db_path, at)
            _tick(db_path, at)
    assert _commands(db_path, "stop_live_relay") == []
    session = _session_state(db_path, "sess-1")
    assert session["state"] == "requested"
    assert session["expires_at"] > (T0 + timedelta(minutes=44)).isoformat()  # lease was extended


def test_heartbeat_writes_are_throttled(db_path):
    _insert_session(db_path, "sess-1", requested_at=T0)
    assert _heartbeat(db_path, T0 + timedelta(seconds=1))["heartbeat"] is True
    assert _heartbeat(db_path, T0 + timedelta(seconds=3))["heartbeat"] is False
    assert _heartbeat(db_path, T0 + timedelta(seconds=1 + sweep.HEARTBEAT_WRITE_INTERVAL_SECONDS + 1))["heartbeat"] is True


# --------------------------------------------------------------- reconnect


def test_a_new_viewer_after_an_idle_stop_starts_the_relay_again(db_path):
    with override_target(sqlite_path=str(db_path)):
        client = _sessions_client(db_path)
        session = client.post("/api/customer/cameras/cam-1/live/start", cookies=_cookie()).json()
        client.post(f"/api/customer/live/sessions/{session['session_id']}/stop", cookies=_cookie())
    now = datetime.now()
    _tick(db_path, now)
    _tick(db_path, now + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1

    with override_target(sqlite_path=str(db_path)):
        assert client.post("/api/customer/cameras/cam-1/live/start", cookies=_cookie()).status_code == 200
        with connection() as db:
            assert db.execute("SELECT COUNT(*) FROM live_relay_idle_tracking").fetchone()[0] == 0
    assert len(_commands(db_path, "start_live_relay")) == 2


def test_a_returning_viewer_resumes_an_idle_stopped_relay_exactly_once(db_path):
    """A background tab stopped fetching long enough for the relay to be
    idle-stopped; when it comes back its next playlist fetch restarts the
    relay -- and a burst of fetches queues only one start."""
    _insert_session(db_path, "sess-1", requested_at=T0, last_seen_at=T0)
    away = T0 + timedelta(seconds=sweep.RELAY_STARTUP_GRACE_SECONDS + 1)
    _tick(db_path, away)
    _tick(db_path, away + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1

    back = away + timedelta(minutes=5)
    first = _heartbeat(db_path, back)
    second = _heartbeat(db_path, back + timedelta(seconds=2))
    assert first["resumed"] is True and second["resumed"] is False
    starts = _commands(db_path, "start_live_relay")
    assert len(starts) == 1 and starts[0]["created_by"] == "live-relay-resume"
    _tick(db_path, back + timedelta(seconds=GRACE + 1))
    assert len(_commands(db_path, "stop_live_relay")) == 1  # the returning viewer is demand again


def test_a_viewer_without_a_live_session_cannot_resume_the_relay(db_path):
    _insert_session(db_path, "sess-1", requested_at=T0, state="stopped")
    _tick(db_path, T0)
    _tick(db_path, T0 + timedelta(seconds=GRACE + 1))
    assert _heartbeat(db_path, T0 + timedelta(minutes=2))["resumed"] is False
    assert _commands(db_path, "start_live_relay") == []


# ----------------------------------------------------------------- re-stop


def test_a_relay_still_uploading_well_after_its_stop_is_stopped_again(db_path):
    _insert_session(db_path, "sess-1", requested_at=T0, state="stopped")
    _tick(db_path, T0)
    stop_at = T0 + timedelta(seconds=GRACE + 1)
    _tick(db_path, stop_at)
    later = stop_at + timedelta(seconds=sweep.RESTOP_AFTER_SECONDS + 10)
    _tick(db_path, later, {"cam-1": later.timestamp()})
    assert len(_commands(db_path, "stop_live_relay")) == 2
    _tick(db_path, later + timedelta(seconds=30), {"cam-1": (later + timedelta(seconds=30)).timestamp()})
    assert len(_commands(db_path, "stop_live_relay")) == 2  # spaced at least RESTOP_AFTER_SECONDS apart


def test_no_restop_once_the_relay_has_actually_stopped(db_path):
    _insert_session(db_path, "sess-1", requested_at=T0, state="stopped")
    _tick(db_path, T0)
    stop_at = T0 + timedelta(seconds=GRACE + 1)
    _tick(db_path, stop_at)
    last_segment = stop_at + timedelta(seconds=60)  # the edge applied the stop within its command poll
    _tick(db_path, stop_at + timedelta(seconds=sweep.RESTOP_AFTER_SECONDS * 3), {"cam-1": last_segment.timestamp()})
    assert len(_commands(db_path, "stop_live_relay")) == 1


# --------------------------------------------------- playlist route heartbeat


def test_the_relay_playlist_route_records_the_viewer_heartbeat(db_path, monkeypatch):
    monkeypatch.setenv(live_playlist.CLOUDFRONT_URL_ENV, "https://d123456.cloudfront.net")
    monkeypatch.setenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, "APKAFAKEKEYPAIRID")
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: (lambda message: b"fake-signature"))
    monkeypatch.setattr(live_playlist.live_manifest_store, "manifest_for", lambda camera_id: {"segments": [], "updated_at": None})
    _insert_session(db_path, "sess-1", requested_at=datetime.now() - timedelta(minutes=2))
    with override_target(sqlite_path=str(db_path)):
        app = FastAPI()
        live_playlist.register_live_playlist_routes(app)
        response = TestClient(app).get("/api/customer/cameras/cam-1/live/playlist.m3u8", cookies=_cookie())
    assert response.status_code == 200
    assert _session_state(db_path, "sess-1")["last_seen_at"] is not None


# ------------------------------------------- recording/event paths unaffected


def test_the_relay_lifecycle_only_ever_queues_relay_commands_and_touches_no_recording_or_event_data(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,created_at) "
                "VALUES('rec-1','cust-1','site-1','appl-1','cam-1','k',?,?,?)", (SEED_AT, SEED_AT, SEED_AT)
            ) if _has_table(db, "recordings") else None
            db.execute(
                "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,object_count,event_timestamp,created_at) "
                "VALUES('det-1','cust-1','site-1','appl-1','cam-1','local-1','person',1,?,?)", (SEED_AT, SEED_AT)
            )
            before = _snapshot(db)

    _insert_session(db_path, "sess-1", requested_at=T0, last_seen_at=T0)
    _tick(db_path, T0 + timedelta(minutes=2))
    _tick(db_path, T0 + timedelta(minutes=3))
    _heartbeat(db_path, T0 + timedelta(minutes=4))
    later = T0 + timedelta(minutes=10)
    _tick(db_path, later, {"cam-1": later.timestamp()})

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            after = _snapshot(db)
            commands = {row[0] for row in db.execute("SELECT DISTINCT command FROM appliance_commands").fetchall()}
    assert before == after
    assert commands <= {"start_live_relay", "stop_live_relay"}

    for module in ("live_relay_idle_sweep.py", "live_playlist.py"):
        source = (Path(__file__).resolve().parent.parent / module).read_text(encoding="utf-8")
        for unrelated in ("recording_uploader", "event_media", "event_clips", "local_recording"):
            assert unrelated not in source, f"{module} must not reach into {unrelated}"


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _snapshot(db):
    snapshot = {}
    for table in ("recordings", "detection_events", "detection_event_media", "cameras"):
        if _has_table(db, table):
            snapshot[table] = [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
    return snapshot
