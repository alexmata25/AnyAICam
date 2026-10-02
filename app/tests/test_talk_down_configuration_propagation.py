"""Talkdown discovery result propagation, cloud round trip
(2026-09-16): closes the gap where POST /api/appliance/cameras already
persisted a discovery probe's talk_down_supported/talk_down_metadata
into the cloud cameras table (see talk_down_discovery.py's
_report_capability()), but GET /api/appliance/configuration never read
those two columns back out -- so a real, successful discovery cycle
could never reach the edge appliance's own local database, and
talk_sessions.py's tri-state talk_down_supported gate (the thing that
actually authorizes/denies a customer's Talk session) stayed NULL
forever regardless of how many discovery cycles ran. Same class of gap
as the earlier people_counting_enabled miss this file's sibling,
test_camera_people_counting_entitlement.py, already documents and
fixed.

Part 1 (cloud side): GET /api/appliance/configuration now exposes a
talk_down: {supported, metadata} object (or None when never probed).
Part 2 (edge side): edge_camera_sync.sync_provisioned_cameras() now
writes talk_down_supported/talk_down_metadata/talk_down_verified_at
into the local cameras table from that object, and -- since a sync
response omitting the key must mean "the cloud had nothing new to say
this cycle," never "clear the last known result" -- preserves an
existing local value rather than wiping it back to NULL.
"""

import json
import secrets
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_talk_down_configuration_propagation.db"):
    import appliance_cloud
    import partner_portal
    from partner_db import connection, password_hash


def _seed(db, appliance_id, cloud_id, credential, camera_id, camera_site_id="site-1"):
    now = "2026-09-16T00:00:00"
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


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_talk_down_configuration_propagation.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture()
def seeded_db_path(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
    return db_path


# --------------------------------------------------- cloud side: GET /api/appliance/configuration


def test_configuration_route_reports_talk_down_none_when_never_probed(client, seeded_db_path):
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    assert response.json()["cameras"][0]["talk_down"] is None


def test_configuration_route_exposes_a_supported_result(client, seeded_db_path):
    report = client.post(
        "/api/appliance/cameras",
        headers=_appliance_auth_headers("appl-1", "test-credential"),
        json={"cameras": [{"id": "cam-1", "talk_down": {"supported": True, "metadata": {"audio_output_token": "AudioOutputToken"}}}]},
    )
    assert report.status_code == 200
    assert report.json()["status"] == "accepted"

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    talk_down = response.json()["cameras"][0]["talk_down"]
    assert talk_down == {"supported": True, "metadata": {"audio_output_token": "AudioOutputToken"}}


def test_configuration_route_exposes_an_unsupported_result(client, seeded_db_path):
    client.post("/api/appliance/cameras", headers=_appliance_auth_headers("appl-1", "test-credential"), json={"cameras": [{"id": "cam-1", "talk_down": {"supported": False}}]})

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    talk_down = response.json()["cameras"][0]["talk_down"]
    assert talk_down == {"supported": False, "metadata": None}


def test_talk_down_and_cloud_recording_mode_are_independent(client, seeded_db_path):
    """Proves the propagation fix reads a genuinely separate pair of
    columns, not accidentally aliasing/overwriting cloud_recording_mode
    (a real risk given both are per-camera "capability/entitlement"
    concepts read off the same SELECT)."""
    with override_target(sqlite_path=str(seeded_db_path)):
        with connection() as db:
            db.execute("UPDATE cameras SET cloud_recording_mode='motion' WHERE id='cam-1'")
    client.post("/api/appliance/cameras", headers=_appliance_auth_headers("appl-1", "test-credential"), json={"cameras": [{"id": "cam-1", "talk_down": {"supported": True}}]})

    camera = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential")).json()["cameras"][0]
    assert camera["recording_mode"] == "motion"
    assert camera["talk_down"] == {"supported": True, "metadata": None}


# ------------------------------------------------------------------------- edge side: sync write


import edge_camera_sync  # noqa: E402  (imported after override_target-guarded appliance_cloud import above, matching this suite's established ordering)


IDENTITY = {
    "appliance_id": "appl-ryzen", "cloud_id": "AIC-RYZEN0001",
    "credential": "ryzen-credential", "customer_id": "cust-1", "site_id": "site-1",
}


@pytest.fixture()
def edge_db_path(tmp_path):
    return tmp_path / "test_talk_down_edge_sync.db"


@pytest.fixture()
def _edge_seeded_db(edge_db_path, monkeypatch):
    with override_target(sqlite_path=str(edge_db_path)):
        from partner_db import initialize_database
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    with override_target(sqlite_path=str(edge_db_path)):
        yield


def _mock_identity(monkeypatch, identity=IDENTITY):
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: identity)


def _mock_cloud_config(monkeypatch, cameras):
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: {"cameras": cameras, "configuration_version": "x"})


def _local_camera(edge_db_path, camera_id="cam-1"):
    with override_target(sqlite_path=str(edge_db_path)):
        con = sqlite3.connect(edge_db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        return dict(row) if row else None


_CAM_BASE = {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured"}


def test_sync_writes_a_supported_result_locally(edge_db_path, monkeypatch, _edge_seeded_db):
    _mock_identity(monkeypatch)
    cam = {**_CAM_BASE, "talk_down": {"supported": True, "metadata": {"audio_output_token": "AudioOutputToken"}}}
    _mock_cloud_config(monkeypatch, [cam])
    edge_camera_sync.sync_provisioned_cameras()
    local = _local_camera(edge_db_path)
    assert local["talk_down_supported"] == 1
    assert json.loads(local["talk_down_metadata"]) == {"audio_output_token": "AudioOutputToken"}
    assert local["talk_down_verified_at"] is not None


def test_sync_writes_an_unsupported_result_locally(edge_db_path, monkeypatch, _edge_seeded_db):
    _mock_identity(monkeypatch)
    cam = {**_CAM_BASE, "talk_down": {"supported": False, "metadata": None}}
    _mock_cloud_config(monkeypatch, [cam])
    edge_camera_sync.sync_provisioned_cameras()
    local = _local_camera(edge_db_path)
    assert local["talk_down_supported"] == 0


def test_sync_leaves_talk_down_null_when_never_probed(edge_db_path, monkeypatch, _edge_seeded_db):
    _mock_identity(monkeypatch)
    cam = {**_CAM_BASE, "talk_down": None}
    _mock_cloud_config(monkeypatch, [cam])
    edge_camera_sync.sync_provisioned_cameras()
    local = _local_camera(edge_db_path)
    assert local["talk_down_supported"] is None


def test_a_later_sync_with_no_talk_down_key_preserves_the_known_result(edge_db_path, monkeypatch, _edge_seeded_db):
    """A sync cycle that runs between discovery probes (or against an
    older cloud response shape) must never wipe out an already-known,
    real result back to NULL -- that would make talk_sessions.py's
    tri-state gate forget a camera it already confirmed supports
    Talkdown, re-blocking a customer who was correctly authorized a
    moment ago."""
    _mock_identity(monkeypatch)
    supported_cam = {**_CAM_BASE, "talk_down": {"supported": True, "metadata": {"x": "y"}}}
    _mock_cloud_config(monkeypatch, [supported_cam])
    edge_camera_sync.sync_provisioned_cameras()
    assert _local_camera(edge_db_path)["talk_down_supported"] == 1

    unchanged_cam = {**_CAM_BASE, "talk_down": None}
    _mock_cloud_config(monkeypatch, [unchanged_cam])
    edge_camera_sync.sync_provisioned_cameras()
    local = _local_camera(edge_db_path)
    assert local["talk_down_supported"] == 1
    assert json.loads(local["talk_down_metadata"]) == {"x": "y"}
