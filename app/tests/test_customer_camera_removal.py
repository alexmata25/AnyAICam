"""DELETE /api/customer/cameras/{camera_id}: removing an active camera
must free its licensed slot WITHOUT destroying historical data.

Found and fixed a real, previously-untested bug: this route used to run
`DELETE FROM cameras WHERE id=?`, even though recordings, detection_events,
and customer_clip_jobs all carry a real `FOREIGN KEY(camera_id) REFERENCES
cameras(id)` (db_migrations.py) -- either orphaning every historical row's
camera_id reference or violating the FK outright, exactly the "camera
removal must not destroy historical recordings/events/audit history"
requirement. Fixed to a soft removal: device_key/camera_number cleared
(frees the slot -- device_key IS NOT NULL is exactly what quota
enforcement and configured_camera_count both count) and status becomes
'removed', but the camera row itself -- and every historical row that
references its id -- is kept permanently.

Real HTTP throughout (TestClient(main.app)).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_camera_removal.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant_with_camera(db_path, *, customer_id="cust-1", camera_id="cam-1", device_key="dev-1", camera_number=1, partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", "removal-test@example.test", "active", "2026-01-01"),
        )
        site_id = f"site-{customer_id}"
        appliance_id = f"appl-{customer_id}"
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
            (appliance_id, customer_id, site_id, f"AIC-{customer_id.upper()}", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,device_key,camera_number,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (camera_id, customer_id, site_id, appliance_id, "Front Door", device_key, camera_number, "configured", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)",
            (camera_id, "encrypted-stuff", "2026-01-01", "2026-01-01"),
        )
        conn.commit()


def _seed_history(db_path, *, customer_id="cust-1", camera_id="cam-1"):
    """One recording and one detection_event referencing camera_id,
    matching the real FK-carrying tables removal must never orphan."""
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,created_at) "
            "VALUES('rec-1',?,?,?,?,?,?,?,?)",
            (customer_id, "site-cust-1", "appl-cust-1", camera_id, "recordings/rec-1.mp4", "2026-01-01T00:00:00", "2026-01-01T00:05:00", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
            "VALUES('evt-1',?,?,?,?,?,?,?,?)",
            (customer_id, "site-cust-1", "appl-cust-1", camera_id, "local-evt-1", "motion", "2026-01-01T00:02:00", "2026-01-01"),
        )
        conn.commit()


def _owner_cookie(customer_id="cust-1"):
    import partner_portal
    return partner_portal._token("removal-test@example.test", "customer_owner", None, customer_id, None)


def _session_cookie_name():
    import partner_portal
    return partner_portal.SESSION_COOKIE


def _remove(client, camera_id):
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    response = client.delete(f"/api/customer/cameras/{camera_id}")
    client.cookies.clear()
    return response


def _camera_row(db_path, camera_id="cam-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone())


def test_removal_succeeds(client, db_path):
    _seed_tenant_with_camera(db_path)
    response = _remove(client, "cam-1")
    assert response.status_code == 200


def test_the_camera_row_itself_is_never_deleted(client, db_path):
    _seed_tenant_with_camera(db_path)
    _remove(client, "cam-1")
    camera = _camera_row(db_path)
    assert camera is not None
    assert camera["id"] == "cam-1"
    assert camera["name"] == "Front Door"  # untouched


def test_removal_clears_device_key_and_camera_number_frees_the_slot(client, db_path):
    _seed_tenant_with_camera(db_path)
    _remove(client, "cam-1")
    camera = _camera_row(db_path)
    assert camera["device_key"] is None
    assert camera["camera_number"] is None
    assert camera["status"] == "removed"


def test_removal_deletes_credentials_a_replacement_camera_gets_its_own(client, db_path):
    _seed_tenant_with_camera(db_path)
    _remove(client, "cam-1")
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id='cam-1'").fetchone()[0] == 0


def test_historical_recording_and_event_survive_removal_with_a_resolvable_camera(client, db_path):
    _seed_tenant_with_camera(db_path)
    _seed_history(db_path)
    _remove(client, "cam-1")
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        recording = conn.execute("SELECT * FROM recordings WHERE id='rec-1'").fetchone()
        event = conn.execute("SELECT * FROM detection_events WHERE id='evt-1'").fetchone()
        # Both historical rows survive, still pointing at a real,
        # resolvable camera row (not orphaned, not cascaded away).
        assert recording is not None and recording["camera_id"] == "cam-1"
        assert event is not None and event["camera_id"] == "cam-1"
        camera_for_history = conn.execute("SELECT name FROM cameras WHERE id=?", (recording["camera_id"],)).fetchone()
        assert camera_for_history["name"] == "Front Door"


def test_removed_camera_disappears_from_the_active_customer_camera_list(client, db_path):
    _seed_tenant_with_camera(db_path)
    _remove(client, "cam-1")
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    response = client.get("/api/customer/cameras")
    client.cookies.clear()
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["cameras"]]
    assert "cam-1" not in ids


def test_removal_frees_the_quota_slot_for_a_new_camera(client, db_path):
    from customer_entitlements import upsert_entitlement
    _seed_tenant_with_camera(db_path)
    with override_target(sqlite_path=str(db_path)):
        upsert_entitlement(customer_id="cust-1", product="camera_slots_rdm", camera_slot_quantity=1, status="active")
    # At quota (1/1) -- a second camera is rejected.
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    blocked = client.post("/api/customer/cameras/provision", json={"appliance_id": "appl-cust-1", "device_key": "dev-2", "name": "Back Door"})
    client.cookies.clear()
    assert blocked.status_code == 403

    _remove(client, "cam-1")

    # Freed -- the same request now succeeds.
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    permitted = client.post("/api/customer/cameras/provision", json={"appliance_id": "appl-cust-1", "device_key": "dev-2", "name": "Back Door"})
    client.cookies.clear()
    assert permitted.status_code == 200, permitted.text


def test_removing_someone_elses_camera_is_404(client, db_path):
    _seed_tenant_with_camera(db_path, customer_id="cust-1", camera_id="cam-1")
    _seed_tenant_with_camera(db_path, customer_id="cust-2", camera_id="cam-2", device_key="dev-2", camera_number=1, partner_id="partner-2")
    response = _remove(client, "cam-2")  # cust-1's session, cust-2's camera
    assert response.status_code == 404
    # cust-2's camera is completely untouched.
    camera = _camera_row(db_path, "cam-2")
    assert camera["device_key"] == "dev-2"
    assert camera["status"] == "configured"


def test_removing_an_unknown_camera_is_404(client, db_path):
    _seed_tenant_with_camera(db_path)
    response = _remove(client, "cam-does-not-exist")
    assert response.status_code == 404
