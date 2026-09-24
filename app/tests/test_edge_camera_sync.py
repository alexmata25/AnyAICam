"""Cloud->edge camera-configuration sync (2026-09-12): sync_provisioned_
cameras() reconciliation half. See edge_camera_sync.py's own module
docstring for the full trace this closes (a successfully cloud-
provisioned camera never reaching the edge VMS's own local database, so
process_supervisor() never started a stream for it).

The cloud call itself (_control_plane_get) is monkeypatched to a canned
GET /api/appliance/configuration response for every test here -- the
real HTTP/auth plumbing around it is identical to recording_uploader.py's
already-tested _control_plane_get, and is not what's under test in this
file.
"""
import sqlite3

import pytest

import edge_camera_sync
from appliance_protocol import decrypt_camera_credentials, encrypt_camera_credentials
from database_backend import override_target
from partner_db import connection, initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_edge_camera_sync.db"


@pytest.fixture(autouse=True)
def _seeded_db(db_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    with override_target(sqlite_path=str(db_path)):
        yield


IDENTITY = {
    "appliance_id": "appl-ryzen", "cloud_id": "AIC-RYZEN0001",
    "credential": "ryzen-credential", "customer_id": "cust-1", "site_id": "site-1",
}


def _mock_identity(monkeypatch, identity=IDENTITY):
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: identity)


def _mock_cloud_config(monkeypatch, cameras):
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: {"cameras": cameras, "configuration_version": "x"})


def _seed_pending_credential(db_path, device_key, username="admin", password="hunter2"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "INSERT INTO pending_camera_credentials(device_key,encrypted_blob,created_at) VALUES(?,?,?)",
                (device_key, encrypt_camera_credentials(username, password), "2026-09-12T05:50:00"),
            )


def _local_cameras(db_path):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return {r["id"]: dict(r) for r in con.execute("SELECT * FROM cameras")}


def _local_credentials(db_path):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return {r["camera_id"]: dict(r) for r in con.execute("SELECT * FROM camera_credentials")}


def _pending(db_path):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM pending_camera_credentials")]


# --------------------------------------------------------------- gating


def test_cloud_role_is_a_no_op(db_path, monkeypatch):
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "cloud")
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result["status"] == "not_applicable"
    assert _local_cameras(db_path) == {}


def test_not_activated_yet_is_a_no_op(db_path, monkeypatch):
    _mock_identity(monkeypatch, None)
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result["status"] == "not_activated"


def test_cloud_unreachable_is_reported_not_raised(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda *a, **k: None)
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result["status"] == "unreachable"


# ------------------------------------------------------ metadata reconciliation


def test_cloud_camera_is_upserted_into_the_local_cameras_table(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
         "resolution": "2mp", "recording_mode": "motion", "people_counting_enabled": 0},
    ])
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result == {"status": "ok", "synced": 1, "credentials_moved": 0, "product_mode_restart_required": False, "rules_synced": None, "aac_voice_call_synced": None}
    cameras = _local_cameras(db_path)
    assert set(cameras) == {"cam-1"}
    assert cameras["cam-1"]["camera_number"] == 1
    assert cameras["cam-1"]["device_key"] == "urn:uuid:aaaa"
    assert cameras["cam-1"]["onvif_endpoint"] == "rtsp://192.168.0.38:554/ch1"
    assert cameras["cam-1"]["customer_id"] == "cust-1"
    assert cameras["cam-1"]["site_id"] == "site-1"
    assert cameras["cam-1"]["appliance_id"] == "appl-ryzen"


def test_local_recording_mode_and_configurable_fields_are_synced(db_path, monkeypatch):
    """The exact same sync path people_counting_enabled etc. already
    prove -- local_recording_mode (and its 4 configurable seconds
    fields) must reach the LOCAL cameras table process_supervisor()
    actually reads, not just exist on the cloud side."""
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
         "resolution": "2mp", "recording_mode": None, "people_counting_enabled": 0,
         "local_recording_mode": "event", "local_recording_pre_roll_seconds": 10,
         "local_recording_post_roll_seconds": 15, "local_recording_merge_gap_seconds": 20,
         "local_recording_max_event_seconds": 600},
    ])
    edge_camera_sync.sync_provisioned_cameras()
    cameras = _local_cameras(db_path)
    assert cameras["cam-1"]["local_recording_mode"] == "event"
    assert cameras["cam-1"]["local_recording_pre_roll_seconds"] == 10
    assert cameras["cam-1"]["local_recording_post_roll_seconds"] == 15
    assert cameras["cam-1"]["local_recording_merge_gap_seconds"] == 20
    assert cameras["cam-1"]["local_recording_max_event_seconds"] == 600


def test_local_recording_mode_defaults_to_null_when_not_reported(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
         "resolution": "2mp", "recording_mode": None, "people_counting_enabled": 0},
    ])
    edge_camera_sync.sync_provisioned_cameras()
    cameras = _local_cameras(db_path)
    assert cameras["cam-1"]["local_recording_mode"] is None


def test_re_sync_updates_in_place_never_duplicates(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    cam = {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
           "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
           "resolution": "2mp", "recording_mode": "motion", "people_counting_enabled": 0}
    _mock_cloud_config(monkeypatch, [cam])
    edge_camera_sync.sync_provisioned_cameras()
    renamed = {**cam, "name": "Front Door"}
    _mock_cloud_config(monkeypatch, [renamed])
    edge_camera_sync.sync_provisioned_cameras()
    cameras = _local_cameras(db_path)
    assert len(cameras) == 1
    assert cameras["cam-1"]["name"] == "Front Door"


def test_restart_safe_a_fresh_process_re_running_sync_reaches_the_same_state(db_path, monkeypatch):
    """No in-memory state is required for correctness -- everything this
    function needs (identity, pending credentials, local camera rows) is
    already durable, so calling it again from what is effectively a brand
    new process (a fresh sync_state module dict) behaves identically."""
    _mock_identity(monkeypatch)
    cam = {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
           "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
           "resolution": "2mp", "recording_mode": "motion", "people_counting_enabled": 0}
    _mock_cloud_config(monkeypatch, [cam])
    _seed_pending_credential(db_path, "urn:uuid:aaaa")
    first = edge_camera_sync.sync_provisioned_cameras()
    edge_camera_sync.sync_state.clear()  # simulate a fresh process
    second = edge_camera_sync.sync_provisioned_cameras()
    assert first == {"status": "ok", "synced": 1, "credentials_moved": 1, "product_mode_restart_required": False, "rules_synced": None, "aac_voice_call_synced": None}
    assert second == {"status": "ok", "synced": 1, "credentials_moved": 0, "product_mode_restart_required": False, "rules_synced": None, "aac_voice_call_synced": None}
    assert len(_local_credentials(db_path)) == 1


# ------------------------------------------------------ credential reconciliation


def test_pending_credential_is_moved_into_camera_credentials_once_camera_id_is_known(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _seed_pending_credential(db_path, "urn:uuid:aaaa", username="admin", password="hunter2-secret")
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": None, "resolution": None,
         "recording_mode": None, "people_counting_enabled": 0},
    ])
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result == {"status": "ok", "synced": 1, "credentials_moved": 1, "product_mode_restart_required": False, "rules_synced": None, "aac_voice_call_synced": None}
    assert _pending(db_path) == []
    creds = _local_credentials(db_path)
    assert set(creds) == {"cam-1"}
    decrypted = decrypt_camera_credentials(creds["cam-1"]["encrypted_blob"])
    assert decrypted == {"username": "admin", "password": "hunter2-secret"}


def test_credential_never_appears_in_the_returned_stats(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _seed_pending_credential(db_path, "urn:uuid:aaaa", username="admin", password="hunter2-secret")
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": None, "resolution": None,
         "recording_mode": None, "people_counting_enabled": 0},
    ])
    result = edge_camera_sync.sync_provisioned_cameras()
    assert "hunter2-secret" not in str(result)
    assert "admin" not in str(result)


def test_no_matching_pending_credential_leaves_camera_without_one(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:never-arrived", "onvif_endpoint": None, "resolution": None,
         "recording_mode": None, "people_counting_enabled": 0},
    ])
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result == {"status": "ok", "synced": 1, "credentials_moved": 0, "product_mode_restart_required": False, "rules_synced": None, "aac_voice_call_synced": None}
    assert _local_credentials(db_path) == {}


def test_existing_local_credential_is_never_overwritten_by_a_stale_pending_row(db_path, monkeypatch):
    _mock_identity(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,device_key,created_at) "
                "VALUES('cam-1','cust-1','site-1','appl-ryzen','Camera 1',1,'configured','urn:uuid:aaaa','2026-01-01')"
            )
            db.execute(
                "INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)",
                ("cam-1", encrypt_camera_credentials("original", "original-secret"), "2026-01-01", "2026-01-01"),
            )
    _seed_pending_credential(db_path, "urn:uuid:aaaa", username="attacker", password="replayed-old-password")
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": None, "resolution": None,
         "recording_mode": None, "people_counting_enabled": 0},
    ])
    edge_camera_sync.sync_provisioned_cameras()
    creds = _local_credentials(db_path)
    decrypted = decrypt_camera_credentials(creds["cam-1"]["encrypted_blob"])
    assert decrypted == {"username": "original", "password": "original-secret"}


# --------------------------------------------------------------- removal safety


def test_a_camera_no_longer_reported_by_the_cloud_is_left_untouched_locally(db_path, monkeypatch):
    """Deprovisioning is an explicit, separate, future design decision --
    this fix must never silently delete a locally configured camera."""
    _mock_identity(monkeypatch)
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": "rtsp://192.168.0.38:554/ch1",
         "resolution": "2mp", "recording_mode": "motion", "people_counting_enabled": 0},
    ])
    edge_camera_sync.sync_provisioned_cameras()
    assert set(_local_cameras(db_path)) == {"cam-1"}

    _mock_cloud_config(monkeypatch, [])  # cloud now reports zero cameras for this appliance
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result == {"status": "ok", "synced": 0, "credentials_moved": 0, "product_mode_restart_required": False, "rules_synced": None, "aac_voice_call_synced": None}
    assert set(_local_cameras(db_path)) == {"cam-1"}  # still present, untouched


# ------------------------------------------------------- cross-appliance isolation


def test_a_different_appliances_local_rows_are_never_touched_by_this_appliances_sync(db_path, monkeypatch):
    """Simulates the (never-normal, but defensible-to-prove) case of a
    second appliance's rows existing in the same local database --
    real cloud-configuration responses are already server-side scoped to
    the calling appliance's own appliance_id (see appliance_cloud.py's
    appliance_configuration()), so this proves the reconciliation itself
    adds no additional cross-appliance leakage on top of that."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,device_key,created_at) "
                "VALUES('cam-other','cust-2','site-2','appl-other','Other Camera',1,'configured','urn:uuid:other',?)",
                ("2026-01-01",),
            )
            db.execute(
                "INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)",
                ("cam-other", encrypt_camera_credentials("other-user", "other-secret"), "2026-01-01", "2026-01-01"),
            )
    _seed_pending_credential(db_path, "urn:uuid:other", username="attacker", password="should-never-be-used")
    _mock_identity(monkeypatch)  # identity is for appl-ryzen, not appl-other
    _mock_cloud_config(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
         "device_key": "urn:uuid:aaaa", "onvif_endpoint": None, "resolution": None,
         "recording_mode": None, "people_counting_enabled": 0},
    ])
    edge_camera_sync.sync_provisioned_cameras()

    cameras = _local_cameras(db_path)
    assert cameras["cam-other"]["appliance_id"] == "appl-other"  # never touched/reassigned
    other_creds = decrypt_camera_credentials(_local_credentials(db_path)["cam-other"]["encrypted_blob"])
    assert other_creds == {"username": "other-user", "password": "other-secret"}  # never overwritten
