"""Audit probes (2026-10-15) for per-analytic on/off toggles, VMS 1.2.6 RC3 01d1470.

Written as REQUIREMENT tests: a failing test documents a defect. Not part of
the product tree; copied into the container next to the 01d1470 sources.
"""
import asyncio
import importlib
import secrets
import sqlite3
import time

import pytest

from database_backend import override_target

KEYS = ("smart_motion", "people_counting", "lpr", "ppe", "facial_recognition")
FLAG = {"smart_motion": "smart_motion_enabled", "people_counting": "people_counting_enabled",
        "lpr": "lpr_enabled", "ppe": "ppe_enabled", "facial_recognition": "facial_recognition_enabled"}
NOW = "2026-01-01T00:00:00"


def _seed(conn):
    conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p','P',?)", (NOW,))
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('c','p','C','o@example.test','active',?)", (NOW,))
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s','c','S',?)", (NOW,))
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('a','c','s','AIC-A',?)", (NOW,))
    for n in (1, 2):
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,cloud_recording_mode,smart_motion_enabled,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (f"cam-{n}", "c", "s", "a", n, f"Cam {n}", "configured", "motion", 1, NOW))
    for key in KEYS:
        conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (f"sub-{key}", "c", "s", key, "active", 5, NOW))
    for n, rtype in enumerate(("line_crossing", "intrusion", "loitering", "people_counting", "exclusion", "security_line")):
        for cam in ("cam-1", "cam-2"):
            geometry = '[{"x":0.1,"y":0.2},{"x":0.9,"y":0.8}]' if rtype == "people_counting" else "[]"
            conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,1,?,?)", (f"{cam}-{rtype}", "c", "s", "a", cam, rtype, rtype, geometry, NOW, NOW))
    import customer_analytics_panel as panel
    for cam in ("cam-1", "cam-2"):
        for key in KEYS:
            panel.assign_entitlement(conn, cam, key, now=NOW)
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred','a',?,?)",
                 (__import__("partner_db").password_hash("cred-secret"), NOW))
    conn.commit()


def _headers():
    return {"X-Appliance-Id": "a", "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": "Bearer cred-secret"}


@pytest.fixture()
def cloud(tmp_path):
    db_path = tmp_path / "cloud.db"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed(conn)
        yield db_path, conn


def _config(db_path):
    import appliance_cloud
    from fastapi import FastAPI
    from starlette.requests import Request
    with override_target(sqlite_path=str(db_path)):
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        route = next(route for route in app.routes
                     if getattr(route, "path", None) == "/api/appliance/configuration")
        request = Request({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": "/api/appliance/configuration", "raw_path": b"/api/appliance/configuration",
            "query_string": b"", "headers": [(key.lower().encode(), value.encode()) for key, value in _headers().items()],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 443),
        })
        # Call the registered authenticated route handler directly. This keeps
        # the signed appliance request and database isolation intact while
        # avoiding Windows' blocked Proactor self-pipe used by TestClient.
        body = route.endpoint(request)
    cams = {c["id"]: c for c in body["cameras"]}
    rules = {(r["camera_id"], r["rule_type"]) for r in body["analytics_rules"]}
    return cams, rules


def _set_op(conn, cam, key, enabled):
    conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET enabled=excluded.enabled", (cam, key, int(enabled), NOW, "probe"))
    conn.commit()


# ---------------------------------------------------------------- cloud -> edge delivery, per analytic

def test_baseline_all_enabled_delivered(cloud):
    db_path, _ = cloud
    cams, rules = _config(db_path)
    for cam in ("cam-1", "cam-2"):
        for key in KEYS:
            assert cams[cam][FLAG[key]] is True, (cam, key)
        for rtype in ("line_crossing", "intrusion", "loitering", "people_counting", "exclusion", "security_line"):
            assert (cam, rtype) in rules


@pytest.mark.parametrize("key", KEYS)
def test_cloud_toggle_off_reaches_edge_flag_only_for_that_analytic_and_camera(cloud, key):
    db_path, conn = cloud
    _set_op(conn, "cam-1", key, False)
    cams, rules = _config(db_path)
    assert cams["cam-1"][FLAG[key]] is False
    for other in KEYS:
        if other != key:
            assert cams["cam-1"][FLAG[other]] is True, other
        assert cams["cam-2"][FLAG[other]] is True
    # Recording configuration untouched.
    assert cams["cam-1"]["recording_mode"] == cams["cam-2"]["recording_mode"]
    assert cams["cam-1"]["local_recording_mode"] == cams["cam-2"]["local_recording_mode"]
    # Re-enable restores (persisted preference, DB-backed; recomputed every poll).
    _set_op(conn, "cam-1", key, True)
    cams, _ = _config(db_path)
    assert cams["cam-1"][FLAG[key]] is True


@pytest.mark.parametrize("key,rtypes", [("line_crossing", {"line_crossing"}),
                                        ("intrusion", {"intrusion"}), ("loitering", {"loitering"})])
def test_cloud_rule_toggle_off_removes_only_that_rule_type(cloud, key, rtypes):
    db_path, conn = cloud
    _set_op(conn, "cam-1", key, False)
    _, rules = _config(db_path)
    cam1 = {r for c, r in rules if c == "cam-1"}
    assert cam1 == {"line_crossing", "intrusion", "loitering", "people_counting", "exclusion", "security_line"} - rtypes
    assert {r for c, r in rules if c == "cam-2"} == {"line_crossing", "intrusion", "loitering", "people_counting", "exclusion", "security_line"}


def test_smart_motion_off_drops_dependent_rules_but_keeps_people_counting_line(cloud):
    db_path, conn = cloud
    _set_op(conn, "cam-1", "smart_motion", False)
    _, rules = _config(db_path)
    assert {r for c, r in rules if c == "cam-1"} == {"people_counting"}


def test_people_counting_off_drops_counting_line(cloud):
    db_path, conn = cloud
    _set_op(conn, "cam-1", "people_counting", False)
    _, rules = _config(db_path)
    assert "people_counting" not in {r for c, r in rules if c == "cam-1"}


# ---------------------------------------------------------------- control-center API: rule analytics

@pytest.fixture()
def acc_app(cloud):
    db_path, conn = cloud
    import analytics_control_center as acc
    import partner_portal
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                 "VALUES('owner','p','o@example.test','O','customer_owner','x',1,'c',?,'active','all')", (NOW,))
    conn.commit()
    app = FastAPI()
    acc.register_routes(app, lambda t, n, c: HTMLResponse(c))
    cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("o@example.test", "customer_owner", None, "c", None)}
    import test_analytics_control_center as base  # reuse the in-process route caller

    def call(method, path, payload=None):
        with override_target(sqlite_path=str(db_path)):
            return base._call(app, method, path, cookies=cookie, payload=payload)
    return call, db_path, conn


def test_rule_analytic_ui_state_matches_runtime_before_any_toggle(acc_app):
    call, db_path, _ = acc_app
    state = call("GET", "/api/customer/analytics-control/cameras/cam-1").json()
    _, rules = _config(db_path)
    for key in ("line_crossing", "intrusion", "loitering"):
        item = next(i for i in state["analytics"] if i["key"] == key)
        delivered = (("cam-1", key) in rules)
        assert delivered is True
        assert item["enabled"] is delivered, (key, item)  # UI must not show OFF/Unassigned while the edge runs it


@pytest.mark.parametrize("key", ["line_crossing", "intrusion", "loitering"])
def test_rule_analytic_can_be_turned_back_on_after_off(acc_app, key):
    call, _, _ = acc_app
    assert call("PUT", f"/api/customer/analytics-control/cameras/cam-1/{key}", {"enabled": False}).status_code == 200
    again = call("PUT", f"/api/customer/analytics-control/cameras/cam-1/{key}", {"enabled": True})
    assert again.status_code == 200, (again.status_code, again.json())


@pytest.mark.parametrize("key", KEYS)
def test_seat_analytic_off_then_on_round_trip(acc_app, key):
    call, db_path, _ = acc_app
    assert call("PUT", f"/api/customer/analytics-control/cameras/cam-1/{key}", {"enabled": False}).status_code == 200
    cams, _ = _config(db_path)
    assert cams["cam-1"][FLAG[key]] is False
    again = call("PUT", f"/api/customer/analytics-control/cameras/cam-1/{key}", {"enabled": True})
    assert again.status_code == 200, again.json()
    cams, _ = _config(db_path)
    assert cams["cam-1"][FLAG[key]] is True


def test_ui_shows_rule_analytic_on_while_smart_motion_off(acc_app):
    call, db_path, _ = acc_app
    assert call("PUT", "/api/customer/analytics-control/cameras/cam-1/smart_motion", {"enabled": False}).status_code == 200
    state = call("GET", "/api/customer/analytics-control/cameras/cam-1").json()
    _, rules = _config(db_path)
    lc = next(i for i in state["analytics"] if i["key"] == "line_crossing")
    assert ("cam-1", "line_crossing") not in rules
    assert lc["enabled"] is False, lc  # must not claim ON when the edge no longer receives the rule


# ---------------------------------------------------------------- edge-side behaviour

def test_split_edge_facial_runs_when_cloud_enables_it(tmp_path):
    """A split edge's local DB holds the cloud-synced camera row but no
    camera_analytics_entitlements rows (nothing syncs them)."""
    db_path = tmp_path / "edge.db"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database, connection
        initialize_database()
        with connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,facial_recognition_enabled,created_at) "
                       "VALUES('cam-1','c','s','a',1,'Door','configured',1,?)", (NOW,))
            import facial_events
            assert facial_events._is_entitled(db, camera_id="cam-1") is True


def test_edge_local_toggle_off_is_honoured_by_edge_runtime(tmp_path, monkeypatch):
    """Edge-local Control Center (local customer sign-in) writes
    camera_analytics_operations in the EDGE DB; the edge runtime reads only
    the cloud-synced flags."""
    db_path = tmp_path / "edge.db"
    import edge_camera_sync, recording_uploader, analytics_control_center as acc, product_mode
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database, connection
        initialize_database()
        with connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,lpr_enabled,created_at) "
                       "VALUES('cam-1','c','s','a',1,'Door','configured',1,?)", (NOW,))
            db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) VALUES('cam-1','lpr',0,?,'owner')", (NOW,))
            assert acc.operational_enabled(db, "cam-1", "lpr") is False
    cloud_response = {"cameras": [{"id": "cam-1", "name": "Door", "camera_number": 1, "site_id": "s", "status": "configured",
                                   "recording_mode": "motion", "lpr_enabled": True, "smart_motion_enabled": True}]}
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setattr(product_mode, "current_mode", lambda: "hybrid")
    monkeypatch.setattr("appliance_activation.load_persisted_identity",
                        lambda: {"appliance_id": "a", "cloud_id": "AIC-A", "credential": "x", "customer_id": "c", "site_id": "s"})
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda *a: cloud_response)
    monkeypatch.setattr(recording_uploader, "_control_plane_get", lambda path: cloud_response)
    with override_target(sqlite_path=str(db_path)):
        edge_camera_sync.sync_provisioned_cameras()
        recording_uploader._refresh_camera_map()
        assert recording_uploader._camera_identity(1)["lpr_enabled"] is False


def test_local_mode_appliance_populates_runtime_camera_map(monkeypatch):
    """Fresh Local-mode install: no ANYAICAM_RECORDING_UPLOAD_ENABLED and no
    ANYAICAM_RECORDING_UPLOAD_CAMERAS. Some edge worker must still fill
    recording_uploader._camera_map, which every analytic gate reads."""
    import product_mode
    monkeypatch.delenv("ANYAICAM_RECORDING_UPLOAD_ENABLED", raising=False)
    monkeypatch.delenv("ANYAICAM_RECORDING_UPLOAD_CAMERAS", raising=False)
    monkeypatch.setattr(product_mode, "current_mode", lambda: "local")
    import recording_uploader
    ru = importlib.reload(recording_uploader)
    try:
        assert ru.RECORDING_UPLOAD_ENABLED is False and ru.RECORDING_UPLOAD_CAMERA_SCOPE is None
        calls = []
        monkeypatch.setattr(ru, "_refresh_camera_map", lambda: calls.append(1))
        monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")

        class Stop(BaseException):
            pass

        async def direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        async def fake_sleep(_s):
            raise Stop()

        monkeypatch.setattr(ru.asyncio, "to_thread", direct)
        monkeypatch.setattr(ru.asyncio, "sleep", fake_sleep)
        worker = ru.analytics_camera_map_worker()
        with pytest.raises(Stop):
            worker.send(None)
        worker.close()
        assert calls, "recording_upload_worker never refreshes the analytics camera map in Local mode"
    finally:
        monkeypatch.undo()
        importlib.reload(recording_uploader)


def test_smart_motion_off_with_lpr_on_creates_no_person_vehicle_events(monkeypatch):
    import main

    class Stop(BaseException):
        pass

    saved = []

    def fake_save(camera_number, result):
        saved.append(camera_number)
        raise Stop()

    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def no_sleep(_s):
        raise Stop()

    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main, "cv2", object())
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "get_yolo_model", lambda: None)
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (30.0, 300.0))
    monkeypatch.setattr(main, "detect_objects_frame", lambda camera: {
        "ok": True, "frame": None, "frame_captured_at": None,
        "detections": [{"class_name": "person", "confidence": 0.9, "x": 1, "y": 1, "width": 50, "height": 90}]})
    monkeypatch.setattr(main, "save_yolo_events", fake_save)
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", False)
    monkeypatch.setattr(main.asyncio, "to_thread", direct)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    class ImmediateSemaphore:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False
    monkeypatch.setattr(main, "ai_inference_semaphore", ImmediateSemaphore())
    monkeypatch.setitem(main.ai_detection_state, 1, main._new_ai_state())
    monkeypatch.setitem(main.ai_person_last_event, 1, -1e18)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: {
        "camera_id": "cam-1", "smart_motion_enabled": False, "people_counting_enabled": False,
        "lpr_enabled": True, "ppe_enabled": False, "facial_recognition_enabled": False})
    monkeypatch.setattr(main, "_ai_event_media_loop", object())
    detector = main.ai_person_detector(1)
    try:
        detector.send(None)
    except Stop:
        pass
    finally:
        detector.close()
    assert saved == [], "Smart Motion is OFF but person/vehicle AI events are still generated because LPR is ON"
