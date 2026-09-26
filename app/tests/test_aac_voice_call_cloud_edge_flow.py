"""AAC Voice Call cloud/edge split (2026-09-24).

The cloud owns the one authoritative, homeowner-facing Voice Call
session; the edge detects, checks its (cloud-synced) entrance-camera
config, greets locally, and sends the trigger up through the existing
analytics-sync channel. These tests cover each half separately and then
the whole path end to end with two real, separate SQLite databases (one
cloud, one edge):

  cloud config -> GET /api/appliance/configuration -> edge_camera_sync
  -> edge detection + local greeting -> local analytics event
  -> analytics_sync -> POST /api/appliance/analytics/{camera_id}/events
  -> cloud ingestion -> authoritative session + notification + listening

Only mock greeting/relay providers are ever used; no real audio, relay,
lock, or network I/O happens anywhere in this file.
"""
import asyncio
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_aac_voice_call_cloud_edge_flow_import.db"):
    import aac_voice_call
    import aac_voice_call_events as store
    import aac_voice_call_greeting
    import analytics_sync
    import appliance_cloud
    import edge_camera_sync
    import relay_control
    from partner_db import connection, initialize_database, password_hash

NOW = "2026-09-24T00:00:00"
APPLIANCE_ID = "appl-1"
CREDENTIAL = "cred-appl-1"
IDENTITY = {
    "appliance_id": APPLIANCE_ID, "cloud_id": "AIC-FLOW0001", "credential": CREDENTIAL,
    "customer_id": "cust-1", "site_id": "site-1",
}


# ------------------------------------------------------------- fixtures


@pytest.fixture()
def cloud_db(tmp_path):
    path = tmp_path / "cloud.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
    return path


@pytest.fixture()
def edge_db(tmp_path):
    path = tmp_path / "edge.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
    return path


@pytest.fixture()
def cloud_client(cloud_db, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(cloud_db)):
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as client:
            yield client


@pytest.fixture(autouse=True)
def greeting_provider(monkeypatch):
    provider = aac_voice_call_greeting.MockGreetingAudioProvider()
    monkeypatch.setattr(aac_voice_call_greeting, "_provider", provider)
    yield provider
    aac_voice_call_greeting.reset_provider()


@pytest.fixture(autouse=True)
def relay_provider(monkeypatch):
    """Nothing in this flow may ever reach door/relay code."""
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    yield provider
    relay_control.reset_provider()


@pytest.fixture()
def edge_env(monkeypatch):
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: IDENTITY)
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")


@pytest.fixture()
def sync_env(tmp_path, monkeypatch):
    """analytics_sync pointed at throwaway files, with the camera map
    already known (as _refresh_camera_map() would have built it)."""
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(analytics_sync, "ANALYTICS_EVENTS_FILE", events_file)
    monkeypatch.setattr(analytics_sync, "SYNC_STATE_FILE", tmp_path / "analytics_sync_state.json")
    monkeypatch.setattr(analytics_sync, "_synced_ids_cache", None)
    monkeypatch.setattr(analytics_sync, "SYNC_CAMERA_SCOPE", None)
    monkeypatch.setattr(analytics_sync, "ANALYTICS_SYNC_NOTIFY_ENABLED", False)
    monkeypatch.setattr(analytics_sync, "_camera_map", {1: {"camera_id": "cam-1", "site_id": "site-1"}})
    return events_file


def _auth_headers(appliance_id=APPLIANCE_ID, credential=CREDENTIAL):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed_cloud(cloud_db, *, appliance_id=APPLIANCE_ID, credential=CREDENTIAL, customer_id="cust-1", site_id="site-1",
                camera_id="cam-1", camera_number=1, camera_name="Front Door", owner_email="owner@example.test"):
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "P", "approved", "real", NOW))
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, "partner-1", "C", f"{customer_id}@example.test", "active", "real", NOW))
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Home", NOW))
            db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", (appliance_id, customer_id, site_id, f"AIC-{appliance_id}", "partner-1", NOW))
            db.execute("INSERT OR IGNORE INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), NOW))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (camera_id, customer_id, site_id, appliance_id, camera_name, camera_number, "configured", NOW))
            db.execute(
                "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,account_status,customer_id,camera_access_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"user-{customer_id}", "partner-1", owner_email, "Owner", "customer_owner", "x", 1, "active", customer_id, "all", NOW),
            )


def _cloud_rows(cloud_db, sql, params=()):
    con = sqlite3.connect(cloud_db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(item) for item in con.execute(sql, params).fetchall()]
    finally:
        con.close()


_edge_rows = _cloud_rows


def _event_payload(local_event_id="aacvc-1", timestamp=None, greeting_text="Hello there!", delivered=True):
    return {
        "local_event_id": local_event_id,
        "event_type": "aac_voice_call",
        "confidence": 0.9,
        "object_count": 1,
        "detections": [{"greeting_text_used": greeting_text, "greeting_delivered": delivered}],
        "event_timestamp": timestamp or datetime.now().isoformat(),
    }


def _append_local_event(events_file):
    """Stand-in for main.py's append_analytics_event(): same newest-first
    JSON list the real one writes and analytics_sync reads."""
    def append(event):
        events = json.loads(events_file.read_text(encoding="utf-8")) if events_file.exists() else []
        events.append(event)
        events.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        events_file.write_text(json.dumps(events), encoding="utf-8")
    return append


# ------------------------------------------------ cloud -> edge configuration


def test_configuration_delivers_only_this_appliances_enabled_entrance_cameras_and_greetings(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _seed_cloud(cloud_db, camera_id="cam-1b", camera_number=2, camera_name="Garage")
    _seed_cloud(cloud_db, appliance_id="appl-2", credential="cred-2", customer_id="cust-2", site_id="site-2", camera_id="cam-2", owner_email="o2@example.test")
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1", enabled=True)
        store.set_camera_greeting_text(customer_id="cust-1", camera_id="cam-1", greeting_text="Hi from the front door")
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1b", enabled=False)
        store.set_entrance_camera(customer_id="cust-2", camera_id="cam-2", enabled=True)
        store.set_site_default_greeting(customer_id="cust-1", site_id="site-1", greeting_text="Welcome home")
        store.set_site_default_greeting(customer_id="cust-2", site_id="site-2", greeting_text="Other tenant")

        response = cloud_client.get("/api/appliance/configuration", headers=_auth_headers())

    assert response.status_code == 200
    config = response.json()["aac_voice_call"]
    assert config["entrance_cameras"] == [{"camera_id": "cam-1", "greeting_text": "Hi from the front door"}]
    assert config["site_greetings"] == [{"site_id": "site-1", "greeting_text": "Welcome home"}]


# The appliance's own identity rows, exactly as the real cloud's
# /api/appliance/configuration always includes them -- the edge sync
# materializes these local parent rows before anything references them.
EDGE_IDENTITY_PAYLOAD = {
    "partner": {"id": "partner-1", "name": "P", "approval_status": "approved"},
    "customer": {"id": "cust-1", "partner_id": "partner-1", "name": "C", "email": "cust-1@example.test", "status": "active"},
    "site": {"id": "site-1", "customer_id": "cust-1", "name": "Home"},
    "appliance": {"id": APPLIANCE_ID, "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-FLOW0001", "partner_id": "partner-1"},
}


def _sync_edge(edge_db, monkeypatch, response):
    response = {"identity": EDGE_IDENTITY_PAYLOAD, **response}
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: response)
    with override_target(sqlite_path=str(edge_db)):
        return edge_camera_sync.sync_provisioned_cameras()


EDGE_CAMERA = {"id": "cam-1", "name": "Front Door", "camera_number": 1, "status": "configured", "site_id": "site-1"}


def test_edge_sync_mirrors_entrance_camera_and_greetings_locally(edge_db, edge_env, monkeypatch):
    result = _sync_edge(edge_db, monkeypatch, {
        "cameras": [EDGE_CAMERA],
        "aac_voice_call": {
            "entrance_cameras": [{"camera_id": "cam-1", "greeting_text": "Hi from the front door"}],
            "site_greetings": [{"site_id": "site-1", "greeting_text": "Welcome home"}],
        },
    })
    assert result["aac_voice_call_synced"] == {"entrance_cameras": 1, "site_greetings": 1}
    with override_target(sqlite_path=str(edge_db)):
        assert store.is_entrance_camera("cust-1", "cam-1") is True
        assert store.resolve_greeting_text(customer_id="cust-1", camera_id="cam-1", site_id="site-1") == "Hi from the front door"
    greetings = _edge_rows(edge_db, "SELECT site_id,greeting_text FROM aac_voice_call_site_greetings")
    assert greetings == [{"site_id": "site-1", "greeting_text": "Welcome home"}]


def test_edge_sync_removes_an_entrance_camera_the_cloud_no_longer_reports(edge_db, edge_env, monkeypatch):
    _sync_edge(edge_db, monkeypatch, {
        "cameras": [EDGE_CAMERA],
        "aac_voice_call": {"entrance_cameras": [{"camera_id": "cam-1"}], "site_greetings": [{"site_id": "site-1", "greeting_text": "Hi"}]},
    })
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {"entrance_cameras": [], "site_greetings": []}})
    with override_target(sqlite_path=str(edge_db)):
        assert store.is_entrance_camera("cust-1", "cam-1") is False
    assert _edge_rows(edge_db, "SELECT * FROM aac_voice_call_site_greetings") == []


def test_edge_sync_leaves_local_config_untouched_when_the_cloud_sends_no_voice_call_field(edge_db, edge_env, monkeypatch):
    """An older control plane without this field must never wipe the
    edge's entrance-camera configuration."""
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {"entrance_cameras": [{"camera_id": "cam-1"}], "site_greetings": []}})
    result = _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA]})
    assert result["aac_voice_call_synced"] is None
    with override_target(sqlite_path=str(edge_db)):
        assert store.is_entrance_camera("cust-1", "cam-1") is True


def test_edge_sync_never_touches_another_appliances_cameras_or_accepts_a_foreign_camera(edge_db, edge_env, monkeypatch):
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {"entrance_cameras": [], "site_greetings": []}})
    con = sqlite3.connect(edge_db)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES('cam-other','cust-1','site-1','appl-other','Old',9,'configured',?)", (NOW,))
    con.execute("INSERT INTO aac_voice_call_entrance_cameras(camera_id,customer_id,enabled,configured_at) VALUES('cam-other','cust-1',1,?)", (NOW,))
    con.commit()
    con.close()

    _sync_edge(edge_db, monkeypatch, {
        "cameras": [EDGE_CAMERA],
        "aac_voice_call": {"entrance_cameras": [{"camera_id": "cam-not-mine"}], "site_greetings": []},
    })
    rows = _edge_rows(edge_db, "SELECT camera_id FROM aac_voice_call_entrance_cameras ORDER BY camera_id")
    assert rows == [{"camera_id": "cam-other"}]


# ------------------------------------------------------------- edge half


def _seed_edge_entrance_camera(edge_db, edge_env, monkeypatch, greeting="Hi from the front door"):
    _sync_edge(edge_db, monkeypatch, {
        "cameras": [EDGE_CAMERA],
        "aac_voice_call": {"entrance_cameras": [{"camera_id": "cam-1", "greeting_text": greeting}], "site_greetings": []},
    })


def test_edge_detection_greets_locally_and_forwards_without_creating_a_local_session(edge_db, edge_env, monkeypatch, greeting_provider):
    _seed_edge_entrance_camera(edge_db, edge_env, monkeypatch)
    forwarded = []
    with override_target(sqlite_path=str(edge_db)):
        result = aac_voice_call.handle_edge_person_detected(
            customer_id="cust-1", camera_id="cam-1", camera_number=1, confidence=0.91, forward_event=forwarded.append,
        )
    assert result["triggered"] is True and result["greeting_delivered"] is True
    assert [call.text for call in greeting_provider.calls] == ["Hi from the front door"]
    assert len(forwarded) == 1
    event = forwarded[0]
    assert event["event_type"] == "aac_voice_call" and event["camera"] == 1 and event["id"] == result["local_event_id"]
    assert event["greeting_text_used"] == "Hi from the front door" and event["greeting_delivered"] is True
    assert _edge_rows(edge_db, "SELECT * FROM aac_voice_call_events") == []
    assert _edge_rows(edge_db, "SELECT * FROM notifications") == []


def test_edge_detection_on_a_non_entrance_camera_does_nothing(edge_db, edge_env, monkeypatch, greeting_provider):
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {"entrance_cameras": [], "site_greetings": []}})
    forwarded = []
    with override_target(sqlite_path=str(edge_db)):
        result = aac_voice_call.handle_edge_person_detected(customer_id="cust-1", camera_id="cam-1", camera_number=1, forward_event=forwarded.append)
    assert result == {"triggered": False, "skipped_reason": "not_entrance_camera"}
    assert forwarded == [] and greeting_provider.calls == []


def test_edge_detection_cooldown_suppresses_a_repeat(edge_db, edge_env, monkeypatch):
    _seed_edge_entrance_camera(edge_db, edge_env, monkeypatch)
    forwarded = []
    with override_target(sqlite_path=str(edge_db)):
        first = aac_voice_call.handle_edge_person_detected(customer_id="cust-1", camera_id="cam-1", camera_number=1, forward_event=forwarded.append)
        second = aac_voice_call.handle_edge_person_detected(customer_id="cust-1", camera_id="cam-1", camera_number=1, forward_event=forwarded.append)
    assert first["triggered"] is True
    assert second == {"triggered": False, "skipped_reason": "cooldown"}
    assert len(forwarded) == 1


def test_a_failed_local_greeting_still_forwards_the_trigger_marked_not_greeted(edge_db, edge_env, monkeypatch):
    _seed_edge_entrance_camera(edge_db, edge_env, monkeypatch)

    class BrokenProvider:
        def speak(self, request):
            raise RuntimeError("speaker unavailable")

    forwarded = []
    with override_target(sqlite_path=str(edge_db)):
        result = aac_voice_call.handle_edge_person_detected(
            customer_id="cust-1", camera_id="cam-1", camera_number=1, forward_event=forwarded.append, greeting_provider=BrokenProvider(),
        )
    assert result["triggered"] is True and result["greeting_delivered"] is False
    assert forwarded[0]["greeting_delivered"] is False


@pytest.mark.parametrize(
    ("role", "sync_enabled", "expected"),
    [("edge", True, True), ("edge", False, False), ("combined", True, False), ("cloud", True, False)],
)
def test_cloud_coordinates_voice_calls_only_for_a_hybrid_edge(monkeypatch, role, sync_enabled, expected):
    monkeypatch.setattr(analytics_sync, "RUNTIME_ROLE", role)
    monkeypatch.setattr(analytics_sync, "ANALYTICS_SYNC_ENABLED", sync_enabled)
    assert aac_voice_call.cloud_coordinates_voice_calls() is expected


def test_main_detection_hook_routes_a_hybrid_edge_through_the_edge_half():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "aac_voice_call.cloud_coordinates_voice_calls()" in source
    assert "aac_voice_call.handle_edge_person_detected(" in source
    assert "analytics_sync.request_prompt_scan()" in source


# ----------------------------------------------------- analytics_sync half


def test_sync_payload_carries_the_edge_greeting_outcome():
    payload = analytics_sync._build_payload({
        "id": "aacvc-1", "event_type": "aac_voice_call", "timestamp": NOW, "confidence": 0.9, "object_count": 1,
        "greeting_text_used": "Hi", "greeting_delivered": True, "camera": 1,
    })
    assert payload["event_type"] == "aac_voice_call"
    assert payload["detections"] == [{"greeting_text_used": "Hi", "greeting_delivered": True}]


def test_legacy_notification_forward_never_sends_a_second_voice_call_notification(monkeypatch):
    posts = []
    monkeypatch.setattr(analytics_sync, "_control_plane_post", lambda path, payload: posts.append((path, payload)) or {"status": "accepted"})
    analytics_sync._forward_notification({"id": "aacvc-1", "event_type": "aac_voice_call", "timestamp": NOW}, "cam-1")
    assert posts == []


def test_request_prompt_scan_wakes_the_sync_worker_early(monkeypatch):
    async def scenario():
        monkeypatch.setattr(analytics_sync, "_wake_event", asyncio.Event())
        monkeypatch.setattr(analytics_sync, "_wake_loop", asyncio.get_running_loop())
        started = time.monotonic()
        threading.Timer(0.05, analytics_sync.request_prompt_scan).start()
        await analytics_sync._sleep_or_wake(10.0)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5.0


def test_request_prompt_scan_is_a_no_op_when_the_worker_is_not_running(monkeypatch):
    monkeypatch.setattr(analytics_sync, "_wake_event", None)
    monkeypatch.setattr(analytics_sync, "_wake_loop", None)
    analytics_sync.request_prompt_scan()


# ------------------------------------------------------------- cloud half


def _enable_cloud_entrance(cloud_db, camera_id="cam-1"):
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id=camera_id, enabled=True)


def _post_event(cloud_client, cloud_db, payload, camera_id="cam-1"):
    with override_target(sqlite_path=str(cloud_db)):
        return cloud_client.post(f"/api/appliance/analytics/{camera_id}/events", headers=_auth_headers(), json=payload)


def test_cloud_creates_one_authoritative_session_with_one_notification(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    response = _post_event(cloud_client, cloud_db, _event_payload(greeting_text="Hi from the front door"))
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    detection_id = response.json()["event_id"]

    events = _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")
    assert len(events) == 1
    event = events[0]
    assert event["trigger_source"] == "detection" and event["trigger_detection_event_id"] == detection_id
    assert event["greeting_text_used"] == "Hi from the front door" and event["greeted_at"]
    assert event["listening_opened_at"] and event["state"] == "notified"

    notifications = _cloud_rows(cloud_db, "SELECT event_id,event_type,message FROM notifications")
    assert notifications == [{"event_id": event["id"], "event_type": "aac_voice_call", "message": "Someone is at Front Door."}]


def test_a_replayed_delivery_never_creates_a_second_session_or_notification(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    payload = _event_payload()
    first = _post_event(cloud_client, cloud_db, payload)
    second = _post_event(cloud_client, cloud_db, payload)
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json() == {"status": "duplicate", "event_id": first.json()["event_id"]}
    assert len(_cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")) == 1
    assert len(_cloud_rows(cloud_db, "SELECT * FROM notifications")) == 1


def test_cloud_config_is_authoritative_a_camera_disabled_since_the_last_edge_sync_gets_no_call(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1", enabled=False)
    response = _post_event(cloud_client, cloud_db, _event_payload())
    assert response.status_code == 200
    assert len(_cloud_rows(cloud_db, "SELECT * FROM detection_events WHERE event_type='aac_voice_call'")) == 1
    assert _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events") == []
    assert _cloud_rows(cloud_db, "SELECT * FROM notifications") == []


def test_a_trigger_delivered_late_after_an_outage_becomes_a_missed_visitor(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    old = (datetime.now() - timedelta(hours=1)).isoformat()
    assert _post_event(cloud_client, cloud_db, _event_payload(timestamp=old)).status_code == 200
    event = _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")[0]
    assert event["state"] == "missed" and event["listening_opened_at"] is None
    assert _cloud_rows(cloud_db, "SELECT message FROM notifications") == [{"message": "You missed a visitor at Front Door."}]


def test_greeted_at_is_only_stamped_when_the_edge_actually_delivered_the_greeting(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    _post_event(cloud_client, cloud_db, _event_payload(delivered=False))
    event = _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")[0]
    assert event["greeted_at"] is None and event["listening_opened_at"]


def test_a_failed_ingestion_is_retryable_and_the_replay_completes_it(cloud_client, cloud_db, monkeypatch):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    real_ingest = aac_voice_call.ingest_edge_visitor_event
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return real_ingest(**kwargs)

    monkeypatch.setattr(aac_voice_call, "ingest_edge_visitor_event", flaky)
    payload = _event_payload()
    first = _post_event(cloud_client, cloud_db, payload)
    assert first.status_code == 503
    assert _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events") == []

    second = _post_event(cloud_client, cloud_db, payload)
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert len(_cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")) == 1
    assert len(_cloud_rows(cloud_db, "SELECT * FROM notifications")) == 1


def test_other_event_types_still_get_the_generic_notification(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    response = _post_event(cloud_client, cloud_db, dict(_event_payload(), event_type="person", detections=[]))
    assert response.status_code == 200
    assert [item["event_type"] for item in _cloud_rows(cloud_db, "SELECT event_type FROM notifications")] == ["person"]


# ------------------------------------------------------- full cloud + edge


def _wire_edge_to_cloud(monkeypatch, cloud_client, cloud_db, *, online):
    def cloud_get(path, appliance_id, credential):
        with override_target(sqlite_path=str(cloud_db)):
            return cloud_client.get(path, headers=_auth_headers()).json()

    def cloud_post(path, payload):
        if not online["value"]:
            return None  # cloud/internet unreachable, exactly what _control_plane_post returns
        with override_target(sqlite_path=str(cloud_db)):
            response = cloud_client.post(path, headers=_auth_headers(), json=payload)
        return response.json() if response.status_code == 200 else None

    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", cloud_get)
    monkeypatch.setattr(analytics_sync, "_control_plane_post", cloud_post)


def _edge_detect(edge_db, events_file):
    from appliance_activation import active_appliance_id

    with override_target(sqlite_path=str(edge_db)):
        with connection() as db:
            context = aac_voice_call._camera_tenant_context(db, 1, active_appliance_id())
        return aac_voice_call.handle_edge_person_detected(
            customer_id=context["customer_id"], camera_id=context["id"], camera_number=1,
            forward_event=_append_local_event(events_file),
        )


def test_full_flow_cloud_config_to_edge_greeting_to_cloud_session(cloud_client, cloud_db, edge_db, edge_env, sync_env, monkeypatch, greeting_provider, relay_provider):
    _seed_cloud(cloud_db)
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1", enabled=True)
        store.set_camera_greeting_text(customer_id="cust-1", camera_id="cam-1", greeting_text="Hello! The owner has been notified.")
    _wire_edge_to_cloud(monkeypatch, cloud_client, cloud_db, online={"value": True})

    # 1. Cloud config reaches the edge.
    with override_target(sqlite_path=str(edge_db)):
        sync_result = edge_camera_sync.sync_provisioned_cameras()
    assert sync_result["aac_voice_call_synced"] == {"entrance_cameras": 1, "site_greetings": 0}

    # 2. The edge detects, greets locally, and queues the trigger -- no local session.
    detected = _edge_detect(edge_db, sync_env)
    assert detected["triggered"] is True
    assert [call.text for call in greeting_provider.calls] == ["Hello! The owner has been notified."]
    assert _edge_rows(edge_db, "SELECT * FROM aac_voice_call_events") == []

    # 3. analytics_sync delivers it; the cloud creates the one authoritative session.
    summary = analytics_sync._sync_pending_events()
    assert summary["synced"] == 1
    events = _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")
    assert len(events) == 1
    session = events[0]
    assert session["trigger_source"] == "detection" and session["camera_id"] == "cam-1"
    assert session["greeting_text_used"] == "Hello! The owner has been notified." and session["greeted_at"]
    assert session["listening_opened_at"]
    notifications = _cloud_rows(cloud_db, "SELECT user_id,event_id,event_type FROM notifications")
    assert notifications == [{"user_id": "user-cust-1", "event_id": session["id"], "event_type": "aac_voice_call"}]

    # 4. The homeowner-facing conversation continues on the cloud session.
    with override_target(sqlite_path=str(cloud_db)):
        reply = aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=session["id"], transcript_text="I have a package delivery for you")
    assert reply["intent"] == "delivery" and reply["escalated"] is False

    # 5. Nothing is left pending, and nothing ever touched door/relay code.
    assert analytics_sync._sync_pending_events()["attempted"] == 0
    assert relay_provider.calls == []


def test_full_flow_through_a_cloud_outage_greets_locally_and_delivers_after_reconnect(cloud_client, cloud_db, edge_db, edge_env, sync_env, monkeypatch, greeting_provider):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    online = {"value": True}
    _wire_edge_to_cloud(monkeypatch, cloud_client, cloud_db, online=online)
    with override_target(sqlite_path=str(edge_db)):
        edge_camera_sync.sync_provisioned_cameras()

    online["value"] = False
    detected = _edge_detect(edge_db, sync_env)
    assert detected["triggered"] is True and len(greeting_provider.calls) == 1  # local greeting needs no cloud
    offline = analytics_sync._sync_pending_events()
    assert offline["synced"] == 0 and offline["failed"] == 1
    assert _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events") == []

    online["value"] = True
    assert analytics_sync._sync_pending_events()["synced"] == 1
    assert len(_cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")) == 1
    assert len(_cloud_rows(cloud_db, "SELECT * FROM notifications")) == 1
