"""People -> Add / edit person (2026-10-01): Take Photo, Upload Photo, Enroll
from Camera and Face Access on one phone-first page; only administrators
(facial.manage) can change anything, and the new APIs follow the Face
Access add-on like the pages do."""
import sqlite3

import partner_portal
from test_notification_settings import _owner_cookie, _seed_owner, _seed_tenant, _viewer_cookie, db_path, http_client  # noqa: F401


def _seed(db_path, *, face_access=True, viewer=True):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    if viewer:
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,camera_access_mode,created_at) "
                     "VALUES('viewer-1','partner-1','viewer@example.test','Viewer','customer_viewer','x',1,'cust-1','all','2026-01-01')")
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,camera_number,name,door_access_enabled,door_relay_channel,created_at) "
                 "VALUES('lobby','cust-1','site-1',1,'Lobby Door',1,1,'2026-01-01')")
    if face_access:
        conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at,updated_at) "
                     "VALUES('fa','cust-1',NULL,'facial_recognition','active',1,'2026-10-01','2026-10-01')")
    conn.execute("INSERT INTO facial_people(id,customer_id,display_name,status,created_at,updated_at) VALUES('person_1','cust-1','Ana','active','2026-10-01','2026-10-01')")
    conn.commit()
    conn.close()


OWNER = {partner_portal.SESSION_COOKIE: None}


def _owner():
    return {partner_portal.SESSION_COOKIE: _owner_cookie()}


def _viewer():
    return {partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test")}


def test_the_owner_page_offers_all_three_enrollment_methods_and_face_access(http_client, db_path):
    _seed(db_path)
    page = http_client.get("/aac/people/enroll?person_id=person_1", cookies=_owner()).text
    for marker in ('data-tab="take"', 'data-tab="upload"', 'data-tab="camera"', 'id="cam-select"', ">Lobby Door<",
                   'id="a-enabled"', 'id="a-unit"', 'id="a-site"', 'id="a-starts"', 'id="a-expires"', 'id="a-save"'):
        assert marker in page, marker
    assert "getUserMedia" in page and "/api/aac/face-preview" in page and "/live/still.jpg" in page
    # Enroll from Camera keeps the live stream watched while capturing (no frames otherwise).
    assert page.index("/live/playlist.m3u8") < page.index("/live/still.jpg")


def test_enroll_from_camera_lists_real_cameras_only(http_client, db_path):
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,camera_number,name,status,created_at) VALUES('slot','cust-1','site-1',NULL,'Camera 6','pending_installation','2026-01-01')")
    conn.commit(); conn.close()
    page = http_client.get("/aac/people/enroll?person_id=person_1", cookies=_owner()).text
    select = page[page.index('id="cam-select"'):page.index("</select>", page.index('id="cam-select"'))]
    assert "Lobby Door" in select and "Camera 6" not in select


def test_a_viewer_sees_but_cannot_change_anything(http_client, db_path):
    _seed(db_path)
    page = http_client.get("/aac/people/enroll?person_id=person_1", cookies=_viewer()).text
    assert 'id="a-save"' not in page and 'id="e-create"' not in page and 'data-tab="take"' not in page
    assert http_client.get("/api/aac/people/person_1/access", cookies=_viewer()).status_code == 200
    assert http_client.put("/api/aac/people/person_1/access", json={"access_enabled": False}, cookies=_viewer()).status_code == 403


def test_the_owner_saves_face_access_through_the_api(http_client, db_path):
    _seed(db_path)
    response = http_client.put("/api/aac/people/person_1/access", cookies=_owner(),
                               json={"unit": "12", "access_enabled": True, "doors": [{"camera_id": "lobby", "allowed": True, "days": ["mon"]}]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unit"] == "12" and body["doors"][0]["allowed"] and body["doors"][0]["days"] == ["mon"]
    bad = http_client.put("/api/aac/people/person_1/access", cookies=_owner(), json={"access_expires_on": "tomorrow"})
    assert bad.status_code == 400 and "date" in bad.json()["detail"]


def test_without_the_face_access_add_on_the_new_apis_refuse(http_client, db_path):
    _seed(db_path, face_access=False)
    assert http_client.put("/api/aac/people/person_1/access", json={}, cookies=_owner()).status_code == 403
    assert http_client.post("/api/aac/face-preview", json={"image_base64": "x"}, cookies=_owner()).status_code == 403
    assert "Face Access required" in http_client.get("/aac/people/enroll", cookies=_owner()).text


def test_the_people_list_loads_by_itself_and_shows_unit_and_access(http_client, db_path):
    _seed(db_path)
    page = http_client.get("/aac/people", cookies=_owner()).text
    assert "if(FIXED_CUSTOMER_ID!==null)aacLoadPeople();" in page and "Face Access on" in page and "'Unit '" in page
