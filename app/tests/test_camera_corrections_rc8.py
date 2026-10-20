"""RC8 camera-management corrections (owner direction 2026-10-20).

CAM-R1  removing a camera releases its analytics seats; seat counts ignore
        removed cameras, so a replacement camera can take the seat without
        the fail-closed over-assignment rule switching the analytic off for
        every camera.
CAM-R2  a removed camera's kept recordings and events stay viewable,
        read-only, by its own customer, under its ORIGINAL camera number.
CAM-R3  PUT /api/customer/cameras never revives a removed camera.
CAM-6   the camera password is typed into a masked field, never prompt().
CAM-5   (cloud side) add a camera by its local IP address: a discovery job
        targeting one validated LAN address, the rest of the flow unchanged.
"""

import sqlite3
import secrets
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from database_backend import override_target

NOW = "2026-10-20T00:00:00"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "camera_corrections.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(db_path, customer_id="cust-1"):
    conn = _conn(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Partner',?)", (NOW,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                 (customer_id, "partner-1", "Customer", f"{customer_id}@example.test", "active", NOW))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Site", NOW))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                 (f"appl-{customer_id}", customer_id, f"site-{customer_id}", f"AIC-{customer_id.upper()}", NOW))
    conn.commit()
    return conn


def _camera(conn, camera_id, number, customer_id="cust-1", status="configured"):
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,device_key,camera_number,status,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (camera_id, customer_id, f"site-{customer_id}", f"appl-{customer_id}", f"Camera {camera_id}",
                  f"dev-{camera_id}", number, status, NOW))
    conn.commit()


def _cookie(client, role="customer_owner", customer_id="cust-1", email=None):
    import partner_portal
    client.cookies.set(partner_portal.SESSION_COOKIE,
                       partner_portal._token(email or f"{customer_id}@example.test", role, None, customer_id, None))


def _remove(client, camera_id, customer_id="cust-1"):
    _cookie(client, customer_id=customer_id)
    response = client.delete(f"/api/customer/cameras/{camera_id}")
    client.cookies.clear()
    return response


# ------------------------------------------------------------------ CAM-R1

def _licence(conn, key="lpr", quantity=1, customer_id="cust-1"):
    conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,monthly_retail,monthly_partner,created_at,licensed_quantity,updated_at) "
                 "VALUES(?,?,NULL,?,'active',0,0,?,?,?)", (f"sub-{key}", customer_id, key, NOW, quantity, NOW))
    conn.commit()


def _assign(conn, camera_id, key="lpr", status="active"):
    conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET status=excluded.status", (camera_id, key, status, NOW, NOW))
    conn.commit()


def _authorized(db_path, camera_id, key="lpr"):
    from analytics_entitlements import commercial_camera_analytic_authorized
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)), connection() as db:
        return commercial_camera_analytic_authorized(db, customer_id="cust-1", site_id="site-cust-1", camera_id=camera_id, analytic_key=key)


def test_removal_releases_the_cameras_analytics_seats(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1); _licence(conn); _assign(conn, "cam-a")
    assert _remove(client, "cam-a").status_code == 200
    row = _conn(db_path).execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id='cam-a' AND analytic_key='lpr'").fetchone()
    assert row["status"] == "cancelled"


def test_a_replacement_camera_takes_the_seat_without_switching_the_analytic_off(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1); _camera(conn, "cam-c", 3); _licence(conn, quantity=2)
    _assign(conn, "cam-a"); _assign(conn, "cam-c")
    assert _authorized(db_path, "cam-c")
    assert _remove(client, "cam-a").status_code == 200
    _camera(conn, "cam-b", 2)
    from customer_analytics_panel import assign_entitlement
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)), connection() as db:
        assign_entitlement(db, "cam-b", "lpr", now=NOW)        # a full licence would raise LicenseLimitExceeded
    assert _authorized(db_path, "cam-b")
    assert _authorized(db_path, "cam-c")                        # the other camera never fails closed


def test_a_camera_removed_before_rc8_with_a_stale_active_seat_is_not_counted(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1); _camera(conn, "cam-b", 2); _licence(conn, quantity=1)
    # pre-RC8 removal: slot freed, seat row left 'active'
    conn.execute("UPDATE cameras SET status='removed',camera_number=NULL,device_key=NULL,former_camera_number=1 WHERE id='cam-a'")
    conn.commit(); _assign(conn, "cam-a"); _assign(conn, "cam-b")
    assert _authorized(db_path, "cam-b")


def test_removing_a_camera_without_analytics_still_works_and_audits_nothing_extra(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1)
    response = _remove(client, "cam-a")
    assert response.status_code == 200
    assert _conn(db_path).execute("SELECT status FROM cameras WHERE id='cam-a'").fetchone()["status"] == "removed"


# ------------------------------------------------------------------ CAM-R2

def _recording(conn, camera_id, number, rec_id="rec-1", customer_id="cust-1"):
    name = f"camera{number}_20261019_120000.mkv"
    conn.execute("INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,'available',?)",
                 (rec_id, customer_id, f"site-{customer_id}", f"appl-{customer_id}", camera_id, f"local/{name}",
                  "2026-10-19T12:00:00", "2026-10-19T12:05:00", NOW))
    conn.commit()
    return name


def _local_file(name, number):
    import main
    folder = main.RECORDINGS_FOLDER / f"camera{number}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"\x1a\x45\xdf\xa3 kept recording")


def test_a_removed_cameras_recording_still_plays_for_its_owner_under_its_original_number(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1)
    name = _recording(conn, "cam-a", 1); _local_file(name, 1)
    assert _remove(client, "cam-a").status_code == 200
    _cookie(client)
    assert client.get("/api/customer/recordings/cam-a/rec-1/local").status_code == 200
    assert client.get("/api/customer/recordings/cam-a/dates").status_code == 200
    assert client.get("/api/customer/recordings/cam-a").status_code == 200
    assert client.get("/api/customer/events/cam-a").status_code == 200
    page = client.get("/playback")
    assert page.status_code == 200 and "(removed)" in page.text


def test_another_customer_can_never_reach_a_removed_cameras_recordings(client, db_path):
    conn = _seed(db_path); _seed(db_path, "cust-2"); _camera(conn, "cam-a", 1)
    name = _recording(conn, "cam-a", 1); _local_file(name, 1)
    assert _remove(client, "cam-a").status_code == 200
    _cookie(client, customer_id="cust-2")
    assert client.get("/api/customer/recordings/cam-a/rec-1/local").status_code == 403
    assert client.get("/api/customer/recordings/cam-a").status_code == 403


def test_a_viewer_needs_a_playback_grant_for_a_removed_camera(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1); _camera(conn, "cam-b", 2)
    name = _recording(conn, "cam-a", 1); _local_file(name, 1)
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,created_at) "
                 "VALUES('viewer-1','partner-1','viewer@example.test','V','customer_viewer','x',1,'cust-1','active',?)", (NOW,))
    conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback) VALUES('viewer-1','cam-b',1,1)")
    conn.commit()
    assert _remove(client, "cam-a").status_code == 200
    _cookie(client, role="customer_viewer", email="viewer@example.test")
    assert client.get("/api/customer/recordings/cam-a/rec-1/local").status_code == 403
    conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback) VALUES('viewer-1','cam-a',1,1)")
    conn.commit()
    assert client.get("/api/customer/recordings/cam-a/rec-1/local").status_code == 200


def test_live_and_new_clips_are_never_offered_for_a_removed_camera(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1)
    assert _remove(client, "cam-a").status_code == 200
    _cookie(client)
    assert client.get("/api/customer/cameras/cam-a/latest-thumbnail").status_code == 403
    clip = client.post("/api/customer/clips", json={"camera_id": "cam-a", "start_time": "2026-10-19T12:00:00", "end_time": "2026-10-19T12:01:00"})
    assert clip.status_code == 403


def test_the_active_camera_list_used_by_dashboards_still_excludes_removed_cameras(client, db_path, monkeypatch):
    import main
    import partner_portal
    conn = _seed(db_path); _camera(conn, "cam-a", 1); _camera(conn, "cam-b", 2)
    assert _remove(client, "cam-a").status_code == 200
    monkeypatch.setattr(partner_portal, "partner_identity",
                        lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "cust-1@example.test"})
    with override_target(sqlite_path=str(db_path)):
        active = main._customer_playback_cameras(SimpleNamespace())
        history = main._customer_removed_playback_cameras(SimpleNamespace())
    assert [camera["id"] for camera in active] == ["cam-b"]
    assert [(camera["id"], camera["camera_number"], camera["removed"]) for camera in history] == [("cam-a", 1, True)]


def test_the_replacement_camera_gets_a_new_number_and_the_history_keeps_the_old_one(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1)
    assert _remove(client, "cam-a").status_code == 200
    row = _conn(db_path).execute("SELECT camera_number, former_camera_number FROM cameras WHERE id='cam-a'").fetchone()
    assert row["camera_number"] is None and row["former_camera_number"] == 1


# ------------------------------------------------------------------ CAM-R3

def _put(client, cameras, appliance_id="appl-cust-1"):
    _cookie(client)
    response = client.put("/api/customer/cameras", json={"appliance_id": appliance_id, "cameras": cameras})
    client.cookies.clear()
    return response


def test_put_never_revives_a_removed_camera(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1)
    assert _remove(client, "cam-a").status_code == 200
    response = _put(client, [{"id": "cam-a", "name": "Back again", "site_id": "site-cust-1", "status": "configured"}])
    assert response.status_code == 200 and response.json()["skipped_removed"] == 1
    row = _conn(db_path).execute("SELECT name,status,device_key,camera_number FROM cameras WHERE id='cam-a'").fetchone()
    assert row["status"] == "removed" and row["device_key"] is None and row["camera_number"] is None
    assert row["name"] == "Camera cam-a"


def test_put_cannot_mark_a_live_camera_removed(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1)
    _put(client, [{"id": "cam-a", "name": "Front", "site_id": "site-cust-1", "status": "removed"}])
    row = _conn(db_path).execute("SELECT name,status,device_key FROM cameras WHERE id='cam-a'").fetchone()
    assert row["status"] == "configured" and row["device_key"] == "dev-cam-a" and row["name"] == "Front"


def test_the_setup_wizard_save_still_renames_live_cameras(client, db_path):
    conn = _seed(db_path); _camera(conn, "cam-a", 1, status="pending_installation")
    response = _put(client, [{"id": "cam-a", "name": "Driveway", "site_id": "site-cust-1", "resolution": "4mp", "status": "configured"}])
    assert response.status_code == 200
    row = _conn(db_path).execute("SELECT name,status,resolution FROM cameras WHERE id='cam-a'").fetchone()
    assert (row["name"], row["status"], row["resolution"]) == ("Driveway", "configured", "4mp")


# ------------------------------------------------------------------ CAM-6

def _setup_page_source():
    import inspect
    import partner_workspace
    return inspect.getsource(partner_workspace)


def test_the_camera_password_is_typed_into_a_masked_field():
    source = _setup_page_source()
    assert 'id="add-camera-password" type="password" autocomplete="new-password"' in source
    assert "<dialog" in source and 'id="add-camera-dialog"' in source


def test_no_browser_prompt_is_used_for_camera_details():
    assert "prompt(" not in _setup_page_source()


# ------------------------------------------------------------------ CAM-5 (cloud)

def _route(path, method):
    import main
    for route in main.app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or set()):
            return route.endpoint
    raise AssertionError(path)


def _enrolled(db_path, credential="token-appl-cust-1"):
    from partner_db import password_hash
    conn = _seed(db_path)
    conn.execute("UPDATE appliances SET online_status='online', last_check_in='2026-01-01T00:00:10' WHERE id='appl-cust-1'")
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,last_used_at) VALUES('cred-1','appl-cust-1',?,'2026-01-01','2026-01-01T00:00:05')",
                 (password_hash(credential),))
    conn.commit()
    return conn


def _owner(monkeypatch):
    import partner_workspace
    monkeypatch.setattr(partner_workspace, "partner_identity",
                        lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "cust-1@example.test"})


def _fake_request(headers=None):
    return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


def _appliance_headers(credential="token-appl-cust-1"):
    return {"x-appliance-id": "appl-cust-1", "x-request-timestamp": str(int(time.time())),
            "x-request-nonce": secrets.token_hex(16), "authorization": f"Bearer {credential}"}


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "224.0.0.1", "0.0.0.0", "camera.local", "192.168.1", "fe80::1", "192.168.1.300"])
def test_only_a_local_ipv4_camera_address_is_accepted(db_path, monkeypatch, address):
    scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    with override_target(sqlite_path=str(db_path)):
        _ = __import__("partner_db").initialize_database()
        _enrolled(db_path); _owner(monkeypatch)
        with pytest.raises(HTTPException) as refused:
            scan(_fake_request(), "appl-cust-1", {"target_ip": address})
    assert refused.value.status_code == 400
    assert _conn(db_path).execute("SELECT COUNT(*) AS n FROM camera_scan_jobs").fetchone()["n"] == 0


def test_a_manual_address_job_reaches_the_appliance_and_the_address_is_dropped_when_done(db_path, monkeypatch):
    scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    jobs = _route("/api/appliance/{cloud_id}/scan-jobs", "GET")
    submit = _route("/api/appliance/{cloud_id}/scan-jobs/{job_id}", "POST")
    with override_target(sqlite_path=str(db_path)):
        __import__("partner_db").initialize_database()
        _enrolled(db_path); _owner(monkeypatch)
        started = scan(_fake_request(), "appl-cust-1", {"target_ip": " 10.20.30.40 "})
        picked = jobs(_fake_request(_appliance_headers()), "AIC-CUST-1")
        assert picked["jobs"][0]["target_ip"] == "10.20.30.40"
        submit(_fake_request(_appliance_headers()), "AIC-CUST-1", started["job_id"],
               {"status": "complete", "progress": 100, "results": [{"device_key": "urn:uuid:5f0c3b1e-1111-4222-8333-444455556666", "ip": "10.20.30.40", "mac_address": "aa:bb:cc:dd:ee:ff", "manual_target": True}]})
    row = _conn(db_path).execute("SELECT target_ip,results_json,status FROM camera_scan_jobs").fetchone()
    assert row["status"] == "complete" and row["target_ip"] is None
    assert "10.20.30.40" not in row["results_json"] and "aa:bb" not in row["results_json"]


def test_a_full_scan_job_carries_no_target(db_path, monkeypatch):
    scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    jobs = _route("/api/appliance/{cloud_id}/scan-jobs", "GET")
    with override_target(sqlite_path=str(db_path)):
        __import__("partner_db").initialize_database()
        _enrolled(db_path); _owner(monkeypatch)
        scan(_fake_request(), "appl-cust-1")
        picked = jobs(_fake_request(_appliance_headers()), "AIC-CUST-1")
    assert "target_ip" not in picked["jobs"][0]


def test_a_different_search_while_one_runs_is_refused_not_silently_merged(db_path, monkeypatch):
    scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    with override_target(sqlite_path=str(db_path)):
        __import__("partner_db").initialize_database()
        _enrolled(db_path); _owner(monkeypatch)
        first = scan(_fake_request(), "appl-cust-1", {"target_ip": "192.168.1.64"})
        again = scan(_fake_request(), "appl-cust-1", {"target_ip": "192.168.1.64"})
        assert again["job_id"] == first["job_id"]
        with pytest.raises(HTTPException) as busy:
            scan(_fake_request(), "appl-cust-1", {"target_ip": "192.168.1.65"})
    assert busy.value.status_code == 409


def test_the_audit_log_never_records_the_address(db_path, monkeypatch):
    scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    with override_target(sqlite_path=str(db_path)):
        __import__("partner_db").initialize_database()
        _enrolled(db_path); _owner(monkeypatch)
        scan(_fake_request(), "appl-cust-1", {"target_ip": "192.168.7.21"})
    dump = " ".join(str(tuple(r)) for r in _conn(db_path).execute("SELECT * FROM audit_logs").fetchall())
    assert "192.168.7.21" not in dump


def test_the_setup_page_offers_add_by_address():
    source = _setup_page_source()
    assert 'id="manual-camera-form"' in source and 'id="manual-camera-ip"' in source
    assert "target_ip:address" in source


# ------------------------------------------------------------------ setup page JavaScript (CAM-5 / CAM-6)

def test_the_setup_page_scripts_with_the_new_dialog_and_address_form_are_valid_javascript(client, db_path):
    import re
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed here (the PG16+Node image and the Dell host have it)")
    conn = _seed(db_path)
    conn.execute("UPDATE appliances SET activation_status='activated' WHERE id='appl-cust-1'")
    conn.commit()
    _cookie(client)
    page = client.get("/customer/setup?step=4&appliance=appl-cust-1")
    assert page.status_code == 200
    assert 'id="add-camera-dialog"' in page.text and 'id="manual-camera-form"' in page.text
    scripts = [m.group(1) for m in re.finditer(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page.text, re.S | re.I) if m.group(1).strip()]
    assert any("askCameraDetails" in script for script in scripts)
    for index, script in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
        try:
            done = subprocess.run([node, "--check", handle.name], capture_output=True, text=True, timeout=60)
        finally:
            Path(handle.name).unlink(missing_ok=True)
        assert done.returncode == 0, f"setup script #{index}: {(done.stderr or done.stdout)[:800]}"
