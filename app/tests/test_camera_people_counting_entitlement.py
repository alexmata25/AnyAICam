"""People Counting milestone, cloud-side entitlement half: tests for the
new cameras.people_counting_enabled column, the
POST /api/admin/cameras/{camera_id}/people-counting admin route that
sets it, and GET /api/appliance/configuration exposing it to the
appliance. This is the first per-camera analytics-entitlement flag in
the system -- see people_counting.py and its own test suite in the
edge lineage for the appliance-side tracking/counting logic this field
gates, and the accompanying report for how other named analytics
(LPR, PPE, etc.) can adopt this same column-per-feature pattern later.

Mirrors test_camera_cloud_recording_mode.py's own established pattern
exactly (same seed helper shape, same admin-session-minting helper,
same appliance-auth-header helper, same override_target()-before-import
constraint), since this is the same kind of camera-level entitlement
column following the same no-hidden-default convention.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_camera_people_counting_entitlement.db"):
    import appliance_cloud
    import partner_portal
    from partner_db import connection, password_hash


def _seed(db, appliance_id, cloud_id, credential, camera_id, camera_site_id="site-1"):
    now = "2026-08-24T00:00:00"
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
    return tmp_path / "test_people_counting_entitlement.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _camera_entitlement(db_path, camera_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT people_counting_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
            return row["people_counting_enabled"] if row else None


# --------------------------------------------------------------- migration


def test_migration_adds_people_counting_enabled_column(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(cameras)").fetchall()}
    assert "people_counting_enabled" in columns


@pytest.fixture()
def seeded_db_path(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    return db_path


def test_new_camera_defaults_to_not_entitled(client, seeded_db_path):
    """No hidden default: a camera nobody has ever explicitly entitled
    must read back as falsy (None/0), never as enabled -- People
    Counting must never silently turn on for a camera just because it
    exists."""
    entitlement = _camera_entitlement(seeded_db_path, "cam-1")
    assert not entitlement  # None or 0 -- both falsy, both mean "not entitled"


# --------------------------------------------------------- admin route: setting entitlement


def test_admin_can_enable_people_counting(client, seeded_db_path):
    response = client.post(
        "/api/admin/cameras/cam-1/people-counting",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"people_counting_enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {"camera_id": "cam-1", "people_counting_enabled": True}
    assert _camera_entitlement(seeded_db_path, "cam-1") == 1


def test_admin_can_disable_people_counting_after_enabling(client, seeded_db_path):
    client.post("/api/admin/cameras/cam-1/people-counting", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"people_counting_enabled": True})

    response = client.post(
        "/api/admin/cameras/cam-1/people-counting",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"people_counting_enabled": False},
    )

    assert response.status_code == 200
    assert response.json() == {"camera_id": "cam-1", "people_counting_enabled": False}
    assert _camera_entitlement(seeded_db_path, "cam-1") == 0


def test_missing_field_in_payload_is_treated_as_false_not_an_error(client, seeded_db_path):
    """Matches the established cloud_recording_mode pattern's own
    style of treating an absent/falsy payload value as the safe,
    disabled state rather than raising -- there's no ambiguous "third
    state" for a plain boolean entitlement the way there is for the
    three-valued cloud_recording_mode."""
    response = client.post(
        "/api/admin/cameras/cam-1/people-counting",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={},
    )

    assert response.status_code == 200
    assert response.json()["people_counting_enabled"] is False
    assert _camera_entitlement(seeded_db_path, "cam-1") == 0


def test_unauthenticated_request_is_rejected(client, seeded_db_path):
    response = client.post("/api/admin/cameras/cam-1/people-counting", json={"people_counting_enabled": True})

    assert response.status_code in (401, 403)
    assert not _camera_entitlement(seeded_db_path, "cam-1")


def test_non_administrator_role_is_rejected(client, seeded_db_path):
    response = client.post(
        "/api/admin/cameras/cam-1/people-counting",
        cookies={partner_portal.SESSION_COOKIE: partner_portal._token("someone@example.test", "customer_owner", None, "cust-1")},
        json={"people_counting_enabled": True},
    )

    assert response.status_code == 403
    assert not _camera_entitlement(seeded_db_path, "cam-1")


def test_nonexistent_camera_is_404(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()

    response = client.post(
        "/api/admin/cameras/does-not-exist/people-counting",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        json={"people_counting_enabled": True},
    )

    assert response.status_code == 404


# ----------------------------------------------- GET /api/appliance/configuration exposure


def test_configuration_route_exposes_people_counting_enabled_true(client, seeded_db_path):
    client.post("/api/admin/cameras/cam-1/people-counting", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"people_counting_enabled": True})

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    cameras = response.json()["cameras"]
    assert len(cameras) == 1
    assert cameras[0]["people_counting_enabled"] == 1


def test_configuration_route_reports_falsy_when_unset(client, seeded_db_path):
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    assert not response.json()["cameras"][0]["people_counting_enabled"]


def test_configuration_route_carries_both_cloud_recording_mode_and_people_counting_independently(client, seeded_db_path):
    """Proves the two per-camera flags are genuinely independent fields
    -- setting one never touches the other."""
    client.post("/api/admin/cameras/cam-1/cloud-recording-mode", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"cloud_recording_mode": "motion"})
    client.post("/api/admin/cameras/cam-1/people-counting", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, json={"people_counting_enabled": True})

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    camera = response.json()["cameras"][0]
    assert camera["recording_mode"] == "motion"
    assert camera["people_counting_enabled"] == 1
