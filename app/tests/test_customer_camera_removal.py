"""Customer camera removal must archive history and release only the active slot."""

import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
import appliance_cloud
import partner_portal
import partner_workspace
import live_view_page
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "camera-removal.db"
    with override_target(sqlite_path=path):
        initialize_database()
    return path


def _route(path, method="GET"):
    for route in main.app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or {method}):
            return route.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _request():
    return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda key, default=None: default))


def _identity(customer_id="cust-1", role="customer_owner"):
    return {"role": role, "customer_id": customer_id, "email": "owner@example.test"}


def _seed_tenant(conn, customer_id="cust-1", appliance_id="appl-1", email="owner@example.test"):
    partner_id = f"partner-{customer_id}"
    site_id = f"site-{customer_id}"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, customer_id, email, "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, customer_id, site_id, f"CLOUD-{customer_id}", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
        "VALUES('user-1',?,?,?,'customer_owner','test-hash',1,?,'2026-01-01')",
        (partner_id, email, "Test Owner", customer_id),
    )
    return site_id


def _seed_camera(conn, camera_id="cam-1", *, customer_id="cust-1", appliance_id="appl-1", number=4, device_key="device-1", name="Bedroom camera #4"):
    site_id = f"site-{customer_id}"
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number,device_key,onvif_endpoint) "
        "VALUES(?,?,?,?,?,'configured','2026-01-01',?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, name, number, device_key, "rtsp://192.0.2.10/stream"),
    )
    conn.execute(
        "INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)",
        (camera_id, b"encrypted-camera-secret", "2026-01-01", "2026-01-01"),
    )


def _seed_history(conn, camera_id="cam-1"):
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES('rec-1','cust-1','site-cust-1','appl-1',?,'recordings/cust-1/site-cust-1/appl-1/cam-1/clip.mkv',"
        "'2026-01-01T00:00:00','2026-01-01T00:05:00','available','2026-01-01T00:05:00')",
        (camera_id,),
    )
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-1','cust-1','site-cust-1','appl-1',?,'local-evt-1','motion','2026-01-01T00:01:00','2026-01-01T00:01:00')",
        (camera_id,),
    )
    conn.execute(
        "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_type,severity,title,message,timestamp,created_at) "
        "VALUES('thumb-1','user-1','cust-1','site-cust-1',?,'motion','info','Motion','Motion event','2026-01-01T00:01:00','2026-01-01T00:01:00')",
        (camera_id,),
    )
    conn.execute("UPDATE notifications SET thumbnail='recordings/cust-1/site-cust-1/appl-1/cam-1/thumb.jpg' WHERE id='thumb-1'")
    conn.execute(
        "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
        "VALUES('owner@example.test','customer_owner','camera.added','camera',?,'{}','2026-01-01T00:00:00')",
        (camera_id,),
    )


def test_remove_archives_camera_and_preserves_playback_events_thumbnails_and_slot_quantity(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        conn.execute("INSERT INTO plans(id,customer_id,camera_quantity,created_at) VALUES('plan-1','cust-1',2,'2026-01-01')")
        _seed_camera(conn)
        _seed_camera(conn, "cam-2", number=5, device_key="device-2", name="Living room")
        _seed_history(conn)
        conn.commit()

        body = remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        assert body["status"] == "removed"
        assert body["camera_capacity"] == {"configured_camera_count": 1, "licensed_camera_slots": 2}

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        camera = conn.execute("SELECT status,camera_number,former_camera_number,device_key FROM cameras WHERE id='cam-1'").fetchone()
        assert dict(camera) == {"status": "removed", "camera_number": None, "former_camera_number": 4, "device_key": "device-1"}
        assert conn.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id='cam-1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM recordings WHERE camera_id='cam-1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM detection_events WHERE camera_id='cam-1'").fetchone()[0] == 1
        notification = conn.execute("SELECT thumbnail FROM notifications WHERE camera_id='cam-1'").fetchone()
        assert notification[0] == "recordings/cust-1/site-cust-1/appl-1/cam-1/thumb.jpg"
        assert conn.execute("SELECT COUNT(*) FROM cameras WHERE id='cam-2' AND status='configured' AND camera_number=5").fetchone()[0] == 1
        assert conn.execute("SELECT camera_quantity FROM plans WHERE customer_id='cust-1'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='camera.removed' AND entity_id='cam-1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='camera.added' AND entity_id='cam-1'").fetchone()[0] == 1

        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _identity())
        cameras = main._customer_playback_cameras(SimpleNamespace())
        assert any(item["id"] == "cam-1" and item["camera_number"] == 4 for item in cameras)
        with patch.object(main, "_presigned_recording_url", return_value="https://media.example.test/signed"):
            assert main._customer_camera_recordings("cam-1")[0]["url"] == "https://media.example.test/signed"
        from partner_db import connection
        with connection() as db:
            live_cameras = live_view_page._customer_live_cameras(db, _identity())
            assert {camera["id"] for camera in live_cameras} == {"cam-2"}
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as live_denied:
                live_view_page._authorized_camera(db, "cam-1", _identity())
            assert live_denied.value.status_code == 404


def test_remove_requires_explicit_confirmation_and_is_idempotent(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn)
        conn.commit()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as missing:
            remove_camera(_request(), "cam-1", {})
        assert missing.value.status_code == 400
        removed = remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        retried = remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        assert removed["status"] == retried["status"] == "removed"
        assert retried["already_removed"] is True
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='camera.removed'").fetchone()[0] == 1


def test_removal_failure_rolls_back_archive_and_credential_change(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn)
        conn.execute(
            "CREATE TRIGGER reject_camera_remove_audit BEFORE INSERT ON audit_logs "
            "WHEN NEW.action='camera.removed' BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
        conn.commit()
        with pytest.raises(Exception):
            remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT status,camera_number FROM cameras WHERE id='cam-1'").fetchone() == ("configured", 4)
        assert conn.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id='cam-1'").fetchone()[0] == 1


def test_stream_stop_failure_rolls_back_removal_and_preserves_credentials(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    monkeypatch.setattr(main, "stop_camera_processes", lambda number: (_ for _ in ()).throw(subprocess.TimeoutExpired("ffmpeg", 5)))
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn)
        conn.commit()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as failed:
            remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        assert failed.value.status_code == 503
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT status,camera_number FROM cameras WHERE id='cam-1'").fetchone() == ("configured", 4)
        assert conn.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id='cam-1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='camera.removed'").fetchone()[0] == 0


def test_foreign_owner_cannot_remove_camera(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity("cust-2"))
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_tenant(conn, "cust-2", "appl-2", "other@example.test")
        _seed_camera(conn)
        conn.commit()
        conn.close()
        from db_migrations import apply_migrations
        apply_migrations()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as response:
            remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        assert response.value.status_code == 404
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT status FROM cameras WHERE id='cam-1'").fetchone()[0] == "configured"


def test_customer_viewer_cannot_remove_camera(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity(role="customer_viewer"))
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn)
        conn.commit()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as denied:
            remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        assert denied.value.status_code == 403
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT status FROM cameras WHERE id='cam-1'").fetchone()[0] == "configured"


def test_camera_worker_stop_targets_only_removed_camera_slot(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.running = True
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminated = True
            self.running = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True
            self.running = False

    removed_live, removed_recording, unaffected = FakeProcess(), FakeProcess(), FakeProcess()
    monkeypatch.setattr(
        main,
        "_ffmpeg_process_camera_numbers",
        {removed_live: 4, removed_recording: 4, unaffected: 5},
    )
    assert main.stop_camera_processes(4) == 2
    assert removed_live.terminated and removed_recording.terminated
    assert not unaffected.terminated


def test_archived_device_identity_cannot_be_registered_under_another_customer(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    request_provision = _route("/api/customer/cameras/provision", "POST")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_tenant(conn, "cust-2", "appl-2", "other@example.test")
        _seed_camera(conn)
        conn.commit()
        remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})

        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity("cust-2"))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as response:
            request_provision(_request(), {"appliance_id": "appl-2", "device_key": "DEVICE-1", "name": "Duplicate camera"})
        assert response.value.status_code == 409
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM cameras WHERE device_key='device-1'").fetchone()[0] == 1


def test_removed_camera_is_not_returned_as_active_appliance_configuration(db_path, monkeypatch):
    remove_camera = _route("/api/customer/cameras/{camera_id}", "DELETE")
    appliance_configuration = _route("/api/appliance/configuration", "GET")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    monkeypatch.setattr(appliance_cloud, "authenticate_appliance", lambda request: {"id": "appl-1"})
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn)
        conn.commit()
        remove_camera(_request(), "cam-1", {"confirmation": "REMOVE"})
        # Exercise the real config endpoint; history remains addressable by ID.
        assert appliance_configuration(_request())["cameras"] == []


def test_customer_management_page_exposes_lifecycle_controls_only_to_camera_owner(db_path, monkeypatch):
    customer_account = _route("/customer-account", "GET")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity())
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        conn.execute("UPDATE appliances SET activation_status='activated' WHERE id='appl-1'")
        _seed_camera(conn)
        conn.commit()
        owner = customer_account(_request())
        owner_html = owner.body.decode() if hasattr(owner, "body") else str(owner)
        assert "Camera Management" in owner_html
        assert "Remove camera" in owner_html
        assert "Transfer camera" in owner_html
        assert "Replace camera" in owner_html
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity(role="customer_viewer"))
        viewer = customer_account(_request())
        viewer_html = viewer.body.decode() if hasattr(viewer, "body") else str(viewer)
        assert "Remove camera" not in viewer_html
        assert "Transfer camera" not in viewer_html
        assert "Replace camera" not in viewer_html
