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


def _grant_legacy_plan(db, customer_id="cust-1", recording_mode="continuous"):
    db.execute(
        "INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"plan-{customer_id}", customer_id, "2mp", recording_mode, 30, 2, 50.0, 25.0, "active", "2026-08-21T00:00:00"))


def _grant_v2_plan(db, customer_id="cust-1", plan_key="hybrid", status="active"):
    cloud = 1 if plan_key == "hybrid" else 0
    db.execute(
        "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
        "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,cloud_event_storage,"
        "cloud_event_retention_days,created_at,updated_at) VALUES(?,?,?,2,'cus_t','sub_t','si_t','price_t',?,'active',7,?,?,"
        "'2026-10-10','2026-10-10')",
        (f"v2-{customer_id}", customer_id, plan_key, status, cloud, 14 if cloud else None))


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


def test_configuration_route_reports_null_when_unset_without_a_billing_v2_plan(client, db_path):
    """Customers outside per-camera billing keep the stored value unchanged:
    an unset camera stays null (older edges read it as continuous upload)."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    assert response.json()["cameras"][0]["recording_mode"] is None


@pytest.mark.parametrize("legacy_mode", ["continuous", "motion"])
def test_a_legacy_plan_customer_keeps_the_stored_unset_mode(client, db_path, legacy_mode):
    """Codex review (d9f1f11): a legacy continuous customer's unset camera
    must not be switched to 'disabled' by the cloud."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _grant_legacy_plan(db, recording_mode=legacy_mode)

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.json()["cameras"][0]["recording_mode"] is None


@pytest.mark.parametrize("plan_key,expected", [("hybrid", "motion"), ("ai_local", "disabled"), ("basic_local", "disabled")])
def test_configuration_route_derives_an_unset_mode_from_the_paid_plan(client, db_path, plan_key, expected):
    """A new paying customer's camera works without an administrator: the
    Samsung Mobile Demo (Hybrid, NULL mode) had every event refused."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _grant_v2_plan(db, plan_key=plan_key)

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    assert response.json()["cameras"][0]["recording_mode"] == expected


@pytest.mark.parametrize("explicit", ["disabled", "continuous", "motion"])
def test_an_explicit_mode_is_kept_on_a_hybrid_plan(client, db_path, explicit):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _grant_v2_plan(db, plan_key="hybrid")
            db.execute("UPDATE cameras SET cloud_recording_mode=? WHERE id='cam-1'", (explicit,))

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.json()["cameras"][0]["recording_mode"] == explicit


@pytest.mark.parametrize("plan_key,status", [("ai_local", "active"), ("basic_local", "active"), ("hybrid", "suspended")])
def test_an_explicit_motion_camera_without_cloud_event_protection_is_disabled(client, db_path, plan_key, status):
    """Codex review (d9f1f11): an explicit 'motion' must not make a Local (or
    suspended Hybrid) camera upload bytes the catalog then refuses."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _grant_v2_plan(db, plan_key=plan_key, status=status)
            db.execute("UPDATE cameras SET cloud_recording_mode='motion' WHERE id='cam-1'")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.json()["cameras"][0]["recording_mode"] == "disabled"


def test_a_suspended_hybrid_plan_does_not_enable_cloud_event_media(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _grant_v2_plan(db, plan_key="hybrid", status="suspended")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.json()["cameras"][0]["recording_mode"] == "disabled"
