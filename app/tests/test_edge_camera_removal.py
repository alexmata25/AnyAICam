"""Camera removal on the appliance (2026-10-08). The customer removes a camera
in the Cameras tab (DELETE /api/customer/cameras/{id}); the next
configuration sync sends it with status "removed" and no device_key or
camera_number. The appliance then
  * stops running it (no camera_number),
  * deletes its stored camera credentials, with SQLite secure_delete, and any
    credential still pending for its former device,
  * keeps the camera row, recordings, events and clips,
and does nothing more on later syncs. Harness: test_edge_camera_sync.py."""
import sqlite3

import edge_camera_sync
from test_edge_camera_sync import (  # noqa: F401  (pytest fixtures)
    IDENTITY, _local_cameras, _local_credentials, _mock_cloud_config, _mock_identity, _pending, _seed_pending_credential,
    _seeded_db, db_path,
)

ACTIVE = {"id": "cam-1", "name": "Front door", "camera_number": 1, "status": "configured",
          "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
          "resolution": "2mp", "recording_mode": "motion", "people_counting_enabled": 0}
REMOVED = {"id": "cam-1", "name": "Front door", "camera_number": None, "status": "removed",
           "device_key": None, "onvif_endpoint": None, "resolution": "2mp", "recording_mode": "motion",
           "people_counting_enabled": 0}
OTHER = {**ACTIVE, "id": "cam-2", "name": "Garage", "camera_number": 2, "device_key": "urn:uuid:bbbb"}


def _provisioned(db_path, monkeypatch, cameras=(ACTIVE,)):
    _mock_identity(monkeypatch)
    for camera in cameras:
        _seed_pending_credential(db_path, camera["device_key"])
    _mock_cloud_config(monkeypatch, list(cameras))
    edge_camera_sync.sync_provisioned_cameras()
    assert set(_local_credentials(db_path)) == {c["id"] for c in cameras}


def _seed_event(db_path, camera_id):
    # A plain connection (foreign keys off), like the sync's own partial-data
    # path: the event only needs to exist next to the camera row.
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,"
                "event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("evt-1", IDENTITY["customer_id"], IDENTITY["site_id"], IDENTITY["appliance_id"], camera_id, "local-1",
                 "motion", "2026-10-08T10:00:00", "2026-10-08T10:00:00"))
    con.commit(); con.close()


def test_a_removed_camera_stops_and_forgets_its_credentials(db_path, monkeypatch):
    _provisioned(db_path, monkeypatch)
    _mock_cloud_config(monkeypatch, [REMOVED])
    assert edge_camera_sync.sync_provisioned_cameras()["status"] == "ok"
    camera = _local_cameras(db_path)["cam-1"]
    assert (camera["status"], camera["camera_number"], camera["device_key"]) == ("removed", None, None)
    assert _local_credentials(db_path) == {}


def test_a_credential_still_pending_for_the_removed_device_is_deleted_too(db_path, monkeypatch):
    _provisioned(db_path, monkeypatch)
    _seed_pending_credential(db_path, "urn:uuid:aaaa", password="second-try")  # e.g. a re-provision in flight
    _mock_cloud_config(monkeypatch, [REMOVED])
    edge_camera_sync.sync_provisioned_cameras()
    assert _pending(db_path) == []


def test_other_cameras_keep_their_credentials(db_path, monkeypatch):
    _provisioned(db_path, monkeypatch, cameras=(ACTIVE, OTHER))
    _mock_cloud_config(monkeypatch, [REMOVED, OTHER])
    edge_camera_sync.sync_provisioned_cameras()
    assert set(_local_credentials(db_path)) == {"cam-2"}
    assert _local_cameras(db_path)["cam-2"]["camera_number"] == 2


def test_the_camera_row_and_its_events_are_kept(db_path, monkeypatch):
    _provisioned(db_path, monkeypatch)
    _seed_event(db_path, "cam-1")
    _mock_cloud_config(monkeypatch, [REMOVED])
    edge_camera_sync.sync_provisioned_cameras()
    assert "cam-1" in _local_cameras(db_path)
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT count(*) FROM detection_events WHERE camera_id='cam-1'").fetchone()[0] == 1


def test_the_credentials_are_deleted_with_secure_delete(db_path, monkeypatch):
    _provisioned(db_path, monkeypatch)
    statements = []
    real_forget = edge_camera_sync._forget_removed_camera

    def spy(db, camera_id, former_device_key):
        original_execute = db.execute

        class Recorder:
            def __getattr__(self, name):
                return getattr(db, name)

            def execute(self, sql, *args):
                statements.append(sql)
                return original_execute(sql, *args)
        return real_forget(Recorder(), camera_id, former_device_key)

    monkeypatch.setattr(edge_camera_sync, "_forget_removed_camera", spy)
    _mock_cloud_config(monkeypatch, [REMOVED])
    edge_camera_sync.sync_provisioned_cameras()
    assert statements[0] == "PRAGMA secure_delete=ON"
    assert statements.index("PRAGMA secure_delete=ON") < statements.index("DELETE FROM camera_credentials WHERE camera_id=?")


def test_later_syncs_do_nothing_more_and_a_never_seen_removed_camera_is_harmless(db_path, monkeypatch):
    _provisioned(db_path, monkeypatch)
    _mock_cloud_config(monkeypatch, [REMOVED])
    edge_camera_sync.sync_provisioned_cameras()
    calls = []
    monkeypatch.setattr(edge_camera_sync, "_forget_removed_camera", lambda *a: calls.append(a))
    edge_camera_sync.sync_provisioned_cameras()
    _mock_cloud_config(monkeypatch, [{**REMOVED, "id": "cam-never-here"}])
    edge_camera_sync.sync_provisioned_cameras()
    assert calls == []


def test_a_camera_added_again_gets_its_new_credentials(db_path, monkeypatch):
    # Re-adding the same device creates a NEW cloud camera (Green's contract).
    _provisioned(db_path, monkeypatch)
    _mock_cloud_config(monkeypatch, [REMOVED])
    edge_camera_sync.sync_provisioned_cameras()
    readded = {**ACTIVE, "id": "cam-3", "camera_number": 3}
    _seed_pending_credential(db_path, "urn:uuid:aaaa", password="new-password")
    _mock_cloud_config(monkeypatch, [REMOVED, readded])
    edge_camera_sync.sync_provisioned_cameras()
    assert set(_local_credentials(db_path)) == {"cam-3"}
    assert _local_cameras(db_path)["cam-1"]["status"] == "removed"
