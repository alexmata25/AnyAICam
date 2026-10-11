"""video-pipeline P1 follow-up (2026-10-11): the measured camera resolution
(cameras.measured_*) is never overwritten by the plan-label writes
(partner onboarding's hard-coded '2mp', the customer setup wizard's
PUT /api/customer/cameras resolution=<plan label>), and the customer-facing
camera list and setup wizard show the MEASURED video, not the label."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import video_profile
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_measured_resolution.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _cookie():
    import partner_portal
    return partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)


def _seed(db_path):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Partner','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Cust','owner@example.test','active','2026-01-01')")
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                     "VALUES('user-1','partner-1','owner@example.test','Owner','customer_owner','x',1,'cust-1','2026-01-01')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home','2026-01-01')")
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,created_at) "
                     "VALUES('appl-1','cust-1','site-1','AIC-VIDEO001','pending','online','2026-01-01')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,resolution,created_at,device_key,camera_number,"
                     "measured_width,measured_height,measured_codec,measured_fps,measured_resolution_class,measured_video_at) "
                     "VALUES('cam-1','cust-1','site-1','appl-1','Driveway','configured','2mp','2026-02-01','urn:uuid:dev-1',1,"
                     "3840,2160,'H265',20.0,'8mp','2026-10-11T00:00:00')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,resolution,created_at,device_key,camera_number) "
                     "VALUES('cam-2','cust-1','site-1','appl-1','Garage','configured','2mp','2026-02-01','urn:uuid:dev-2',2)")
        conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
                     "VALUES('ent-1','cust-1','camera_slots_local',8,'active','2026-01-01','2026-01-01')")
        conn.commit()


def _measured(db_path, camera_id):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone())


def test_wizard_label_write_never_overwrites_the_measured_resolution(client, db_path):
    _seed(db_path)
    response = client.put("/api/customer/cameras", json={"appliance_id": "appl-1", "cameras": [
        {"id": "cam-1", "name": "Driveway", "site_id": "site-1", "resolution": "2mp", "status": "configured"}]},
        cookies={"anyaicam_partner_session": _cookie()})
    assert response.status_code == 200
    row = _measured(db_path, "cam-1")
    assert row["resolution"] == "2mp"                                  # the label write itself
    assert (row["measured_width"], row["measured_height"], row["measured_resolution_class"]) == (3840, 2160, "8mp")


def test_customer_camera_list_shows_measured_video(client, db_path):
    _seed(db_path)
    cameras = client.get("/api/customer/cameras", params={"appliance_id": "appl-1"},
                         cookies={"anyaicam_partner_session": _cookie()}).json()["cameras"]
    by_id = {c["id"]: c for c in cameras}
    assert by_id["cam-1"]["video_resolution"] == "3840x2160 H265 20 fps (8MP)"
    assert by_id["cam-1"]["resolution_source"] == "measured" and by_id["cam-1"]["measured_width"] == 3840
    assert by_id["cam-2"]["video_resolution"] is None and by_id["cam-2"]["resolution_source"] == "not_measured"
    assert by_id["cam-2"]["video_display"] == "Not yet measured"


def test_display_resolution_helper():
    assert video_profile.display_resolution({"measured_width": 2592, "measured_height": 1944, "measured_codec": "H264"}) == "2592x1944 H264 (5MP)"
    assert video_profile.display_resolution({"measured_width": 1920, "measured_height": 1080, "measured_codec": "H264", "measured_fps": 25,
                                             "measured_source": "configured"}) == "1920x1080 H264 25 fps (2MP) - configured (not yet measured)"
    assert video_profile.display_video({}) == "Not yet measured"
    assert video_profile.display_resolution({"measured_width": 4000, "measured_height": 3000}) == "4000x3000 (12.0MP)"
    assert video_profile.display_resolution({"resolution": "2mp"}) is None
    assert video_profile.display_resolution(None) is None
