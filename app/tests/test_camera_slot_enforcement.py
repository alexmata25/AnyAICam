"""Provisioning Phase 7: camera-slot enforcement. Previous audits this
session found that the cloud correctly TRACKS a customer's purchased
camera-slot entitlement (customer_entitlements.total_camera_slots()) but
nothing at either camera-provisioning entry point actually ENFORCED it --
a customer could add unlimited cameras regardless of what they purchased.
This file proves both gates now enforce the real, current entitlement:

  1. POST /api/customer/cameras/provision (partner_workspace.py) -- the
     request-time gate a customer's own browser hits.
  2. POST /api/appliance/{cloud_id}/provisioning-jobs/{job_id}
     (appliance_cloud.py) -- the confirmation-time gate the appliance
     itself hits once a camera is actually verified reachable; a second,
     independent check since a request queued while under the limit
     could still be confirmed after other requests already consumed it.

Both real HTTP routes, real signed appliance auth, real customer_owner
session cookies -- not direct function calls.
"""
import json
import secrets as secrets_module
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target

TIER_1_8 = "price_slot_enforcement_1_8"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_slot_enforcement.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        import customer_entitlements as ce

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")
        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
            TIER_1_8: {"product": "camera_slots_local", "camera_slot_maximum": 8},
        })

        import appliance_activation
        monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
        import appliance_cloud
        appliance_cloud.activation_limiter.events.clear()

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(db_path, customer_id="cust-1", email="slot-test@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _grant_slots(db_path, customer_id, quantity, *, product="camera_slots_local"):
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        ce.upsert_entitlement(customer_id=customer_id, product=product, camera_slot_quantity=quantity, status="active")


def _owner_cookie(customer_id="cust-1", email="slot-test@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _session_cookie_name():
    import partner_portal
    return partner_portal.SESSION_COOKIE


def _seed_and_activate_appliance(db_path, *, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-SLOT1"):
    from partner_db import connection, password_hash
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Site", "2026-01-01"))
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,activation_status,created_at) VALUES(?,?,?,?,?,?,?)",
                (appliance_id, customer_id, "site-1", cloud_id, "windows_byo_pc", "pending", "2026-01-01"),
            )
            token = secrets_module.token_urlsafe(24)
            db.execute(
                "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (secrets_module.token_hex(4), appliance_id, password_hash(token), "2027-01-01T00:00:00", "2026-01-01"),
            )
    return token


def _appliance_headers(appliance_id, credential, nonce=None):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": nonce or secrets_module.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _provision_request(test_client, *, appliance_id, device_key, name="Camera"):
    cookie = _owner_cookie()
    test_client.cookies.set(_session_cookie_name(), cookie)
    response = test_client.post(
        "/api/customer/cameras/provision",
        json={"appliance_id": appliance_id, "device_key": device_key, "name": name},
    )
    test_client.cookies.clear()
    return response


# ============================================================ the request gate


def test_cameras_1_through_8_allowed_camera_9_rejected_at_request_gate(client, db_path):
    _seed_tenant(db_path)
    _grant_slots(db_path, "cust-1", 8)
    token = _seed_and_activate_appliance(db_path)
    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    assert activate.status_code == 200
    credential = activate.json()["credential"]

    for i in range(1, 9):
        response = _provision_request(client, appliance_id="appl-1", device_key=f"dev-{i}", name=f"Camera {i}")
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        confirm = client.post(
            f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}",
            json={"success": True, "message": "Verified."},
            headers=_appliance_headers("appl-1", credential),
        )
        assert confirm.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        configured = conn.execute("SELECT COUNT(*) FROM cameras WHERE customer_id='cust-1' AND device_key IS NOT NULL").fetchone()[0]
    assert configured == 8

    ninth = _provision_request(client, appliance_id="appl-1", device_key="dev-9", name="Camera 9")
    assert ninth.status_code == 403
    assert "licensed for 8 camera" in ninth.json()["detail"]

    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        still_configured = conn.execute("SELECT COUNT(*) FROM cameras WHERE customer_id='cust-1' AND device_key IS NOT NULL").fetchone()[0]
    assert still_configured == 8  # the 9th camera was never even queued


def test_zero_active_slots_blocks_the_first_camera(client, db_path):
    _seed_tenant(db_path)
    # No entitlement granted at all -- total_camera_slots() is 0.
    token = _seed_and_activate_appliance(db_path)
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})

    response = _provision_request(client, appliance_id="appl-1", device_key="dev-1")
    assert response.status_code == 403
    assert "licensed for 0 camera" in response.json()["detail"]


def test_reconnecting_an_existing_camera_is_never_blocked_by_the_slot_limit(client, db_path):
    _seed_tenant(db_path)
    _grant_slots(db_path, "cust-1", 1)
    token = _seed_and_activate_appliance(db_path)
    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    credential = activate.json()["credential"]

    first = _provision_request(client, appliance_id="appl-1", device_key="dev-stable")
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    confirm = client.post(
        f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}",
        json={"success": True, "message": "Verified."},
        headers=_appliance_headers("appl-1", credential),
    )
    assert confirm.status_code == 200

    # Same device_key again (e.g. a rescan, or moving it to a different
    # site on the same appliance) -- must succeed even though the
    # account's single slot is already "used" by this exact camera.
    reconnect = _provision_request(client, appliance_id="appl-1", device_key="dev-stable", name="Renamed Camera")
    assert reconnect.status_code == 200


# ============================================================ the appliance-side gate


def test_appliance_side_gate_also_rejects_a_raced_past_the_limit_confirmation(client, db_path):
    """Simulates the race the request-time gate alone can't fully close:
    a job queued directly (bypassing the browser-facing request route,
    the way a second simultaneous request that both passed the first
    gate's read before either one's write would) must still be rejected
    at confirmation time rather than silently exceeding the limit."""
    _seed_tenant(db_path)
    _grant_slots(db_path, "cust-1", 1)
    token = _seed_and_activate_appliance(db_path)
    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    credential = activate.json()["credential"]

    # Consume the account's only slot through the normal path first.
    first = _provision_request(client, appliance_id="appl-1", device_key="dev-a")
    job_id_1 = first.json()["job_id"]
    client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id_1}", json={"success": True}, headers=_appliance_headers("appl-1", credential))

    # Directly insert a second queued job for a DIFFERENT device_key,
    # bypassing request_camera_provisioning()'s own gate entirely (as a
    # true race between two simultaneous requests would have).
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,recording_mode,analytics_json,status,message,created_at,updated_at) "
                "VALUES('job-raced','cust-1','appl-1','site-1','dev-b','Camera B','motion','[]','queued','Queued.','2026-01-01','2026-01-01')"
            )

    confirm = client.post(
        "/api/appliance/AIC-SLOT1/provisioning-jobs/job-raced",
        json={"success": True, "message": "Verified."},
        headers=_appliance_headers("appl-1", credential, nonce="raced-nonce-padded-to-16chars"),
    )
    assert confirm.status_code == 200  # the route itself still succeeds (result accepted)...
    body = confirm.json()
    assert body["message"] == "Provisioning result saved."

    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        camera_b = conn.execute("SELECT id FROM cameras WHERE customer_id='cust-1' AND device_key='dev-b'").fetchone()
        job_status = conn.execute("SELECT status,message FROM camera_provisioning_requests WHERE id='job-raced'").fetchone()
    assert camera_b is None  # ...but no camera row was created for it
    assert job_status[0] == "failed"
    assert "licensed for 1 camera" in job_status[1]


# ============================================================ cancellation preserves cameras


def test_cancellation_zeroes_slots_but_never_deletes_already_provisioned_cameras(client, db_path):
    _seed_tenant(db_path)
    _grant_slots(db_path, "cust-1", 2)
    token = _seed_and_activate_appliance(db_path)
    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    credential = activate.json()["credential"]

    first = _provision_request(client, appliance_id="appl-1", device_key="dev-keep")
    job_id = first.json()["job_id"]
    client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}", json={"success": True}, headers=_appliance_headers("appl-1", credential))

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        ce.upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=0, status="cancelled")
        total = ce.total_camera_slots("cust-1")
        conn = sqlite3.connect(db_path)
        camera_still_there = conn.execute("SELECT id,status FROM cameras WHERE customer_id='cust-1' AND device_key='dev-keep'").fetchone()
    assert total == 0
    assert camera_still_there is not None
    assert camera_still_there[1] == "configured"  # untouched, not deleted or disabled by the cancellation itself

    # And a NEW camera is now correctly blocked at 0 active slots.
    blocked = _provision_request(client, appliance_id="appl-1", device_key="dev-new-after-cancel")
    assert blocked.status_code == 403
