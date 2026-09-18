"""P2P live-view foundation (2026-09-16): regression coverage for
live_view_p2p.py -- the SDP offer/answer + ICE candidate signaling
exchange between a customer's browser and the appliance that owns the
camera, plus the transport-outcome instrumentation (p2p vs. relay,
connect time, failure) this whole feature exists to enable.

This module never touches a media byte -- see its own module docstring.
These tests prove the signaling plumbing (auth, tenant isolation,
idempotent delivery, instrumentation) is correct; they do not and cannot
prove real WebRTC connectivity, which requires an actual appliance-side
publisher (not yet built -- see live_view_p2p.py's module docstring).

Customer-side routes are tested through the real app (main.app), matching
test_live_view_sessions_relay_flow.py's established pattern, since
partner_identity()'s cookie auth is easiest exercised end-to-end.
Appliance-side routes are tested through a minimal, isolated FastAPI app
(register_live_view_p2p_appliance_routes only), matching
test_live_relay_session_endpoint.py's established pattern for the sibling
relay-session endpoints.
"""

import time
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_p2p_signaling.db"


def _seed(conn):
    now = "2026-09-16T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','partner-1','Customer B','b@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-a','appl-a',?,?)",
                 (__import__("partner_db").password_hash("cred"), now))
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'urn:uuid:fake-a','configured','Camera 1',?)", (now,)
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


@pytest.fixture()
def appliance_client(db_path):
    with override_target(sqlite_path=str(db_path)):
        import live_view_p2p
        app = FastAPI()
        live_view_p2p.register_live_view_p2p_appliance_routes(app)
        with TestClient(app) as test_client:
            yield test_client


def _start_session(client, db_path, *, appliance_id="appl-a", camera_id="cam-a"):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.post(f"/api/customer/cameras/{camera_id}/live/start", cookies=cookies)
    assert response.status_code == 200
    return response.json()["session_id"], cookies


def _appliance_headers(appliance_id, credential):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


# ------------------------------------------------------------- config route


def test_p2p_config_reflects_the_feature_flag(client, monkeypatch):
    import live_view_p2p
    monkeypatch.setattr(live_view_p2p, "LIVE_P2P_ENABLED", True)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get("/api/customer/live/p2p/config", cookies=cookies)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert isinstance(body["ice_servers"], list) and body["ice_servers"]
    assert all("stun:" in entry["urls"] or entry["urls"] == "" for entry in body["ice_servers"] if isinstance(entry["urls"], str))


def test_p2p_config_requires_customer_auth(client):
    # No cookie at all is caught by main.app's own auth middleware before
    # ever reaching this route's _customer_identity() check (which is what
    # produces the 403 in test_offer_rejects_a_session_belonging_to_another_
    # customer above, where a cookie IS present but for the wrong tenant).
    response = client.get("/api/customer/live/p2p/config")
    assert response.status_code == 401


# --------------------------------------------------------- offer / ice / answer


def test_offer_is_accepted_and_marks_session_p2p_attempted(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(f"/api/customer/live/sessions/{session_id}/p2p/offer", cookies=cookies, json={"sdp": "v=0\r\n..."})
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            row = db.execute("SELECT p2p_attempted FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["p2p_attempted"] == 1


def test_offer_rejects_a_session_belonging_to_another_customer(client, db_path):
    session_id, _ = _start_session(client, db_path)
    other_cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.post(f"/api/customer/live/sessions/{session_id}/p2p/offer", cookies=other_cookies, json={"sdp": "v=0\r\n..."})
    assert response.status_code == 404


def test_offer_requires_nonempty_sdp(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(f"/api/customer/live/sessions/{session_id}/p2p/offer", cookies=cookies, json={"sdp": ""})
    assert response.status_code == 400


def test_client_ice_candidate_is_accepted(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/p2p/ice", cookies=cookies,
        json={"candidate": {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host", "sdpMid": "0", "sdpMLineIndex": 0}},
    )
    assert response.status_code == 200


def test_answer_poll_is_empty_until_appliance_responds(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.get(f"/api/customer/live/sessions/{session_id}/p2p/answer", cookies=cookies)
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] is None
    assert body["candidates"] == []


# --------------------------------------------------------- full round trip


def test_full_offer_to_answer_round_trip_between_browser_and_appliance(client, appliance_client, db_path):
    session_id, cookies = _start_session(client, db_path)

    offer = client.post(f"/api/customer/live/sessions/{session_id}/p2p/offer", cookies=cookies, json={"sdp": "offer-sdp"})
    assert offer.status_code == 200
    client.post(
        f"/api/customer/live/sessions/{session_id}/p2p/ice", cookies=cookies,
        json={"candidate": {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host", "sdpMid": "0", "sdpMLineIndex": 0}},
    )

    pending = appliance_client.get("/api/appliance/live/p2p/pending", headers=_appliance_headers("appl-a", "cred"))
    assert pending.status_code == 200
    kinds = {item["kind"] for item in pending.json()["pending"]}
    assert kinds == {"offer", "ice_client"}
    assert all(item["session_id"] == session_id and item["camera_id"] == "cam-a" for item in pending.json()["pending"])

    # A second poll must not re-deliver the same messages (consumed_at).
    pending_again = appliance_client.get("/api/appliance/live/p2p/pending", headers=_appliance_headers("appl-a", "cred"))
    assert pending_again.json()["pending"] == []

    answer = appliance_client.post(
        f"/api/appliance/live/cam-a/p2p/answer", headers=_appliance_headers("appl-a", "cred"),
        json={"session_id": session_id, "sdp": "answer-sdp"},
    )
    assert answer.status_code == 200
    appliance_client.post(
        f"/api/appliance/live/cam-a/p2p/ice", headers=_appliance_headers("appl-a", "cred"),
        json={"session_id": session_id, "candidate": {"candidate": "candidate:2 1 UDP 1 203.0.113.9 6000 typ srflx", "sdpMid": "0", "sdpMLineIndex": 0}},
    )

    browser_poll = client.get(f"/api/customer/live/sessions/{session_id}/p2p/answer", cookies=cookies)
    assert browser_poll.status_code == 200
    body = browser_poll.json()
    assert body["answer"] == {"sdp": "answer-sdp"}
    assert len(body["candidates"]) == 1

    # Idempotent: a second poll must not redeliver the already-consumed answer.
    second_poll = client.get(f"/api/customer/live/sessions/{session_id}/p2p/answer", cookies=cookies)
    assert second_poll.json() == {"answer": None, "candidates": []}


def test_appliance_cannot_answer_for_a_camera_it_does_not_own(appliance_client, db_path, client):
    session_id, cookies = _start_session(client, db_path)
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection, password_hash
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-other','cust-a','site-a','AIC-OTHER','2026-09-16T00:00:00')")
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-other','appl-other',?,?)",
                       (password_hash("other-cred"), "2026-09-16T00:00:00"))
    response = appliance_client.post(
        "/api/appliance/live/cam-a/p2p/answer", headers=_appliance_headers("appl-other", "other-cred"),
        json={"session_id": session_id, "sdp": "answer-sdp"},
    )
    assert response.status_code == 403


def test_appliance_pending_poll_never_returns_another_appliances_signaling(appliance_client, client, db_path):
    """Tenant/fleet isolation for the poll itself: appl-a must never see
    an offer addressed to a camera owned by a different appliance."""
    session_id, cookies = _start_session(client, db_path)
    client.post(f"/api/customer/live/sessions/{session_id}/p2p/offer", cookies=cookies, json={"sdp": "offer-sdp"})

    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection, password_hash
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-other','cust-a','site-a','AIC-OTHER','2026-09-16T00:00:00')")
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-other','appl-other',?,?)",
                       (password_hash("other-cred"), "2026-09-16T00:00:00"))

    pending = appliance_client.get("/api/appliance/live/p2p/pending", headers=_appliance_headers("appl-other", "other-cred"))
    assert pending.json()["pending"] == []


# --------------------------------------------------------- transport outcome


def test_transport_outcome_p2p_sets_transport_and_ready_at(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/transport-outcome", cookies=cookies,
        json={"transport": "p2p", "connect_ms": 850},
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            row = db.execute("SELECT transport,ready_at,failed_at FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["transport"] == "p2p"
    assert row["ready_at"] is not None
    assert row["failed_at"] is None


def test_transport_outcome_wireguard_sets_transport_and_ready_at(client, db_path):
    """docs/wireguard-remote-connectivity-plan.md Sec 17: a fourth
    transport value, same shape as p2p/relay -- no live caller reports
    this yet (Phase B has no portal wiring), but the route must already
    accept it correctly for when Phase C adds one."""
    session_id, cookies = _start_session(client, db_path)
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/transport-outcome", cookies=cookies,
        json={"transport": "wireguard", "connect_ms": 300},
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            row = db.execute("SELECT transport,ready_at,failed_at FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["transport"] == "wireguard"
    assert row["ready_at"] is not None
    assert row["failed_at"] is None


def test_transport_outcome_relay_sets_transport_and_ready_at(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/transport-outcome", cookies=cookies,
        json={"transport": "relay", "connect_ms": 6200},
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            row = db.execute("SELECT transport,ready_at FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["transport"] == "relay"
    assert row["ready_at"] is not None


def test_transport_outcome_failed_sets_failed_at_and_error_not_transport(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/transport-outcome", cookies=cookies,
        json={"transport": "failed", "error": "ice_failed and relay 503"},
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            row = db.execute("SELECT transport,failed_at,error FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["transport"] == "not_configured"
    assert row["failed_at"] is not None
    assert row["error"] == "ice_failed and relay 503"


def test_transport_outcome_rejects_unknown_transport_value(client, db_path):
    session_id, cookies = _start_session(client, db_path)
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/transport-outcome", cookies=cookies,
        json={"transport": "carrier-pigeon"},
    )
    assert response.status_code == 400


def test_transport_outcome_rejects_another_customers_session(client, db_path):
    session_id, _ = _start_session(client, db_path)
    other_cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.post(
        f"/api/customer/live/sessions/{session_id}/transport-outcome", cookies=other_cookies,
        json={"transport": "p2p"},
    )
    assert response.status_code == 404
