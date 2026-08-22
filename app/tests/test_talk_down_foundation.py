"""Talk-down (push-to-talk) capability foundation: tests for the new
DB columns/table, appliance_cloud.py's extended POST
/api/appliance/cameras capability persistence, talk_sessions.py's
start/stop authorization and capability gating, and
live_view_page.py's capability-driven mic rendering.

Explicitly does NOT test any audio transport -- none exists yet. See
the milestone report for what remains.

Same import/isolation convention as this project's other
appliance_cloud.py/live_view tests: redirects to a throwaway sqlite
file via override_target() before importing anything that triggers
partner_db's import-time schema init.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_talk_down_foundation.db"):
    import appliance_cloud
    import talk_sessions
    import live_view_page
    import partner_portal
    from partner_db import connection, password_hash


# ------------------------------------------------------------- seeding helpers

def _owner_email_for(customer_id):
    return f"owner-{customer_id}@example.test"


def _seed_tenant(db, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", cloud_id="AIC-TEST0001"):
    now = "2026-08-21T00:00:00"
    owner_email = _owner_email_for(customer_id)
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, "partner-1", "Test Co", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, now))
    # _authorized_camera()/_authorized_talk_camera() require a partner_users
    # row matching the identity's email for EVERY role (including
    # customer_owner) -- it's how they derive user_id, not just how
    # customer_viewer's permission grant is checked.
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"owner-user-{customer_id}", "partner-1", owner_email, "Owner", "customer_owner", "x", 1, customer_id, now),
    )


def _seed_camera(db, camera_id, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_number=1, name="Camera", talk_down_supported=None):
    now = "2026-08-21T00:00:00"
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,talk_down_supported,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, name, camera_number, talk_down_supported, now),
    )


def _seed_owner_credential(appliance_id, credential="test-credential-1"):
    return credential  # appliance_cloud's authenticate_appliance uses appliance_credentials -- seeded separately below


def _seed_appliance_credential(db, appliance_id, credential):
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-08-21T00:00:00"))


def _seed_viewer(db, user_id, email, customer_id, camera_id, can_talk=0):
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Viewer", "customer_viewer", "x", 1, customer_id, "2026-08-21T00:00:00"),
    )
    db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_talk) VALUES(?,?,1,?)", (user_id, camera_id, can_talk))


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token(_owner_email_for(customer_id), "customer_owner", None, customer_id, None)


def _viewer_cookie(email, customer_id="cust-1"):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_talk_down.db"


@pytest.fixture()
def appliance_client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture()
def customer_client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        talk_sessions.register_talk_session_routes(app)
        live_view_page.register_live_view_page_routes(app, page_shell=lambda title, active, content, scripts="": f"<html><body>{content}{scripts}</body></html>")
        with TestClient(app, follow_redirects=False) as test_client:
            yield test_client


def _cam(db_path, camera_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()


def _talk_session_count(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT COUNT(*) AS c FROM customer_talk_sessions").fetchone()["c"]


# --------------------------------------------------------------- migration

def test_migration_adds_talk_down_columns_to_cameras(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(cameras)").fetchall()}
    assert {"talk_down_supported", "talk_down_metadata", "talk_down_verified_at"}.issubset(columns)


def test_migration_adds_can_talk_to_customer_camera_permissions(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(customer_camera_permissions)").fetchall()}
    assert "can_talk" in columns


def test_migration_creates_customer_talk_sessions_table(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(customer_talk_sessions)").fetchall()}
    expected = {"id", "customer_id", "site_id", "camera_id", "user_id", "requested_by", "role", "state", "requested_at", "ended_at", "expires_at"}
    assert expected.issubset(columns)


# --------------------------------------------- appliance-side capability persistence

def test_talk_down_supported_true_is_persisted(appliance_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=None)
            _seed_appliance_credential(db, "appl-1", "cred")

    response = appliance_client.post(
        "/api/appliance/cameras",
        json={"cameras": [{"id": "cam-1", "name": "Camera 1", "talk_down": {"supported": True, "metadata": {"audio_output_token": "AudioOutput_1", "send_primacy": "HalfDuplex"}}}]},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert response.status_code == 200
    camera = _cam(db_path, "cam-1")
    assert camera["talk_down_supported"] == 1
    assert "AudioOutput_1" in camera["talk_down_metadata"]
    assert camera["talk_down_verified_at"] is not None


def test_talk_down_supported_false_is_persisted(appliance_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=None)
            _seed_appliance_credential(db, "appl-1", "cred")

    appliance_client.post(
        "/api/appliance/cameras",
        json={"cameras": [{"id": "cam-1", "talk_down": {"supported": False}}]},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert _cam(db_path, "cam-1")["talk_down_supported"] == 0


def test_missing_talk_down_key_leaves_existing_value_untouched(appliance_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    # An older appliance reporting camera status with no talk_down key at all.
    appliance_client.post(
        "/api/appliance/cameras",
        json={"cameras": [{"id": "cam-1", "online": True}]},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert _cam(db_path, "cam-1")["talk_down_supported"] == 1  # unchanged, not reset to NULL


def test_rescan_updates_capability(appliance_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=None)
            _seed_appliance_credential(db, "appl-1", "cred")

    appliance_client.post("/api/appliance/cameras", json={"cameras": [{"id": "cam-1", "talk_down": {"supported": False}}]}, headers=_auth_headers("appl-1", "cred"))
    assert _cam(db_path, "cam-1")["talk_down_supported"] == 0

    appliance_client.post("/api/appliance/cameras", json={"cameras": [{"id": "cam-1", "talk_down": {"supported": True}}]}, headers=_auth_headers("appl-1", "cred"))
    assert _cam(db_path, "cam-1")["talk_down_supported"] == 1  # a later rescan overwrites the earlier result


def test_camera_belonging_to_a_different_appliance_is_never_updated(appliance_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            # A second appliance under the SAME customer -- reuses cust-1/site-1,
            # only adds the appliance row itself (not a second customer/owner).
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-2", "cust-1", "site-1", "AIC-OTHER0001", "2026-08-21T00:00:00"))
            _seed_camera(db, "cam-victim", appliance_id="appl-2", talk_down_supported=None)
            _seed_appliance_credential(db, "appl-1", "cred")

    # appl-1 is authenticated, but tries to report capability for a camera
    # that actually belongs to appl-2.
    appliance_client.post(
        "/api/appliance/cameras",
        json={"cameras": [{"id": "cam-victim", "talk_down": {"supported": True}}]},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert _cam(db_path, "cam-victim")["talk_down_supported"] is None  # untouched


# --------------------------------------------------------- talk_sessions.py authorization

def test_owner_can_start_session_for_supported_camera(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)

    response = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "requested"
    assert _talk_session_count(db_path) == 1


def test_start_rejected_for_unsupported_camera(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=0)

    response = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 409
    assert "not supported" in response.json()["detail"].lower()
    assert _talk_session_count(db_path) == 0


def test_start_rejected_for_unverified_camera(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=None)

    response = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 409
    assert "not verified" in response.json()["detail"].lower()
    assert _talk_session_count(db_path) == 0


def test_start_rejected_unauthenticated(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)

    response = customer_client.post("/api/customer/cameras/cam-1/talk/start")
    assert response.status_code == 403
    assert _talk_session_count(db_path) == 0


def test_start_rejected_for_camera_belonging_to_a_different_customer(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_tenant(db, customer_id="cust-2", site_id="site-2", appliance_id="appl-2", cloud_id="AIC-OTHER0002")
            _seed_camera(db, "cam-victim", customer_id="cust-2", site_id="site-2", appliance_id="appl-2", talk_down_supported=1)

    # A real, valid session for cust-1 -- never trusted to reach cust-2's camera.
    response = customer_client.post("/api/customer/cameras/cam-victim/talk/start", cookies={partner_portal.SESSION_COOKIE: _owner_cookie(customer_id="cust-1")})
    assert response.status_code == 404
    assert _talk_session_count(db_path) == 0


def test_viewer_without_can_talk_is_rejected(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_viewer(db, "viewer-1", "viewer@example.test", "cust-1", "cam-1", can_talk=0)

    response = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test")})
    assert response.status_code == 403
    assert _talk_session_count(db_path) == 0


def test_viewer_with_can_talk_and_supported_camera_succeeds(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_viewer(db, "viewer-1", "viewer@example.test", "cust-1", "cam-1", can_talk=1)

    response = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test")})
    assert response.status_code == 200


def test_stop_is_idempotent(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)

    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    start = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies=cookie)
    session_id = start.json()["session_id"]
    first_stop = customer_client.post(f"/api/customer/talk/sessions/{session_id}/stop", cookies=cookie)
    second_stop = customer_client.post(f"/api/customer/talk/sessions/{session_id}/stop", cookies=cookie)
    assert first_stop.status_code == 200
    assert second_stop.status_code == 200
    assert first_stop.json()["status"] == second_stop.json()["status"] == "stopped"


def test_stop_rejected_for_session_belonging_to_a_different_customer(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_tenant(db, customer_id="cust-2", site_id="site-2", appliance_id="appl-2", cloud_id="AIC-OTHER0003")
            _seed_camera(db, "cam-1", talk_down_supported=1)

    start = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _owner_cookie(customer_id="cust-1")})
    session_id = start.json()["session_id"]
    stop_as_other_customer = customer_client.post(f"/api/customer/talk/sessions/{session_id}/stop", cookies={partner_portal.SESSION_COOKIE: _owner_cookie(customer_id="cust-2")})
    assert stop_as_other_customer.status_code == 404


def test_one_cameras_session_does_not_affect_another(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", camera_number=1, talk_down_supported=1)
            _seed_camera(db, "cam-2", camera_number=2, talk_down_supported=1)

    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    start1 = customer_client.post("/api/customer/cameras/cam-1/talk/start", cookies=cookie)
    customer_client.post(f"/api/customer/talk/sessions/{start1.json()['session_id']}/stop", cookies=cookie)

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            cam2_sessions = db.execute("SELECT COUNT(*) AS c FROM customer_talk_sessions WHERE camera_id='cam-2'").fetchone()["c"]
    assert cam2_sessions == 0  # camera 2 was never touched by camera 1's session lifecycle


# --------------------------------------------------------- capability -> render mapping

def test_talk_down_state_mapping_matches_required_tooltips():
    assert live_view_page._talk_down_state(1) == {"enabled": True, "tooltip": None}
    assert live_view_page._talk_down_state(0) == {"enabled": False, "tooltip": "Talk-down not supported by this camera"}
    assert live_view_page._talk_down_state(None) == {"enabled": False, "tooltip": "Talk-down capability not verified"}


def test_customer_live_cameras_capability_is_independent_per_camera(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", camera_number=1, talk_down_supported=1)
            _seed_camera(db, "cam-2", camera_number=2, talk_down_supported=0)
            _seed_camera(db, "cam-3", camera_number=3, talk_down_supported=None)
            identity = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"}
            cameras = live_view_page._customer_live_cameras(db, identity)

    by_id = {camera["id"]: camera for camera in cameras}
    assert by_id["cam-1"]["enabled"] is True
    assert by_id["cam-2"]["enabled"] is False and by_id["cam-2"]["tooltip"] == "Talk-down not supported by this camera"
    assert by_id["cam-3"]["enabled"] is False and by_id["cam-3"]["tooltip"] == "Talk-down capability not verified"


def test_grid_page_renders_disabled_mic_for_unsupported_camera(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=0)

    response = customer_client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="talk-mic-cam-1"' in response.text
    assert "disabled" in response.text
    assert "Talk-down not supported by this camera" in response.text


def test_grid_page_renders_enabled_mic_for_supported_camera(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)

    response = customer_client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    html = response.text
    tile_start = html.index('id="talk-mic-cam-1"')
    tile_fragment = html[max(0, tile_start - 20):tile_start + 400]
    assert "disabled" not in tile_fragment.split(">")[0] + tile_fragment.split(">")[1]


def test_single_camera_page_renders_capability_state(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=None)

    response = customer_client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="talk-mic-cam-1"' in response.text
    assert "Talk-down capability not verified" in response.text


def test_pointer_lifecycle_all_wired_to_stop_on_both_pages(customer_client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)

    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    grid_html = customer_client.get("/customer-live", cookies=cookie).text
    single_html = customer_client.get("/customer/cameras/cam-1/live", cookies=cookie).text
    for html in (grid_html, single_html):
        assert "pointerup" in html and "stopTalk" in html
        assert "pointercancel" in html and "'pointercancel'" in html
        assert "pointerleave" in html
        assert "pointerdown" in html and "startTalk" in html


# --------------------------------------------------------- no hardcoded camera numbers/models

def test_no_camera_number_or_model_hardcoded_in_new_code():
    import inspect
    source = inspect.getsource(talk_sessions) + inspect.getsource(live_view_page)
    forbidden_models = ("CMIP3342WI", "CMIP1042W", "CMIP3342WI-28SDL", "CMIP1042W-28MA")
    for model in forbidden_models:
        assert model not in source
    # No literal per-camera-number branching like "camera_number in (1, 2)"
    # or "camera_number == 3" anywhere in the new capability/session code.
    import re
    assert not re.search(r"camera_number\s*(==|in)\s*[\(\d]", source)


# --------------------------------------------------------- structural isolation

def test_talk_sessions_never_touches_recording_analytics_yolo_hls():
    import ast
    import inspect
    forbidden = {
        "ai_person_detector", "motion_detector", "save_yolo_events", "append_analytics_event",
        "analytics_rules_engine", "analytics_sync", "recording_uploader", "start_recording", "start_live_stream",
    }
    tree = ast.parse(inspect.getsource(talk_sessions))
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            referenced.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            referenced.add(node.module)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    overlap = referenced & forbidden
    assert not overlap, f"talk_sessions.py unexpectedly references {overlap}"


def test_no_audio_bytes_are_ever_sent_by_this_module():
    """Structural proof of the transport boundary: talk_sessions.py has
    no socket/ffmpeg/subprocess/rtsp code at all -- it only reads and
    writes SQL rows."""
    import inspect
    source = inspect.getsource(talk_sessions)
    for forbidden in ("socket", "subprocess", "ffmpeg", "rtsp://", "ONVIF", "onvif"):
        assert forbidden not in source
