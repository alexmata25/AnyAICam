"""Regression coverage for the confirmed-live Ryzen defect (2026-09-20):
a camera synced onto an edge appliance purely through edge_camera_sync.
sync_provisioned_cameras() -- the real, only path a genuine appliance's
local cameras table is ever populated through -- had no corresponding
local customers/sites/appliances rows, because that function only ever
upserted the cameras table itself. The very next time main.py's
_catalog_local_recordings_for_camera() tried to INSERT a newly
discovered recording file into the local recordings table (which
carries its own customer_id/site_id/appliance_id FOREIGN KEYs), it
raised sqlite3.IntegrityError -- for EVERY camera on the appliance,
Event-mode and Continuous-mode alike, confirmed live by reproducing it
identically against both the Event-mode pilot camera and an untouched
Continuous-mode camera (Living Room).

Every existing edge_camera_sync test (test_edge_camera_sync.py) and
every existing recording-catalog test (test_recording_date_index.py)
independently passed throughout this gap, because neither ever combined
"a camera synced the real way" with "a recording cataloged the real
way" in the same test -- test_edge_camera_sync.py never calls the
catalog function, and test_recording_date_index.py always hand-seeds
full partners/customers/sites/appliances rows via _seed_base_tenant()
rather than going through sync_provisioned_cameras() at all. This file
closes that gap directly.

Root cause fix: appliance_cloud.py's appliance_configuration() now
returns the appliance's own real partner/customer/site/appliance rows
(the cloud's actual records, not fabricated placeholders) under a top-
level "identity" key. edge_camera_sync.py materializes them locally, in
FK dependency order, before upserting cameras -- so referential
integrity is genuinely satisfied rather than bypassed with `PRAGMA
foreign_keys=OFF` (still kept, but only as an explicit, documented
fallback for an old control-plane that hasn't shipped the identity
payload yet).
"""
import sqlite3
from datetime import datetime

import pytest

import edge_camera_sync
import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_edge_sync_recording_catalog_integration.db"


IDENTITY = {
    "appliance_id": "appl-ryzen", "cloud_id": "AIC-RYZEN0001",
    "credential": "ryzen-credential", "customer_id": "cust-real-1", "site_id": "site-real-1",
}

CLOUD_IDENTITY_PAYLOAD = {
    "partner": {"id": "partner-real-1", "name": "Real Partner Co", "approval_status": "approved"},
    "customer": {
        "id": "cust-real-1", "partner_id": "partner-real-1", "name": "Real Customer",
        "company": "Real Customer LLC", "email": "real-customer@example.test",
        "phone": None, "status": "active", "trial_status": None, "billing_status": None,
    },
    "site": {"id": "site-real-1", "customer_id": "cust-real-1", "name": "Primary site", "address": None, "site_type": "Customer site"},
    "appliance": {"id": "appl-ryzen", "customer_id": "cust-real-1", "site_id": "site-real-1", "cloud_id": "AIC-RYZEN0001", "partner_id": "partner-real-1"},
}

CAMERA_PAYLOAD = {
    "id": "cam-real-1", "name": "Bedroom", "camera_number": 4, "status": "configured",
    "device_key": "urn:uuid:bedroom", "onvif_endpoint": "rtsp://192.168.0.145:554/ch1",
    "resolution": "2mp", "recording_mode": "motion", "people_counting_enabled": 0,
    "local_recording_mode": "event",
}


def _write_recording_file(camera_folder, camera_number, dt, size_bytes=1024, age_seconds=200):
    camera_folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = camera_folder / name
    path.write_bytes(b"x" * size_bytes)
    import os
    import time as _time
    old_time = _time.time() - age_seconds
    os.utime(path, (old_time, old_time))
    return path


def _sync(monkeypatch, *, with_identity: bool):
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: IDENTITY)
    payload = {"cameras": [CAMERA_PAYLOAD], "configuration_version": "x"}
    if with_identity:
        payload["identity"] = CLOUD_IDENTITY_PAYLOAD
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: payload)
    return edge_camera_sync.sync_provisioned_cameras()


def test_camera_synced_the_real_way_then_cataloging_a_new_recording_no_longer_500s(db_path, tmp_path, monkeypatch):
    """The actual bug, reproduced end to end: sync a camera exactly the
    way a real appliance does (sync_provisioned_cameras() alone, no
    hand-seeded tenant rows), then discover a real recording file for
    it -- must not raise."""
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()

    with override_target(sqlite_path=str(db_path)):
        result = _sync(monkeypatch, with_identity=True)
        assert result["status"] == "ok"

        camera_folder = tmp_path / "recordings" / "camera4"
        _write_recording_file(camera_folder, 4, datetime(2026, 9, 20, 23, 8, 50))

        added = main._catalog_local_recordings_for_camera("cam-real-1")  # must not raise
        assert added == 1

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rec = conn.execute("SELECT customer_id, site_id, appliance_id, camera_id FROM recordings WHERE camera_id='cam-real-1'").fetchone()
        assert dict(rec) == {"customer_id": "cust-real-1", "site_id": "site-real-1", "appliance_id": "appl-ryzen", "camera_id": "cam-real-1"}


def test_identity_payload_materializes_local_parent_rows_matching_the_camera_fks(db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        _sync(monkeypatch, with_identity=True)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        partner = dict(conn.execute("SELECT id, name, approval_status FROM partners WHERE id='partner-real-1'").fetchone())
        customer = dict(conn.execute("SELECT id, partner_id, name, email FROM customers WHERE id='cust-real-1'").fetchone())
        site = dict(conn.execute("SELECT id, customer_id, name FROM sites WHERE id='site-real-1'").fetchone())
        appliance = dict(conn.execute("SELECT id, customer_id, site_id, cloud_id FROM appliances WHERE id='appl-ryzen'").fetchone())

    assert partner == {"id": "partner-real-1", "name": "Real Partner Co", "approval_status": "approved"}
    assert customer == {"id": "cust-real-1", "partner_id": "partner-real-1", "name": "Real Customer", "email": "real-customer@example.test"}
    assert site == {"id": "site-real-1", "customer_id": "cust-real-1", "name": "Primary site"}
    assert appliance == {"id": "appl-ryzen", "customer_id": "cust-real-1", "site_id": "site-real-1", "cloud_id": "AIC-RYZEN0001"}


def test_resync_updates_parent_rows_in_place_never_duplicates(db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        _sync(monkeypatch, with_identity=True)
        _sync(monkeypatch, with_identity=True)

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM partners").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM appliances").fetchone()[0] == 1


def test_an_old_control_plane_without_the_identity_payload_still_syncs_cameras(db_path, monkeypatch):
    """Transitional-compatibility guard: an appliance polling a control
    plane that hasn't shipped the identity payload yet must not have its
    camera sync break -- the pre-existing PRAGMA foreign_keys=OFF
    fallback still applies in that one case."""
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        result = _sync(monkeypatch, with_identity=False)
        assert result["status"] == "ok"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        camera = dict(conn.execute("SELECT id, customer_id, site_id, appliance_id FROM cameras WHERE id='cam-real-1'").fetchone())
    assert camera == {"id": "cam-real-1", "customer_id": "cust-real-1", "site_id": "site-real-1", "appliance_id": "appl-ryzen"}
