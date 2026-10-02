"""Local Event-mode recording, cloud-side half: tests for the new
cameras.local_recording_mode column (and its 4 configurable fields),
the POST /api/admin/cameras/{camera_id}/local-recording-mode admin
route that sets them, and GET /api/appliance/configuration exposing
them to the appliance -- the same shape as
test_camera_cloud_recording_mode.py, deliberately, since this is the
exact same no-hidden-default convention and sync path, just gating
local disk recording instead of cloud upload.

Imports appliance_cloud (which imports partner_db, triggering its
import-time schema init) -- redirects to a throwaway sqlite file via
override_target() before that import, matching this project's own
documented constraint.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_camera_local_recording_mode.db"):
    import appliance_cloud
    import partner_portal
    from partner_db import connection, password_hash


def _seed(db, appliance_id, cloud_id, credential, camera_id, camera_site_id="site-1"):
    now = "2026-09-20T00:00:00"
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
    return tmp_path / "test_local_recording_mode.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _camera_row(db_path, camera_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute(
                "SELECT local_recording_mode,local_recording_pre_roll_seconds,local_recording_post_roll_seconds,"
                "local_recording_merge_gap_seconds,local_recording_max_event_seconds FROM cameras WHERE id=?",
                (camera_id,),
            ).fetchone()
            return dict(row) if row else None


# --------------------------------------------------------------- migration


def test_migration_adds_local_recording_columns(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(cameras)").fetchall()}
    assert "local_recording_mode" in columns
    assert "local_recording_pre_roll_seconds" in columns
    assert "local_recording_post_roll_seconds" in columns
    assert "local_recording_merge_gap_seconds" in columns
    assert "local_recording_max_event_seconds" in columns


def test_new_camera_defaults_to_null_not_event(db_path):
    """No hidden default: a camera nobody has ever explicitly switched
    must read back as null, never as 'event' -- every existing/new
    camera keeps today's unconditional continuous-recording behavior
    until an administrator explicitly opts it into Event mode."""
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    assert _camera_row(db_path, "cam-1")["local_recording_mode"] is None


def test_admin_can_set_event_mode(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "event"},
    )

    assert response.status_code == 200
    assert response.json() == {"camera_id": "cam-1", "local_recording_mode": "event"}
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] == "event"


def test_admin_can_set_event_mode_with_configurable_fields(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={
            "local_recording_mode": "event",
            "pre_roll_seconds": 10,
            "post_roll_seconds": 15,
            "merge_gap_seconds": 20,
            "max_event_seconds": 600,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "camera_id": "cam-1", "local_recording_mode": "event",
        "pre_roll_seconds": 10, "post_roll_seconds": 15,
        "merge_gap_seconds": 20, "max_event_seconds": 600,
    }
    row = _camera_row(db_path, "cam-1")
    assert row["local_recording_pre_roll_seconds"] == 10
    assert row["local_recording_post_roll_seconds"] == 15
    assert row["local_recording_merge_gap_seconds"] == 20
    assert row["local_recording_max_event_seconds"] == 600


def test_omitted_configurable_fields_are_left_unchanged_not_cleared(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "event", "pre_roll_seconds": 10},
    )

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "event", "post_roll_seconds": 20},
    )

    assert response.status_code == 200
    row = _camera_row(db_path, "cam-1")
    assert row["local_recording_pre_roll_seconds"] == 10  # untouched by the second call
    assert row["local_recording_post_roll_seconds"] == 20


def test_admin_can_set_continuous_mode(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "continuous"},
    )

    assert response.status_code == 200
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] == "continuous"


def test_admin_can_clear_mode_back_to_null(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    client.post("/api/admin/cameras/cam-1/local-recording-mode", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"local_recording_mode": "event"})

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": None},
    )

    assert response.status_code == 200
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] is None


def test_invalid_mode_value_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "sometimes"},
    )

    assert response.status_code == 400
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] is None  # unchanged


@pytest.mark.parametrize("field,value", [
    ("pre_roll_seconds", 0),
    ("pre_roll_seconds", -5),
    ("pre_roll_seconds", 301),
    ("max_event_seconds", 3601),
    ("merge_gap_seconds", "ten"),
    ("post_roll_seconds", True),  # bool is an int subclass -- must still be rejected
])
def test_invalid_configurable_field_values_are_rejected(client, db_path, field, value):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "event", field: value},
    )

    assert response.status_code == 400
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] is None  # unchanged


def test_unauthenticated_request_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post("/api/admin/cameras/cam-1/local-recording-mode", json={"local_recording_mode": "event"})

    assert response.status_code in (401, 403)
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] is None


def test_non_administrator_role_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: partner_portal._token("someone@example.test", "customer_owner", None, "cust-1")},
        json={"local_recording_mode": "event"},
    )

    assert response.status_code == 403
    assert _camera_row(db_path, "cam-1")["local_recording_mode"] is None


def test_nonexistent_camera_is_404(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()

    response = client.post(
        "/api/admin/cameras/does-not-exist/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "event"},
    )

    assert response.status_code == 404


# ----------------------------------------------- GET /api/appliance/configuration exposure


def test_configuration_route_exposes_local_recording_fields(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    client.post(
        "/api/admin/cameras/cam-1/local-recording-mode",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"local_recording_mode": "event", "pre_roll_seconds": 10, "post_roll_seconds": 15},
    )

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    camera = response.json()["cameras"][0]
    assert camera["local_recording_mode"] == "event"
    assert camera["local_recording_pre_roll_seconds"] == 10
    assert camera["local_recording_post_roll_seconds"] == 15


def test_configuration_route_reports_null_when_unset(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    camera = response.json()["cameras"][0]
    assert camera["local_recording_mode"] is None
    assert camera["local_recording_pre_roll_seconds"] is None
