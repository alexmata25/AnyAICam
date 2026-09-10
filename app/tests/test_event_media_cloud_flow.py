"""Local-only contract tests for motion-event cloud media.

These tests use temporary files, SQLite, and a mocked S3/control plane.  They
never contact an appliance, AWS, or another environment.
"""

import secrets
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_event_media_cloud_flow_import.db"):
    import appliance_cloud
    import event_media_uploader
    from partner_db import connection, initialize_database, password_hash


def _headers(appliance_id="appl-1", credential="credential"):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed(db):
    now = "2026-08-21T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", "customer@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-1", "cust-1", "site-1", "AIC-TEST", now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", "appl-1", password_hash("credential"), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", ("cam-1", "cust-1", "site-1", "appl-1", "Driveway", now))


@pytest.fixture()
def client(tmp_path):
    database = tmp_path / "event-media.db"
    with override_target(sqlite_path=str(database)):
        initialize_database()
        with connection() as db:
            _seed(db)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *args, **kwargs: "")
        with TestClient(app) as test_client:
            yield test_client, database


def _event_payload():
    return {
        "local_event_id": "evt-1",
        "event_type": "motion",
        "confidence": 0.9,
        "object_count": 1,
        "detections": [],
        "event_timestamp": "2026-08-21T00:00:03",
    }


def _media_payload(**overrides):
    value = {
        "s3_key": "recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/events/motion_evt-1.mp4",
        "thumbnail_s3_key": "recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/events/motion_evt-1.jpg",
        # Deliberately crosses midnight relative to the event timestamp:
        # key authorization must use the stored event, not this value.
        "started_at": "2026-08-20T23:59:58",
        "ended_at": "2026-08-21T00:00:10",
        "duration_seconds": 12.0,
        "size_bytes": 42,
    }
    value.update(overrides)
    return value


def test_media_registration_is_tenant_scoped_idempotent_and_uses_event_date(client, monkeypatch):
    test_client, database = client
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    assert test_client.post("/api/appliance/analytics/cam-1/events", headers=_headers(), json=_event_payload()).status_code == 200

    first = test_client.post("/api/appliance/analytics/cam-1/events/evt-1/media", headers=_headers(), json=_media_payload())
    second = test_client.post("/api/appliance/analytics/cam-1/events/evt-1/media", headers=_headers(), json=_media_payload())

    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    with override_target(sqlite_path=str(database)):
        with connection() as db:
            rows = db.execute("SELECT customer_id,camera_id,s3_key,thumbnail_s3_key FROM detection_event_media").fetchall()
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "customer_id": "cust-1", "camera_id": "cam-1",
        "s3_key": _media_payload()["s3_key"], "thumbnail_s3_key": _media_payload()["thumbnail_s3_key"],
    }


def test_media_registration_rejects_an_unrelated_key_inside_the_camera_prefix(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    assert test_client.post("/api/appliance/analytics/cam-1/events", headers=_headers(), json=_event_payload()).status_code == 200

    response = test_client.post(
        "/api/appliance/analytics/cam-1/events/evt-1/media",
        headers=_headers(),
        json=_media_payload(s3_key="recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/archive/unrelated.mp4"),
    )
    assert response.status_code == 403


def test_local_path_rejects_traversal_before_any_upload(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    recordings = app_root / "recordings"
    recordings.mkdir(parents=True)
    outside = app_root / "private.mp4"
    outside.write_bytes(b"not event media")
    monkeypatch.setattr(event_media_uploader, "APP_ROOT", app_root)
    monkeypatch.setattr(event_media_uploader, "RECORDINGS_ROOT", recordings)

    assert event_media_uploader._local_path_from_recording_url("/recordings/../private.mp4") is None


def test_non_motion_camera_short_circuits_before_credentials_or_s3(tmp_path, monkeypatch):
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", True)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")
    monkeypatch.setattr(event_media_uploader, "_local_path_from_recording_url", lambda _: clip)
    import recording_uploader
    monkeypatch.setattr(recording_uploader, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda _: {"camera_id": "cam-1", "cloud_recording_mode": "continuous"})
    monkeypatch.setattr(recording_uploader, "_ensure_session", lambda *_: pytest.fail("credentials must not be requested"))

    assert event_media_uploader.upload_motion_event_media(
        event_id="evt-1", camera_number=1,
        event_start=datetime(2026, 8, 21, 12), event_end=datetime(2026, 8, 21, 12, 0, 5),
        clip_url="/recordings/clip.mp4", thumbnail_url=None,
    ) is False
