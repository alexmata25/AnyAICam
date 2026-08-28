"""Motion-gated cloud upload milestone, cloud-side half: tests for the
new cameras.cloud_recording_mode column, the
POST /api/admin/cameras/{camera_id}/cloud-recording-mode admin route
that sets it, and GET /api/appliance/configuration exposing it to the
appliance. This is the plan-mode signal recording_uploader.py (edge
side) reads to decide whether to motion-gate a camera's cloud uploads
-- see test_recording_uploader_motion_gate.py in the edge lineage for
the appliance-side behavior this field drives.

Imports appliance_cloud (which imports partner_db, triggering its
import-time schema init) -- redirects to a throwaway sqlite file via
override_target() before that import, matching this project's own
documented constraint and test_appliance_cloud_analytics_events.py's
own established pattern.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_camera_cloud_recording_mode.db"):
    import appliance_cloud
    import partner_portal
    from partner_db import connection, password_hash


def _seed(db, appliance_id, cloud_id, credential, camera_id, camera_site_id="site-1"):
    now = "2026-08-21T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Test Customer", "test@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (camera_site_id, "cust-1", "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, "cust-1", camera_site_id, cloud_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", appliance_id, password_hash(credential), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) VALUES(?,?,?,?,?,?,?)", (camera_id, "cust-1", camera_site_id, appliance_id, "Camera 1", 1, now))


def _appliance_auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _admin_cookie() -> str:
    return partner_portal._token("admin@example.test", "administrator")


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_recording_mode.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _camera_mode(db_path, camera_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT cloud_recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
            return row["cloud_recording_mode"] if row else None


# --------------------------------------------------------------- migration


def test_migration_adds_cloud_recording_mode_column(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(cameras)").fetchall()}
    assert "cloud_recording_mode" in columns


def test_new_camera_defaults_to_null_not_motion(client, db_path):
    """No hidden default: a camera nobody has ever explicitly set must
    read back as null, never as 'motion' or 'continuous'."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    assert _camera_mode(db_path, "cam-1") is None


# --------------------------------------------------------- admin route: setting the mode


def test_admin_can_set_motion_mode(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/cloud-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"cloud_recording_mode": "motion"},
    )

    assert response.status_code == 200
    assert response.json() == {"camera_id": "cam-1", "cloud_recording_mode": "motion"}
    assert _camera_mode(db_path, "cam-1") == "motion"


def test_admin_can_set_continuous_mode(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/cloud-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"cloud_recording_mode": "continuous"},
    )

    assert response.status_code == 200
    assert _camera_mode(db_path, "cam-1") == "continuous"


def test_admin_can_clear_mode_back_to_null(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    client.post("/api/admin/cameras/cam-1/cloud-recording-mode", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"cloud_recording_mode": "motion"})

    response = client.post(
        "/api/admin/cameras/cam-1/cloud-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"cloud_recording_mode": None},
    )

    assert response.status_code == 200
    assert _camera_mode(db_path, "cam-1") is None


def test_invalid_mode_value_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/cloud-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"cloud_recording_mode": "sometimes"},
    )

    assert response.status_code == 400
    assert _camera_mode(db_path, "cam-1") is None  # unchanged


def test_unauthenticated_request_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post("/api/admin/cameras/cam-1/cloud-recording-mode", json={"cloud_recording_mode": "motion"})

    assert response.status_code in (401, 403)
    assert _camera_mode(db_path, "cam-1") is None


def test_non_administrator_role_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/cloud-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: partner_portal._token("someone@example.test", "customer_owner", None, "cust-1")},
        json={"cloud_recording_mode": "motion"},
    )

    assert response.status_code == 403
    assert _camera_mode(db_path, "cam-1") is None


def test_nonexistent_camera_is_404(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()

    response = client.post(
        "/api/admin/cameras/does-not-exist/cloud-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"cloud_recording_mode": "motion"},
    )

    assert response.status_code == 404


# ----------------------------------------------- GET /api/appliance/configuration exposure


def test_configuration_route_exposes_recording_mode_field(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    client.post("/api/admin/cameras/cam-1/cloud-recording-mode", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"cloud_recording_mode": "motion"})

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    cameras = response.json()["cameras"]
    assert len(cameras) == 1
    assert cameras[0]["recording_mode"] == "motion"


def test_configuration_route_reports_null_when_unset(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    assert response.json()["cameras"][0]["recording_mode"] is None
