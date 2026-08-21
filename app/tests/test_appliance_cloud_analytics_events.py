"""POST /api/appliance/analytics/{camera_id}/events: focused tests for
the analytics-event sync milestone's cloud-side route and the
detection_events migration. Proves the two guardrails explicitly
required for this milestone: tenant/camera identifiers are resolved
entirely server-side (never trusted from the payload, even when the
payload tries to inject different ones), and forbidden local-only
fields (thumbnail, linked_recording) are never stored, by construction
-- the route never reads those keys at all.

Imports appliance_cloud (which imports partner_db, triggering its
import-time schema init) -- redirects to a throwaway sqlite file via
override_target() before that import, matching this project's own
documented constraint and the established pattern from
test_appliance_cloud_recording_status.py.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_cloud_analytics_events.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed(db, appliance_id, cloud_id, credential, camera_id, camera_site_id="site-1", other_camera_id=None):
    now = "2026-08-21T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Test Customer", "test@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (camera_site_id, "cust-1", "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, "cust-1", camera_site_id, cloud_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", appliance_id, password_hash(credential), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (camera_id, "cust-1", camera_site_id, appliance_id, "Camera 1", now))
    if other_camera_id:
        # A second appliance owning a different camera, to prove wrong-camera rejection.
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-2", "cust-1", camera_site_id, "AIC-OTHER0001", now))
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (other_camera_id, "cust-1", camera_site_id, "appl-2", "Camera Other", now))


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _valid_payload(**overrides):
    payload = {
        "local_event_id": "local-evt-abc123",
        "event_type": "person",
        "confidence": 0.87,
        "object_count": 2,
        "detections": [{"class_name": "person", "confidence": 0.87, "x": 10, "y": 20, "width": 30, "height": 40}],
        "event_timestamp": "2026-08-21T18:00:00",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_analytics_events.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _row_for(db_path, camera_id, local_event_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute(
                "SELECT * FROM detection_events WHERE camera_id=? AND local_event_id=?",
                (camera_id, local_event_id),
            ).fetchone()


def _count(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT COUNT(*) AS c FROM detection_events").fetchone()["c"]


# --------------------------------------------------------------- migration


def test_migration_creates_detection_events_table_with_expected_columns(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(detection_events)").fetchall()}
    expected = {
        "id", "customer_id", "site_id", "appliance_id", "camera_id",
        "local_event_id", "event_type", "confidence", "object_count",
        "detections_json", "event_timestamp", "created_at",
    }
    assert expected.issubset(columns)


# ---------------------------------------------------------------------- happy path


def test_authenticated_valid_event_is_accepted_and_stored(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    row = _row_for(db_path, "cam-1", "local-evt-abc123")
    assert row is not None
    assert row["event_type"] == "person"
    assert row["object_count"] == 2


# ------------------------------------------------------- guardrail: server-side resolution


def test_tenant_and_site_are_resolved_server_side_never_from_payload(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1", camera_site_id="real-site-1")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(
            customer_id="attacker-customer",
            site_id="attacker-site",
            appliance_id="attacker-appliance",
            camera_id="attacker-camera",
        ),
    )

    assert response.status_code == 200
    row = _row_for(db_path, "cam-1", "local-evt-abc123")
    assert row["customer_id"] == "cust-1"          # from the authorized camera, not the payload
    assert row["site_id"] == "real-site-1"          # camera's own authoritative site_id, not the payload
    assert row["appliance_id"] == "appl-1"          # from authenticate_appliance(), not the payload
    assert row["camera_id"] == "cam-1"              # from the URL path + authorization, not the payload


def test_wrong_camera_is_rejected(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1", other_camera_id="cam-not-mine")

    response = client.post(
        "/api/appliance/analytics/cam-not-mine/events",  # owned by appl-2, not appl-1
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(),
    )

    assert response.status_code == 403
    assert _count(db_path) == 0


def test_unauthenticated_request_is_rejected(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post("/api/appliance/analytics/cam-1/events", json=_valid_payload())

    assert response.status_code == 401
    assert _count(db_path) == 0


# ----------------------------------------------------------------- idempotency


def test_duplicate_local_event_id_is_idempotent_not_a_second_row(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    first = client.post("/api/appliance/analytics/cam-1/events", headers=_auth_headers("appl-1", "test-credential"), json=_valid_payload())
    second = client.post("/api/appliance/analytics/cam-1/events", headers=_auth_headers("appl-1", "test-credential"), json=_valid_payload())

    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert second.json()["event_id"] == first.json()["event_id"]
    assert _count(db_path) == 1


# --------------------------------------------------------------- disabled flag


def test_disabled_flag_rejects_and_stores_nothing(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", False)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(),
    )

    assert response.status_code == 404
    assert _count(db_path) == 0


# ---------------------------------------------------- guardrail: forbidden fields


def test_forbidden_local_fields_are_never_stored(client, db_path, monkeypatch):
    """thumbnail and linked_recording are local-filesystem concepts that
    must never leave the appliance -- the route never reads these keys
    at all, so even when present in the payload they can't appear
    anywhere in the stored row."""
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(
            thumbnail="/recordings/media/ai/2026-08-21/camera1_18-00-00_abc123.jpg",
            linked_recording="/app/recordings/camera1/camera1_2026-08-21_17-55-00.mkv",
            mock=False,
        ),
    )

    assert response.status_code == 200
    row = _row_for(db_path, "cam-1", "local-evt-abc123")
    row_values_joined = " ".join(str(v) for v in dict(row).values())
    assert "thumbnail" not in dict(row)
    assert "linked_recording" not in dict(row)
    assert "/recordings/media/ai" not in row_values_joined
    assert ".mkv" not in row_values_joined


# --------------------------------------------------------------- validation


def test_missing_required_field_is_rejected(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")

    payload = _valid_payload()
    del payload["event_type"]

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=payload,
    )

    assert response.status_code == 400
    assert _count(db_path) == 0
