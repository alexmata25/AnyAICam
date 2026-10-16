import base64
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

import analytics_control_center as acc
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database


@pytest.fixture()
def db_path():
    # This process-wide path is explicitly redirected to the worktree's
    # isolated SQLite test database.
    return os.environ["ANYAICAM_PARTNER_DB"]


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            for table in ("audit_logs", "camera_analytics_operations", "test_lab_analytics_assignments", "test_lab_grants",
                          "customer_analytics_rules", "camera_analytics_entitlements", "analytics_subscriptions",
                          "customer_camera_permissions", "partner_users", "cameras", "appliances", "sites", "customers", "partners"):
                db.execute(f"DELETE FROM {table}")
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('p','Partner','x')")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('c','p','Customer','owner@example.test','active','x')")
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s','c','Site','x')")
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('a','c','s','AIC-TEST','x')")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,cloud_recording_mode,smart_motion_enabled,created_at) "
                       "VALUES('cam-1','c','s','a','Door','configured',1,'continuous',1,'x')")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,cloud_recording_mode,smart_motion_enabled,created_at) "
                       "VALUES('cam-2','c','s','a','Yard','configured',2,'motion',1,'x')")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                       "VALUES('owner','p','owner@example.test','Owner','customer_owner','x',1,'c','x','active','all')")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                       "VALUES('viewer','p','viewer@example.test','Viewer','customer_viewer','x',1,'c','x','active','selected')")
            db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_settings) VALUES('viewer','cam-1',1,1,0)")
            db.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1','smart_motion','active','x','x')")
            db.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1','lpr','active','x','x')")
            db.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) VALUES('sub','c','s','lpr','active','x')")
            db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                       "VALUES('r','c','s','a','cam-1','line_crossing','Gate','[]',1,'x','x')")
        # Register just these synchronous routes so tests do not import the
        # appliance application or start unrelated workers. Invoke the route
        # handlers in-process to keep the SQLite override in the same context;
        # this focused harness does not exercise ASGI transport behavior.
        app = FastAPI()
        acc.register_routes(app, lambda title, nav, content: HTMLResponse(content))
        yield app


def _cookie(email="owner@example.test", role="customer_owner"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, "c", None)}


class _RouteResult:
    def __init__(self, value=None, status_code=200, detail=None):
        self.status_code = status_code
        self._value = value
        self._detail = detail
        self.text = value.body.decode("utf-8") if isinstance(value, HTMLResponse) else ""

    def json(self):
        if self._detail is not None:
            return {"detail": self._detail}
        return self._value


def _request(app, path, cookies=None):
    cookie_header = "; ".join(f"{key}={value}" for key, value in (cookies or {}).items())
    headers = [(b"host", b"app.example.test")]
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode("latin-1")))
    parts = urlsplit(path)
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "path": parts.path,
        "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
        "root_path": "", "headers": headers, "server": ("app.example.test", 443),
        "client": ("127.0.0.1", 12345),
    })


def _call(app, method, path, *, cookies=None, payload=None):
    actual_parts = urlsplit(path).path.strip("/").split("/")
    for route in app.routes:
        if method not in getattr(route, "methods", set()):
            continue
        template_parts = route.path.strip("/").split("/")
        if len(actual_parts) != len(template_parts):
            continue
        params = {}
        for expected, actual in zip(template_parts, actual_parts):
            match = re.fullmatch(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", expected)
            if match:
                params[match.group(1)] = actual
            elif expected != actual:
                break
        else:
            kwargs = {**params, "request": _request(app, path, cookies)}
            if "payload" in getattr(route.endpoint, "__annotations__", {}) or "payload" in route.path:
                kwargs["payload"] = payload
            try:
                value = route.endpoint(**kwargs)
                return _RouteResult(value=value)
            except HTTPException as exc:
                return _RouteResult(status_code=exc.status_code, detail=exc.detail)
    raise AssertionError(f"No {method} route matches {path}")


def test_control_center_page_exposes_eight_controls_and_guardian_is_hard_off(client):
    response = _call(client, "GET", "/analytics-control-center", cookies=_cookie())
    assert response.status_code == 200
    assert "Analytics Control Center" in response.text
    assert "Turn All OFF" in response.text
    assert "Turn OFF all eight analytics for this camera?" in response.text
    assert "playback continue" in response.text.lower()
    assert "Master state: OFF" in response.text
    assert "No Guardian workers are contacted" in response.text
    assert 'id="guardian-master"' not in response.text
    assert 'disabled aria-disabled="true"' in response.text
    assert "Capabilities available from an approved runtime manifest: 0" in response.text
    body = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()
    assert [item["key"] for item in body["analytics"]] == list(acc.ANALYTICS)


def test_controls_require_owner_or_camera_settings_and_tenant_scope(client):
    assert _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1").status_code == 403
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr", payload={"enabled": False}, cookies=_cookie("viewer@example.test", "customer_viewer")).status_code == 403
    assert _call(client, "GET", "/api/customer/analytics-control/cameras/foreign", cookies=_cookie()).status_code == 404
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/not-a-real-analytic", payload={"enabled": True}, cookies=_cookie()).status_code == 400


def test_viewer_can_manage_only_with_camera_settings_permission(client, db_path):
    viewer = _cookie("viewer@example.test", "customer_viewer")
    assert _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=viewer).status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("UPDATE customer_camera_permissions SET can_settings=1 WHERE user_id='viewer' AND camera_id='cam-1'")
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                 payload={"enabled": False}, cookies=viewer).status_code == 200


def test_operational_switch_is_separate_from_purchase_and_turn_all_off_preserves_video(client, db_path):
    before = sqlite3.connect(db_path).execute("SELECT cloud_recording_mode,status,camera_number FROM cameras WHERE id='cam-1'").fetchone()
    response = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr", payload={"enabled": False}, cookies=_cookie())
    assert response.status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id='cam-1' AND analytic_key='lpr'").fetchone()["status"] == "active"
            assert db.execute("SELECT COUNT(*) AS n FROM analytics_subscriptions WHERE id='sub'").fetchone()["n"] == 1
            assert db.execute("SELECT enabled FROM camera_analytics_operations WHERE camera_id='cam-1' AND analytic_key='lpr'").fetchone()["enabled"] == 0
    assert _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()["analytics"][2]["enabled"] is False

    off = _call(client, "POST", "/api/customer/analytics-control/cameras/cam-1/turn-all-off", cookies=_cookie())
    assert off.status_code == 200
    after = sqlite3.connect(db_path).execute("SELECT cloud_recording_mode,status,camera_number FROM cameras WHERE id='cam-1'").fetchone()
    assert after == before == ("continuous", "configured", 1)
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM camera_analytics_operations WHERE camera_id='cam-1' AND enabled=0").fetchone()[0] == 8
    audit = sqlite3.connect(db_path).execute("SELECT action FROM audit_logs WHERE entity_id='cam-1' ORDER BY id").fetchall()
    assert [row[0] for row in audit] == ["analytics.operational_toggle_changed", "analytics.turn_all_off"]


def test_operational_toggle_survives_application_recreation(client, db_path):
    response = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                     payload={"enabled": False}, cookies=_cookie())
    assert response.status_code == 200
    # A fresh route/app instance represents a process restart while sharing
    # only the persistent database state.
    restarted = FastAPI()
    acc.register_routes(restarted, lambda title, nav, content: HTMLResponse(content))
    state = _call(restarted, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()
    assert next(item for item in state["analytics"] if item["key"] == "lpr")["enabled"] is False


def test_appliance_local_toggle_refreshes_runtime_map_immediately_and_persists(client, db_path, monkeypatch):
    import product_mode
    import recording_uploader

    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(product_mode, "current_mode", lambda: "local")
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("UPDATE cameras SET lpr_enabled=1 WHERE id='cam-1'")
    recording_uploader._camera_map.clear()
    recording_uploader._refresh_camera_map()
    assert recording_uploader._camera_identity(1)["lpr_enabled"] is True

    off = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                payload={"enabled": False}, cookies=_cookie())
    assert off.status_code == 200
    assert recording_uploader._camera_identity(1)["lpr_enabled"] is False

    restarted = FastAPI()
    acc.register_routes(restarted, lambda title, nav, content: HTMLResponse(content))
    again = _call(restarted, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                  payload={"enabled": True}, cookies=_cookie())
    assert again.status_code == 200
    assert recording_uploader._camera_identity(1)["lpr_enabled"] is True


def test_local_and_offline_hybrid_boot_seed_runtime_map_from_persisted_cameras(client, db_path, monkeypatch):
    import product_mode
    import recording_uploader

    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("UPDATE cameras SET lpr_enabled=1 WHERE id='cam-1'")
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    recording_uploader._camera_map.clear()

    monkeypatch.setattr(product_mode, "current_mode", lambda: "local")
    recording_uploader._refresh_camera_map()
    local = recording_uploader._camera_identity(1)
    assert local and local["camera_id"] == "cam-1" and local["lpr_enabled"] is True

    monkeypatch.setattr(product_mode, "current_mode", lambda: "hybrid")
    monkeypatch.setattr(recording_uploader.appliance_config_cache, "get_or_fetch", lambda fetch: None)
    recording_uploader._camera_map.clear()
    recording_uploader._refresh_camera_map()
    offline = recording_uploader._camera_identity(1)
    assert offline and offline["camera_id"] == "cam-1" and offline["lpr_enabled"] is True


def test_assignment_counts_reassignment_and_customer_off_preserve_paid_seat(client, db_path):
    before = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()
    lpr = next(item for item in before["analytics"] if item["key"] == "lpr")
    assert lpr["license"] == {"purchase_kind": "licensed", "purchased_quantity": 1,
                               "assigned_quantity": 1, "available_quantity": 0}
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/lpr/assignment",
                 payload={"assigned": True}, cookies=_cookie()).status_code == 409

    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                 payload={"enabled": False}, cookies=_cookie()).status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id='cam-1' AND analytic_key='lpr'").fetchone()[0] == "active"
            assert acc.operational_enabled(db, "cam-1", "lpr") is False
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr/assignment",
                 payload={"assigned": False}, cookies=_cookie()).status_code == 200
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/lpr/assignment",
                 payload={"assigned": True}, cookies=_cookie()).status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert db.execute("SELECT status FROM analytics_subscriptions WHERE id='sub'").fetchone()[0] == "active"
            assert db.execute("SELECT lpr_enabled FROM cameras WHERE id='cam-1'").fetchone()[0] == 0
            assert db.execute("SELECT lpr_enabled FROM cameras WHERE id='cam-2'").fetchone()[0] == 1
            assert acc.operational_enabled(db, "cam-1", "lpr") is False


def test_system_pause_is_separate_audited_and_does_not_clear_customer_choice(client, db_path):
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                 payload={"enabled": False}, cookies=_cookie()).status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            acc.set_system_pause(db, "cam-1", "lpr", paused=True, reason="resource governor")
            paused = acc.operational_status(db, "cam-1", "lpr")
            assert paused == {"customer_enabled": False, "system_paused": True,
                              "system_pause_reason": "resource governor", "effective_enabled": False}
            acc.set_system_pause(db, "cam-1", "lpr", paused=False)
            resumed = acc.operational_status(db, "cam-1", "lpr")
            assert resumed == {"customer_enabled": False, "system_paused": False,
                               "system_pause_reason": None, "effective_enabled": False}
            assert [r["action"] for r in db.execute("SELECT action FROM audit_logs WHERE entity_id='cam-1' ORDER BY id").fetchall()] == [
                "analytics.operational_toggle_changed", "analytics.system_paused", "analytics.system_pause_cleared"]


def test_commercial_seat_assignment_is_atomic_across_cameras(client, db_path):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at) "
                       "VALUES('ppe-sub','c','s','ppe','active',1,'x')")

    def assign(camera_id):
        return _call(client, "PUT", f"/api/customer/analytics-control/cameras/{camera_id}/ppe/assignment",
                     payload={"assigned": True}, cookies=_cookie()).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(assign, ("cam-1", "cam-2")))
    assert sorted(statuses) == [200, 409]
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert db.execute("SELECT COUNT(*) AS n FROM camera_analytics_entitlements WHERE analytic_key='ppe' AND status='active'").fetchone()["n"] == 1


def test_reduced_or_revoked_quantity_pauses_processing_until_assignments_fit(client, db_path):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
                       "VALUES('cam-2','lpr','active','x','x')")
            db.execute("UPDATE cameras SET lpr_enabled=1 WHERE id='cam-2'")
    state = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()
    lpr = next(item for item in state["analytics"] if item["key"] == "lpr")
    assert lpr["system_paused"] is True
    assert "quantity is below" in lpr["system_pause_reason"].lower()
    assert lpr["assigned"] is True and lpr["customer_enabled"] is True
    assert lpr["enabled"] is False
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr",
                 payload={"enabled": True}, cookies=_cookie()).status_code == 409

    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/lpr/assignment",
                 payload={"assigned": False}, cookies=_cookie()).status_code == 200
    recovered = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()
    lpr = next(item for item in recovered["analytics"] if item["key"] == "lpr")
    assert lpr["system_paused"] is False
    assert lpr["assigned"] is True and lpr["customer_enabled"] is True
    assert lpr["enabled"] is True

    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("UPDATE analytics_subscriptions SET status='cancelled' WHERE id='sub'")
    revoked = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie()).json()
    lpr = next(item for item in revoked["analytics"] if item["key"] == "lpr")
    assert lpr["system_paused"] is True and lpr["enabled"] is False


def test_unentitled_enable_is_rejected_but_disable_is_allowed(client):
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/lpr", payload={"enabled": True}, cookies=_cookie()).status_code == 403
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/lpr", payload={"enabled": False}, cookies=_cookie()).status_code == 200


def test_rule_analytic_requires_smart_motion_parent_and_existing_rule(client):
    response = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/line_crossing/assignment", payload={"assigned": True}, cookies=_cookie())
    assert response.status_code == 409  # Smart Motion does not grant a separate Line Crossing seat.
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/line_crossing", payload={"enabled": True}, cookies=_cookie()).status_code == 403


@pytest.mark.parametrize("key", ["line_crossing", "intrusion", "loitering"])
def test_rule_analytic_can_be_reenabled_using_smart_motion_assignment(client, db_path, key):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
                       "VALUES('base-plan','c','camera_slots_hybrid',1,'active','x','x') "
                       "ON CONFLICT(customer_id,product) DO UPDATE SET status='active',camera_slot_quantity=1")
            if not db.execute("SELECT 1 FROM customer_analytics_rules WHERE camera_id='cam-1' AND rule_type=?",
                              (key,)).fetchone():
                db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,1,'x','x')", (f'r-{key}', 'c', 's', 'a', 'cam-1', key, key, '[]'))
    assert _call(client, "PUT", f"/api/customer/analytics-control/cameras/cam-1/{key}",
                 payload={"enabled": False}, cookies=_cookie()).status_code == 200
    enabled = _call(client, "PUT", f"/api/customer/analytics-control/cameras/cam-1/{key}",
                    payload={"enabled": True}, cookies=_cookie())
    assert enabled.status_code == 200, enabled.json()
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert acc.operational_enabled(db, "cam-1", key) is True
            assert db.execute("SELECT 1 FROM camera_analytics_entitlements WHERE camera_id='cam-1' AND analytic_key=?",
                              (key,)).fetchone() is None


def test_people_counting_requires_a_valid_configured_line(client, db_path):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at) "
                       "VALUES('pc-sub','c','s','people_counting','active',1,'x')")
            db.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
                       "VALUES('cam-1','people_counting','active','x','x')")
            db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                       "VALUES('pc-line','c','s','a','cam-1','people_counting','Count','[]',1,'x','x')")
    missing = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/people_counting",
                    payload={"enabled": True}, cookies=_cookie())
    assert missing.status_code == 409
    assert "counting line" in missing.json()["detail"].lower()
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("UPDATE customer_analytics_rules SET geometry_json=? WHERE id='pc-line'",
                       ('[{"x":0.1,"y":0.2},{"x":0.9,"y":0.8}]',))
    ready = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/people_counting",
                  payload={"enabled": True}, cookies=_cookie())
    assert ready.status_code == 200


def test_people_counting_line_validation_rejects_malformed_or_degenerate_geometry(client, db_path):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert acc._people_counting_line_ready(db, "cam-1") is False
            db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                       "VALUES('bad-line','c','s','a','cam-1','people_counting','Bad','not-json',1,'x','x')")
            db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                       "VALUES('degenerate-line','c','s','a','cam-1','people_counting','Same points','[{\"x\":0.5,\"y\":0.5},{\"x\":0.5,\"y\":0.5}]',1,'x','x')")
            assert acc._people_counting_line_ready(db, "cam-1") is False


def test_rule_delivery_honors_individual_switches_preserves_exclusion_and_fails_closed_for_unknown(client, db_path):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,geometry_json,enabled,created_at,updated_at) "
                       "VALUES('r-ex','c','s','a','cam-1','exclusion','Ignore','[]',1,'x','x')")
            rules = [{"id": "r", "camera_id": "cam-1", "rule_type": "line_crossing"},
                     {"id": "r-sec", "camera_id": "cam-1", "rule_type": "security_line"},
                     {"id": "r-ex", "camera_id": "cam-1", "rule_type": "exclusion"},
                     {"id": "r-unknown", "camera_id": "cam-1", "rule_type": "future_rule"}]
            camera = [{"id": "cam-1", "smart_motion_enabled": True, "people_counting_enabled": False}]
            assert [r["id"] for r in acc.filter_rule_items(db, rules, camera)] == ["r", "r-sec", "r-ex"]
            db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-1','line_crossing',0,'x')")
            # The armed Secure Edge alarm is independent of informational
            # Line Crossing, while Smart Motion still gates both.
            assert [r["id"] for r in acc.filter_rule_items(db, rules, camera)] == ["r-sec", "r-ex"]
            assert acc.filter_rule_items(db, rules, [{**camera[0], "smart_motion_enabled": False}]) == []


def _signed_grant(monkeypatch, *, expires_delta=timedelta(hours=2), cameras=None, quantities=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setenv("ANYAICAM_ENV", "test")
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    monkeypatch.setenv("ANYAICAM_TEST_LAB_ENABLED", "true")
    monkeypatch.setenv("ANYAICAM_TEST_LAB_ENVIRONMENT_ID", "lab-ci")
    monkeypatch.setenv("ANYAICAM_TEST_LAB_APPLIANCE_IDS", "a")
    monkeypatch.setenv("ANYAICAM_TEST_LAB_PUBLIC_KEYS_JSON", json.dumps({"ci-key": base64.b64encode(public).decode()}))
    now = datetime.now(timezone.utc)
    payload = {"schema_version": 1, "grant_id": "grant-1", "key_id": "ci-key", "environment_id": "lab-ci",
               "appliance_id": "a", "customer_id": "c", "site_id": "s", "camera_ids": cameras or ["cam-1", "cam-2"],
               "analytics": quantities or {"lpr": 1}, "issued_at": now.isoformat(), "not_before": now.isoformat(),
               "expires_at": (now + expires_delta).isoformat()}
    sig = base64.b64encode(key.sign(acc._canonical(payload))).decode()
    return {"payload": payload, "signature": sig}


def test_test_lab_grant_is_signed_scoped_expiring_and_does_not_create_subscription(client, db_path, monkeypatch):
    # PPE has no commercial entitlement in the seed data, so this exercises
    # the Test Lab's one-camera quota rather than falling back to a purchase.
    envelope = _signed_grant(monkeypatch, quantities={"ppe": 1})
    payload, reason = acc.verify_grant(envelope)
    assert reason == "" and payload["grant_id"] == "grant-1"
    with override_target(sqlite_path=db_path):
        with connection() as db:
            with pytest.raises(PermissionError):
                acc.import_signed_grant(db, envelope, {"role": "customer_owner"})
            acc.import_signed_grant(db, envelope, {"role": "administrator", "email": "admin@example.test"})
            assert db.execute("SELECT COUNT(*) AS n FROM analytics_subscriptions").fetchone()["n"] == 1
    assigned = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/ppe/assignment", payload={"assigned": True}, cookies=_cookie())
    assert assigned.status_code == 200
    enabled = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/ppe", payload={"enabled": True}, cookies=_cookie())
    assert enabled.status_code == 200
    denied = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/ppe", payload={"enabled": True}, cookies=_cookie())
    assert denied.status_code in {403, 409}
    tampered = json.loads(json.dumps(envelope))
    tampered["payload"]["camera_ids"] = ["cam-2"]
    assert acc.verify_grant(tampered)[0] is None


def test_test_lab_fails_closed_in_production_and_after_expiry(monkeypatch):
    envelope = _signed_grant(monkeypatch, expires_delta=timedelta(seconds=-1))
    assert acc.verify_grant(envelope)[0] is None
    monkeypatch.setenv("ANYAICAM_ENV", "production")
    assert acc.verify_grant(envelope)[0] is None
    monkeypatch.delenv("ANYAICAM_ENV")
    assert acc.verify_grant(envelope)[0] is None


def test_test_lab_rejects_extra_fields_and_wrong_scope(monkeypatch):
    envelope = _signed_grant(monkeypatch)
    malformed = {**envelope, "issuer": "browser"}
    assert acc.verify_grant(malformed)[0] is None
    wrong = json.loads(json.dumps(envelope))
    wrong["payload"]["appliance_id"] = "other-appliance"
    assert acc.verify_grant(wrong)[0] is None


def test_test_lab_quota_is_atomic_under_concurrent_camera_assignments(client, db_path, monkeypatch):
    envelope = _signed_grant(monkeypatch, quantities={"ppe": 1})
    with override_target(sqlite_path=db_path):
        with connection() as db:
            acc.import_signed_grant(db, envelope, {"role": "administrator", "email": "admin@example.test"})

    def enable(camera_id):
        return _call(client, "PUT", f"/api/customer/analytics-control/cameras/{camera_id}/ppe/assignment",
                     payload={"assigned": True}, cookies=_cookie()).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(enable, ("cam-1", "cam-2")))
    assert sorted(statuses) == [200, 409]
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert db.execute("SELECT COUNT(*) AS n FROM test_lab_analytics_assignments WHERE grant_id='grant-1' AND analytic_key='ppe' AND enabled=1").fetchone()["n"] == 1


def test_test_lab_revocation_stops_runtime_authorization_without_erasing_preferences(client, db_path, monkeypatch):
    envelope = _signed_grant(monkeypatch, quantities={"ppe": 1})
    actor = {"role": "administrator", "email": "admin@example.test"}
    with override_target(sqlite_path=db_path):
        with connection() as db:
            acc.import_signed_grant(db, envelope, actor)
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/ppe/assignment",
                 payload={"assigned": True}, cookies=_cookie()).status_code == 200
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/ppe",
                 payload={"enabled": True}, cookies=_cookie()).status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert acc.revoke_test_lab_grant(db, "grant-1", actor)
    state = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-2", cookies=_cookie()).json()
    ppe = next(item for item in state["analytics"] if item["key"] == "ppe")
    assert ppe["entitled"] is False and ppe["enabled"] is False
    assert sqlite3.connect(db_path).execute("SELECT enabled FROM camera_analytics_operations WHERE camera_id='cam-2' AND analytic_key='ppe'").fetchone()[0] == 1
    assert sqlite3.connect(db_path).execute("SELECT enabled FROM test_lab_analytics_assignments WHERE camera_id='cam-2' AND analytic_key='ppe'").fetchone()[0] == 0


def test_test_lab_lease_expiry_fails_closed_after_grant_import(client, db_path, monkeypatch):
    envelope = _signed_grant(monkeypatch, expires_delta=timedelta(hours=2), quantities={"ppe": 1})
    with override_target(sqlite_path=db_path):
        with connection() as db:
            acc.import_signed_grant(db, envelope, {"role": "administrator", "email": "admin@example.test"})
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/ppe/assignment",
                 payload={"assigned": True}, cookies=_cookie()).status_code == 200
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/ppe",
                 payload={"enabled": True}, cookies=_cookie()).status_code == 200
    expiry = acc._utc(envelope["payload"]["expires_at"])
    monkeypatch.setattr(acc.time, "time", lambda: expiry + 1)
    state = _call(client, "GET", "/api/customer/analytics-control/cameras/cam-2", cookies=_cookie()).json()
    ppe = next(item for item in state["analytics"] if item["key"] == "ppe")
    assert ppe["entitled"] is False and ppe["enabled"] is False


def test_test_lab_facial_assignment_reaches_runtime_guard_and_turns_off(client, db_path, monkeypatch):
    envelope = _signed_grant(monkeypatch, quantities={"facial_recognition": 1})
    with override_target(sqlite_path=db_path):
        with connection() as db:
            acc.import_signed_grant(db, envelope, {"role": "administrator", "email": "admin@example.test"})
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/facial_recognition/assignment",
                 payload={"assigned": True}, cookies=_cookie()).status_code == 200
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/facial_recognition",
                 payload={"enabled": True}, cookies=_cookie()).status_code == 200
    import facial_events
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert facial_events._is_entitled(db, camera_id="cam-2") is True
    assert _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-2/facial_recognition",
                 payload={"enabled": False}, cookies=_cookie()).status_code == 200
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert facial_events._is_entitled(db, camera_id="cam-2") is True
            assert acc.operational_enabled(db, "cam-2", "facial_recognition") is False


def test_only_authenticated_administrator_can_import_or_revoke_signed_grants(client, db_path, monkeypatch):
    envelope = _signed_grant(monkeypatch, quantities={"ppe": 1})
    route = "/api/admin/test-lab/grants"
    assert _call(client, "POST", route, payload=envelope, cookies=_cookie()).status_code == 403
    imported = _call(client, "POST", route, payload=envelope,
                     cookies=_cookie("admin@example.test", "administrator"))
    assert imported.status_code == 200 and imported.json()["grant_id"] == "grant-1"
    assert _call(client, "POST", f"{route}/grant-1/revoke",
                 cookies=_cookie("owner@example.test", "customer_owner")).status_code == 403
    revoked = _call(client, "POST", f"{route}/grant-1/revoke",
                    cookies=_cookie("admin@example.test", "administrator"))
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    with override_target(sqlite_path=db_path):
        with connection() as db:
            actions = [row["action"] for row in db.execute("SELECT action FROM audit_logs ORDER BY id").fetchall()]
    assert actions == ["test_lab.grant_imported", "test_lab.grant_revoked"]


def test_guardian_has_no_enabled_adapter_or_invented_capabilities():
    # The page intentionally reports zero capabilities until an authenticated
    # Guardian runtime manifest and approved API contract exist.
    assert not hasattr(acc, "guardian_worker")
    assert not hasattr(acc, "activate_guardian")


def test_all_analytics_off_skips_model_load_and_inference_without_touching_recording(monkeypatch):
    import main

    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main, "cv2", object())
    monkeypatch.setattr(main, "YOLO", object())
    loaded, inferred = [], []
    monkeypatch.setattr(main, "get_yolo_model", lambda: loaded.append(True))
    monkeypatch.setattr(main, "detect_objects_frame", lambda camera: inferred.append(camera))
    # Production startup creates per-camera detector state in
    # reconcile_camera_workers() before scheduling ai_person_detector().
    # The direct-generator test bypasses that lifecycle, so seed the same
    # state with the production factory rather than relying on test order.
    monkeypatch.setitem(main.ai_detection_state, 1, main._new_ai_state())
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: {
        "smart_motion_enabled": False, "people_counting_enabled": False, "lpr_enabled": False,
        "ppe_enabled": False, "facial_recognition_enabled": False,
    })
    class YieldOnce:
        def __await__(self):
            yield None
    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: object())
    monkeypatch.setattr(main.asyncio, "sleep", lambda _seconds: YieldOnce())
    detector = main.ai_person_detector(1)
    assert detector.send(None) is None
    detector.close()
    assert loaded == [] and inferred == []
    assert main.ai_detection_state[1]["status"] == "paused"
    assert main.ai_detection_state[1]["pause_reason"] == "analytics_disabled"


def test_expired_lab_lease_is_clamped_in_cached_appliance_identity(monkeypatch):
    import recording_uploader
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    cached = {"lpr_enabled": True, "ppe_enabled": True,
              "analytics_lab_leases": {"lpr": past, "ppe": future}}
    monkeypatch.setattr(recording_uploader, "_camera_map", {1: cached})
    identity = recording_uploader._camera_identity(1)
    assert identity["lpr_enabled"] is False
    assert identity["ppe_enabled"] is True
    assert cached["lpr_enabled"] is True  # cached source object is not mutated
