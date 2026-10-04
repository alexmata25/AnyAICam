"""A camera provisioned without credentials streams (2026-10-04).

Samsung clean-machine acceptance: provisioning accepts a camera that needs
no username/password, but app/main.py's _provisioned_camera_stream()
returned None unless a camera_credentials row existed, so the supervisor
never started it (cameras_online=0, recording_workers=0). A camera with no
credential row now streams its URL as-is; an authenticated camera is
unchanged, and a stored credential that cannot be decrypted still stops
(never silently becomes anonymous)."""
import sqlite3

import pytest
from cryptography.fernet import Fernet

from database_backend import override_target


@pytest.fixture()
def edge_db(tmp_path, monkeypatch):
    import vms_capacity
    db_path = tmp_path / "edge.db"
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setattr(vms_capacity, "STATE_FILE", tmp_path / "vms_capacity.json")
    vms_capacity.persist_capacity({"camera_slot_quantity": 8, "plan_camera_slots": 8, "vms_license_capacity": 8})
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        monkeypatch.setattr(main, "get_camera_numbers", lambda customer_id=None: [1, 2, 3])
        yield db_path, main


def _camera(db_path, camera_id, number, uri):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,onvif_endpoint,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (camera_id, "cust-1", "site-1", "appl-1", "Camera", number, "configured", uri, "2026-10-04"))
    conn.commit()
    conn.close()


def _credential(db_path, camera_id, blob):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)",
                 (camera_id, blob, "2026-10-04", "2026-10-04"))
    conn.commit()
    conn.close()


def test_a_credential_free_camera_streams_on_its_own_port_and_path(edge_db):
    db_path, main = edge_db
    _camera(db_path, "cam-anon", 1, "rtsp://192.168.253.10:8554/testcamera")
    with override_target(sqlite_path=str(db_path)):
        assert main._provisioned_camera_stream(1) == {"rtsp_url": "rtsp://192.168.253.10:8554/testcamera",
                                                      "username": "", "password": ""}
        assert main.camera_url(1) == "rtsp://192.168.253.10:8554/testcamera"  # no userinfo, port kept


def test_an_authenticated_camera_still_uses_its_stored_credentials(edge_db):
    db_path, main = edge_db
    from appliance_protocol import encrypt_camera_credentials
    _camera(db_path, "cam-auth", 2, "rtsp://192.168.253.11:554/Streaming/Channels/101")
    _credential(db_path, "cam-auth", encrypt_camera_credentials("viewer", "p@ss word"))
    with override_target(sqlite_path=str(db_path)):
        assert main._provisioned_camera_stream(2)["username"] == "viewer"
        assert main.camera_url(2) == "rtsp://viewer:p%40ss%20word@192.168.253.11:554/Streaming/Channels/101"


def test_a_stored_credential_that_cannot_be_decrypted_never_becomes_anonymous(edge_db):
    db_path, main = edge_db
    _camera(db_path, "cam-broken", 3, "rtsp://192.168.253.12:554/stream")
    _credential(db_path, "cam-broken", "not-a-valid-token")
    with override_target(sqlite_path=str(db_path)):
        assert main._provisioned_camera_stream(3) is None
        with pytest.raises(main.CameraNotConfiguredError):
            main.camera_url(3)


def test_a_camera_without_a_stream_uri_is_still_not_configured(edge_db):
    db_path, main = edge_db
    _camera(db_path, "cam-pending", 1, "")
    with override_target(sqlite_path=str(db_path)):
        assert main._provisioned_camera_stream(1) is None
