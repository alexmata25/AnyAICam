"""Admin Analytics entitlements use the real per-camera system (2026-09-25).
The page used to write customer_camera_features.json, which nothing that
runs analytics reads. It now reads/writes camera_analytics_entitlements
through customer_analytics_panel (same license cap as the customer
route), stays master-admin only, and records an audit entry per change.
The legacy store and its customer write routes are gone."""
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import customer_platform
import main
from database_backend import override_target
from partner_db import connection, initialize_database


def _endpoint(path, method):
    for route in main.app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or set()):
            return route.endpoint
    raise AssertionError(f"no {method} {path}")


def _cell(fn, name):
    for var, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
        if var == name:
            return cell
    raise AssertionError(name)


@pytest.fixture()
def admin(tmp_path):
    db_path = tmp_path / "admin_ent.db"
    put = _endpoint("/api/admin/analytics-entitlements", "PUT")
    get = _endpoint("/api/admin/analytics-entitlements", "GET")
    gate, audit = _cell(put, "_require_master_admin"), _cell(put, "record_audit")
    saved = (gate.cell_contents, audit.cell_contents)
    state = {"admin": True, "audits": []}

    def fake_gate(request):
        if not state["admin"]:
            raise HTTPException(status_code=403, detail="Master administrator required.")
        return {"id": "local-admin"}

    gate.cell_contents = fake_gate
    audit.cell_contents = lambda request, action, target, detail: state["audits"].append((action, target, detail))
    try:
        with override_target(sqlite_path=db_path):
            initialize_database()
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust','p1','Acme','a@e.test','active','x')")
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s1','cust','HQ','x')")
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-1','cust','s1','Gate','configured',1,'x')")
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-2','cust','s1','Dock','configured',2,'x')")
            conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,licensed_quantity,status,created_at,updated_at) "
                         "VALUES('sub-1','cust','s1','lpr',1,'active','x','x')")
            conn.commit()
            conn.close()
            yield SimpleNamespace(put=put, get=get, state=state)
    finally:
        gate.cell_contents, audit.cell_contents = saved


def _payload(camera, key, enabled):
    return customer_platform.AdminEntitlementChange(camera_id=camera, analytic_key=key, enabled=enabled)


def _enabled(camera, key):
    with connection() as db:
        return db.execute("SELECT 1 FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=? AND status='active'",
                          (camera, key)).fetchone() is not None


def test_admin_writes_the_real_per_camera_entitlement_and_audits(admin):
    result = admin.put(_payload("cam-1", "lpr", True), SimpleNamespace())
    assert result["enabled"] is True and _enabled("cam-1", "lpr")
    assert admin.state["audits"][-1][1] == "camera-analytics:cam-1:lpr"
    admin.put(_payload("cam-1", "lpr", False), SimpleNamespace())
    assert not _enabled("cam-1", "lpr")


def test_admin_respects_the_purchased_license_cap(admin):
    admin.put(_payload("cam-1", "lpr", True), SimpleNamespace())
    with pytest.raises(HTTPException) as error:
        admin.put(_payload("cam-2", "lpr", True), SimpleNamespace())
    assert error.value.status_code == 409 and not _enabled("cam-2", "lpr")


def test_state_listing_matches_the_real_store(admin):
    assert [c["id"] for c in admin.get(SimpleNamespace(), customer_id="")["customers"]] == ["cust"]
    admin.put(_payload("cam-1", "lpr", True), SimpleNamespace())
    cameras = admin.get(SimpleNamespace(), customer_id="cust")["cameras"]
    gate = next(c for c in cameras if c["id"] == "cam-1")
    assert gate["name"] == "Gate" and gate["site"] == "HQ"
    assert {a["key"]: a["enabled"] for a in gate["analytics"]}["lpr"] is True
    with pytest.raises(HTTPException) as error:
        admin.get(SimpleNamespace(), customer_id="nobody")
    assert error.value.status_code == 404


def test_non_admins_and_bad_input_are_refused(admin):
    with pytest.raises(HTTPException) as error:
        admin.put(_payload("cam-1", "not_a_thing", True), SimpleNamespace())
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        admin.put(_payload("missing", "lpr", True), SimpleNamespace())
    assert error.value.status_code == 404
    admin.state["admin"] = False
    with pytest.raises(HTTPException) as error:
        admin.put(_payload("cam-1", "lpr", True), SimpleNamespace())
    assert error.value.status_code == 403 and not _enabled("cam-1", "lpr")


def test_legacy_store_and_routes_are_gone():
    for name in ("FEATURES_FILE", "ALERTS_FILE", "_features", "_alerts", "EntitlementUpdate"):
        assert not hasattr(customer_platform, name), name
    paths = {(getattr(r, "path", None), m) for r in main.app.routes for m in (getattr(r, "methods", None) or ())}
    assert ("/api/customer/cameras/{camera_id}/features", "PUT") not in paths
    assert ("/api/customer/cameras/{camera_id}/alerts", "PUT") not in paths
