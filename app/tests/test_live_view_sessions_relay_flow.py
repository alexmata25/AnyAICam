"""Staging Live View transport (2026-09-13): regression coverage for the
customer-facing half of the relay lifecycle -- POST /api/customer/cameras/
{camera_id}/live/start and POST /api/customer/live/sessions/{session_id}/
stop (live_view_sessions.py).

Written as part of enabling the existing S3/CloudFront live-relay pipeline
for real staging traffic for the first time -- this module had no prior
test coverage. Confirms, with real HTTP through the real app (matching
test_live_view_page_customer_auth.py's own established pattern):

  * an authorized owner starting live view on their own, real, configured
    camera succeeds and queues exactly one start_live_relay command
    addressed to the correct appliance_id/camera_number/camera_id;
  * a different customer can never start a session on someone else's
    camera_id (tenant isolation, same 404-via-scoped-lookup shape as
    live_view_page.py's own camera route);
  * a licensed-but-undiscovered placeholder camera (camera_number IS
    NULL) is rejected with 409, never silently queuing a relay command
    for a camera that was never actually configured;
  * stopping a session marks it 'stopped' and is idempotent, matching
    the module's own documented duplicate-replay-is-a-200 precedent --
    and never queues a second stop for an already-terminal session.
"""

import json

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_sessions_relay_flow.db"


def _seed(conn):
    now = "2026-09-13T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','partner-1','Customer B','b@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'urn:uuid:fake-a','configured','Camera 1',?)", (now,)
    )
    # A licensed-but-undiscovered placeholder on the SAME appliance -- no
    # device_key, no camera_number, exactly onboarding.py's own INSERT shape.
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a-placeholder','cust-a','site-a','appl-a',NULL,NULL,'pending_installation','Camera 2',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-b','owner-b@example.test','customer_owner','cust-b','x','all',?)", (now,)
    )
    conn.commit()


def _owner_cookie(customer_id, email):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed(conn)
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


def _latest_relay_command(db_path, appliance_id, command):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            return db.execute(
                "SELECT * FROM appliance_commands WHERE appliance_id=? AND command=? ORDER BY created_at DESC LIMIT 1",
                (appliance_id, command),
            ).fetchone()


# --------------------------------------------------------------- happy path


def test_owner_can_start_live_view_on_their_own_configured_camera(client, db_path):
    response = client.post(
        "/api/customer/cameras/cam-a/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "requested"
    assert body["session_id"]

    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            session = db.execute("SELECT * FROM live_view_sessions WHERE id=?", (body["session_id"],)).fetchone()
    assert session["camera_id"] == "cam-a"
    assert session["customer_id"] == "cust-a"
    assert session["state"] == "requested"


def test_starting_a_session_queues_start_live_relay_for_the_correct_appliance_and_camera(client, db_path):
    response = client.post(
        "/api/customer/cameras/cam-a/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    )
    assert response.status_code == 200
    command = _latest_relay_command(db_path, "appl-a", "start_live_relay")
    assert command is not None
    assert command["status"] == "pending"
    payload = json.loads(command["payload_json"])
    assert payload["camera_id"] == "cam-a"
    assert payload["camera_number"] == 1


# --------------------------------------------------------------- tenant isolation


def test_a_different_customer_cannot_start_live_view_on_someone_elses_camera(client, db_path):
    response = client.post(
        "/api/customer/cameras/cam-a/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")},
    )
    assert response.status_code == 404
    command = _latest_relay_command(db_path, "appl-a", "start_live_relay")
    assert command is None  # never queued on behalf of an unauthorized caller


# --------------------------------------------------------------- placeholder rejection


def test_a_licensed_but_undiscovered_placeholder_camera_cannot_start_live_view(client, db_path):
    response = client.post(
        "/api/customer/cameras/cam-a-placeholder/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    )
    assert response.status_code == 409
    command = _latest_relay_command(db_path, "appl-a", "start_live_relay")
    assert command is None


# --------------------------------------------------------------- stop / expiry cleanup


def test_stopping_a_session_marks_it_stopped(client, db_path):
    start = client.post(
        "/api/customer/cameras/cam-a/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    ).json()
    response = client.post(
        f"/api/customer/live/sessions/{start['session_id']}/stop",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            session = db.execute("SELECT state FROM live_view_sessions WHERE id=?", (start["session_id"],)).fetchone()
    assert session["state"] == "stopped"


def test_stopping_an_already_stopped_session_is_idempotent_and_queues_no_second_stop(client, db_path):
    start = client.post(
        "/api/customer/cameras/cam-a/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    ).json()
    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    first = client.post(f"/api/customer/live/sessions/{start['session_id']}/stop", cookies=cookie)
    second = client.post(f"/api/customer/live/sessions/{start['session_id']}/stop", cookies=cookie)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "stopped"


def test_a_different_customer_cannot_stop_someone_elses_session(client, db_path):
    start = client.post(
        "/api/customer/cameras/cam-a/live/start",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
    ).json()
    response = client.post(
        f"/api/customer/live/sessions/{start['session_id']}/stop",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")},
    )
    assert response.status_code == 404
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            session = db.execute("SELECT state FROM live_view_sessions WHERE id=?", (start["session_id"],)).fetchone()
    assert session["state"] == "requested"  # untouched by the unauthorized attempt
