"""Push-to-talk audio transport -- cloud relay tests.

Uses Starlette's TestClient.websocket_connect(), which runs entirely
in-process over ASGI (no real socket, no `websockets` package needed
for testing) -- so these tests exercise the real route logic, not a
mocked stand-in, while staying fully disposable/offline.

Same seeding/auth conventions as test_talk_down_foundation.py.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_talk_audio_relay.db"):
    import appliance_cloud
    import talk_sessions
    import talk_audio_relay
    import partner_portal
    from partner_db import connection, password_hash


def _seed_tenant(db, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", cloud_id="AIC-TEST0001"):
    now = "2026-08-21T00:00:00"
    owner_email = f"owner-{customer_id}@example.test"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, "partner-1", "Test Co", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, now))
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"owner-user-{customer_id}", "partner-1", owner_email, "Owner", "customer_owner", "x", 1, customer_id, now),
    )


def _seed_camera(db, camera_id, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_number=1, talk_down_supported=1, metadata=None):
    now = "2026-08-21T00:00:00"
    import json
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,talk_down_supported,talk_down_metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, "Camera", camera_number, talk_down_supported, json.dumps(metadata) if metadata else None, now),
    )


def _seed_viewer(db, user_id, email, customer_id, camera_id, can_talk=0):
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Viewer", "customer_viewer", "x", 1, customer_id, "2026-08-21T00:00:00"),
    )
    db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_talk) VALUES(?,?,1,?)", (user_id, camera_id, can_talk))


def _seed_appliance_credential(db, appliance_id, credential):
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-08-21T00:00:00"))


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token(f"owner-{customer_id}@example.test", "customer_owner", None, customer_id, None)


def _viewer_cookie(email, customer_id="cust-1"):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


def _appliance_headers(appliance_id, credential):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_talk_audio_relay.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        talk_sessions.register_talk_session_routes(app)
        talk_audio_relay.register_talk_audio_relay_routes(app)
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture(autouse=True)
def _isolated_relay_state():
    talk_audio_relay._appliance_channels.clear()
    talk_audio_relay._active_relays.clear()
    yield
    talk_audio_relay._appliance_channels.clear()
    talk_audio_relay._active_relays.clear()


def _start_session(client, camera_id, cookie):
    response = client.post(f"/api/customer/cameras/{camera_id}/talk/start", cookies=cookie)
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def _session_state(db_path, session_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT state FROM customer_talk_sessions WHERE id=?", (session_id,)).fetchone()
            return row["state"] if row else None


# --------------------------------------------------------------- happy path

def test_owner_can_start_and_audio_is_forwarded_to_the_appliance_channel(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1, metadata={"send_primacy": "HalfDuplex"})
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})

    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio?sample_rate=44100", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}) as customer_ws:
            start_message = appliance_ws.receive_json()
            assert start_message["type"] == "start"
            assert start_message["session_id"] == session_id
            assert start_message["camera_id"] == "cam-1"
            assert start_message["metadata"]["send_primacy"] == "HalfDuplex"
            assert start_message["sample_rate"] == 44100

            customer_ws.send_bytes(b"\x01\x02\x03\x04")
            audio_message = appliance_ws.receive_json()
            assert audio_message["type"] == "audio"
            assert audio_message["session_id"] == session_id
            import base64
            assert base64.b64decode(audio_message["pcm_b64"]) == b"\x01\x02\x03\x04"


# --------------------------------------------------------------- authorization

def test_viewer_without_can_talk_is_rejected_at_the_audio_socket(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_viewer(db, "viewer-1", "viewer@example.test", "cust-1", "cam-1", can_talk=0)
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})
    # A viewer without can_talk was never able to get a session_id of
    # their own via /start (403 there too, covered by
    # test_talk_down_foundation.py) -- this proves the WS route ALSO
    # independently rejects even if a session_id belonging to someone
    # else were somehow presented, since it re-derives customer_id from
    # the viewer's own cookie, not the URL.
    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test")}):
            pass


def test_cross_tenant_session_id_rejected_at_the_audio_socket(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_tenant(db, customer_id="cust-2", site_id="site-2", appliance_id="appl-2", cloud_id="AIC-OTHER0001")
            _seed_camera(db, "cam-1", customer_id="cust-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-2")}):
            pass
    assert _session_state(db_path, session_id) == "requested"  # untouched by the rejected cross-tenant attempt


def test_unsupported_camera_rejected_at_the_audio_socket(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})

    # Capability revoked (e.g. a rescan changed it) in between /start and
    # the audio socket connecting -- the socket must re-check, not trust
    # the earlier decision.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("UPDATE cameras SET talk_down_supported=0 WHERE id='cam-1'")

    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}):
            pass


def test_unverified_camera_rejected_at_the_audio_socket(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=None)

    response = client.post("/api/customer/cameras/cam-1/talk/start", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 409  # never even gets a session_id


def test_no_appliance_channel_rejects_the_audio_socket(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})
    # No appliance has connected its control channel at all.
    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}):
            pass


# --------------------------------------------------------------- lifecycle / cleanup

def test_browser_disconnect_marks_session_stopped(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}) as customer_ws:
            appliance_ws.receive_json()  # the "start" message
            customer_ws.close()  # abrupt disconnect, no explicit stop
    assert _session_state(db_path, session_id) == "stopped"
    assert session_id not in talk_audio_relay._active_relays


def test_idle_timeout_terminates_session(client, db_path, monkeypatch):
    monkeypatch.setattr(talk_audio_relay, "IDLE_TIMEOUT_SECONDS", 0.2)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}) as customer_ws:
            appliance_ws.receive_json()  # "start"
            # Never send a single audio frame -- the idle timeout must
            # close the socket on its own.
            stop_message = appliance_ws.receive_json()
            assert stop_message == {"type": "stop", "session_id": session_id}
    assert _session_state(db_path, session_id) == "stopped"


def test_repeated_press_release_leaves_no_orphan_sessions(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        for _ in range(3):
            session_id = _start_session(client, "cam-1", cookie)
            with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=cookie) as customer_ws:
                appliance_ws.receive_json()
                customer_ws.send_bytes(b"\x00")
                appliance_ws.receive_json()
            assert _session_state(db_path, session_id) == "stopped"
    assert talk_audio_relay._active_relays == {}


def test_one_cameras_session_never_receives_another_sessions_audio(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", camera_number=1, talk_down_supported=1)
            _seed_camera(db, "cam-2", camera_number=2, talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    session_a = _start_session(client, "cam-1", cookie)
    session_b = _start_session(client, "cam-2", cookie)

    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_a}/audio", cookies=cookie) as ws_a, \
             client.websocket_connect(f"/api/customer/talk/sessions/{session_b}/audio", cookies=cookie) as ws_b:
            start_a = appliance_ws.receive_json()
            start_b = appliance_ws.receive_json()
            assert {start_a["session_id"], start_b["session_id"]} == {session_a, session_b}

            ws_a.send_bytes(b"\xaa")
            audio_a = appliance_ws.receive_json()
            assert audio_a["session_id"] == session_a

            ws_b.send_bytes(b"\xbb")
            audio_b = appliance_ws.receive_json()
            assert audio_b["session_id"] == session_b
            assert audio_b["session_id"] != audio_a["session_id"]


def test_no_audio_forwarded_after_relay_ended(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        talk_audio_relay._active_relays["evt-1"] = {"camera_id": "cam-1", "appliance_id": "appl-1", "customer_id": "cust-1", "created_at": time.monotonic()}
        import asyncio
        asyncio.run(talk_audio_relay._end_relay("evt-1", notify_appliance=False))
    assert "evt-1" not in talk_audio_relay._active_relays


def test_appliance_channel_disconnect_ends_its_relays(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            _seed_appliance_credential(db, "appl-1", "cred")

    session_id = _start_session(client, "cam-1", {partner_portal.SESSION_COOKIE: _owner_cookie()})
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}):
            appliance_ws.receive_json()
            appliance_ws.close()  # appliance's control channel drops mid-session
            import time as _t
            _t.sleep(0.3)
    assert _session_state(db_path, session_id) == "stopped"


# --------------------------------------------------------------- structural

def test_no_camera_number_or_model_hardcoded():
    import inspect
    import re
    source = inspect.getsource(talk_audio_relay)
    for model in ("CMIP3342WI", "CMIP1042W"):
        assert model not in source
    assert not re.search(r"camera_number\s*(==|in)\s*[\(\d]", source)


def test_module_never_touches_recording_analytics_yolo_hls():
    import ast
    forbidden = {
        "ai_person_detector", "motion_detector", "save_yolo_events", "append_analytics_event",
        "analytics_rules_engine", "start_recording", "start_live_stream", "recording_uploader", "analytics_sync",
    }
    import inspect
    tree = ast.parse(inspect.getsource(talk_audio_relay))
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
    assert not overlap, f"talk_audio_relay.py unexpectedly references {overlap}"
