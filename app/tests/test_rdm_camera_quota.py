"""RDM Camera Quota: POST /api/admin/customers/{customer_id}/camera-quota.

Closes the gap customer_entitlements.py's own module docstring already
documented: total_camera_slots() -- the one function both real
provisioning-enforcement gates (see test_camera_slot_enforcement.py) call
-- could previously only ever be changed by a real Stripe webhook event.
The pre-existing partner-facing "Update camera entitlement" form (PUT
/api/partner/customers/{id}/plan) writes plans.camera_quantity, a
completely different column total_camera_slots() has never read -- a
partner/RDM admin using that form changes what the customer PORTAL
displays, not what is actually enforced.

This route is the real, administrator-only, audited way for RDM to set a
customer's enforced camera count directly, via the same upsert_entitlement()
Stripe events already use -- not a second, disconnected entitlement
concept.

Real HTTP throughout (TestClient(main.app)): the admin route itself, and
-- in the end-to-end section -- the real customer-facing provisioning
request/confirm routes from test_camera_slot_enforcement.py's own
established pattern, proving the quota this route sets is the exact
value those gates enforce.
"""

import secrets as secrets_module
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_rdm_camera_quota.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")

        import appliance_activation
        monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
        import appliance_cloud
        appliance_cloud.activation_limiter.events.clear()

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(db_path, customer_id="cust-1", email="quota-test@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _admin_cookie():
    import partner_portal
    return partner_portal._token("admin@example.test", "administrator")


def _viewer_cookie(customer_id="cust-1"):
    import partner_portal
    return partner_portal._token("viewer@example.test", "customer_viewer", None, customer_id, None)


def _owner_cookie(customer_id="cust-1", email="quota-test@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _session_cookie_name():
    import partner_portal
    return partner_portal.SESSION_COOKIE


def _entitlement_rows(db_path, customer_id="cust-1", product="camera_slots_rdm"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer_id, product)
        ).fetchall()]


def _set_quota(client, customer_id, quota):
    return client.post(
        f"/api/admin/customers/{customer_id}/camera-quota",
        cookies={_session_cookie_name(): _admin_cookie()},
        json={"camera_quota": quota},
    )


# --------------------------------------------------------------- the route itself


def test_admin_can_set_a_quota(client, db_path):
    _seed_tenant(db_path)
    response = _set_quota(client, "cust-1", 5)
    assert response.status_code == 200
    assert response.json() == {"customer_id": "cust-1", "camera_quota": 5, "total_camera_slots": 5}


def test_setting_the_quota_actually_updates_total_camera_slots(client, db_path):
    _seed_tenant(db_path)
    _set_quota(client, "cust-1", 5)
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import total_camera_slots
        assert total_camera_slots("cust-1") == 5


def test_re_setting_the_quota_updates_in_place_never_accumulates_rows(client, db_path):
    _seed_tenant(db_path)
    _set_quota(client, "cust-1", 5)
    _set_quota(client, "cust-1", 8)
    _set_quota(client, "cust-1", 5)
    rows = _entitlement_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["camera_slot_quantity"] == 5


def test_zero_is_an_explicit_valid_quota_not_a_no_op(client, db_path):
    _seed_tenant(db_path)
    _set_quota(client, "cust-1", 8)
    response = _set_quota(client, "cust-1", 0)
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import total_camera_slots
        assert total_camera_slots("cust-1") == 0


def test_negative_quota_is_rejected(client, db_path):
    _seed_tenant(db_path)
    response = _set_quota(client, "cust-1", -1)
    assert response.status_code == 400


def test_non_integer_quota_is_rejected(client, db_path):
    _seed_tenant(db_path)
    response = _set_quota(client, "cust-1", 5.5)
    assert response.status_code == 400


def test_unknown_customer_is_404(client, db_path):
    _seed_tenant(db_path)
    response = _set_quota(client, "cust-does-not-exist", 5)
    assert response.status_code == 404


def test_non_admin_partner_role_is_rejected(client, db_path):
    _seed_tenant(db_path)
    response = client.post(
        "/api/admin/customers/cust-1/camera-quota",
        cookies={_session_cookie_name(): _viewer_cookie()},
        json={"camera_quota": 5},
    )
    assert response.status_code == 403


def test_a_stripe_granted_entitlement_and_an_rdm_quota_are_independent_products(client, db_path):
    """RDM's own product key (camera_slots_rdm) must never collide with
    or silently overwrite a real Stripe-purchased entitlement
    (camera_slots_local/camera_slots_hybrid) for the same customer --
    each product key is one real, distinct grant, summed together."""
    _seed_tenant(db_path)
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement, total_camera_slots
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=3, status="active")
    _set_quota(client, "cust-1", 5)
    with override_target(sqlite_path=str(db_path)):
        assert total_camera_slots("cust-1") == 8  # 3 (Stripe) + 5 (RDM), both real, both summed


# --------------------------------------------------------------- end-to-end: RDM quota -> real provisioning enforcement


def _appliance_headers(appliance_id, credential, nonce=None):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": nonce or secrets_module.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed_and_activate_appliance(db_path, *, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-QUOTA1"):
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


def _provision(client, *, appliance_id, device_key, name="Camera"):
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    response = client.post(
        "/api/customer/cameras/provision",
        json={"appliance_id": appliance_id, "device_key": device_key, "name": name},
    )
    client.cookies.clear()
    return response


def _provision_and_confirm(client, *, appliance_id, cloud_id, credential, device_key, name="Camera"):
    response = _provision(client, appliance_id=appliance_id, device_key=device_key, name=name)
    if response.status_code != 200:
        return response
    job_id = response.json()["job_id"]
    confirm = client.post(
        f"/api/appliance/{cloud_id}/provisioning-jobs/{job_id}",
        json={"success": True, "message": "Verified."},
        headers=_appliance_headers(appliance_id, credential),
    )
    assert confirm.status_code == 200, confirm.text
    return response


def _configured_count(db_path, customer_id="cust-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        return conn.execute(
            "SELECT COUNT(*) FROM cameras WHERE customer_id=? AND device_key IS NOT NULL", (customer_id,)
        ).fetchone()[0]


def test_full_quota_lifecycle_matches_the_exact_specified_sequence(client, db_path):
    """5/5 -> reject camera 6; remove one -> 4/5 -> permit one replacement;
    RDM raises quota 5->8 -> permit cameras 6-8; 8/8 -> reject camera 9.
    Uses the real admin route to set the quota at each step -- never a
    direct database write -- and the real customer-facing provisioning
    routes to prove enforcement, exactly matching the specified test
    sequence."""
    _seed_tenant(db_path)
    token = _seed_and_activate_appliance(db_path)
    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-QUOTA1", "activation_token": token})
    assert activate.status_code == 200
    credential = activate.json()["credential"]

    # RDM sets the initial quota to 5.
    assert _set_quota(client, "cust-1", 5).status_code == 200

    for i in range(1, 6):
        result = _provision_and_confirm(client, appliance_id="appl-1", cloud_id="AIC-QUOTA1", credential=credential, device_key=f"dev-{i}", name=f"Camera {i}")
        assert result.status_code == 200, result.text
    assert _configured_count(db_path) == 5

    # 5/5 -> camera 6 rejected.
    sixth = _provision(client, appliance_id="appl-1", device_key="dev-6")
    assert sixth.status_code == 403
    assert "licensed for 5 camera" in sixth.json()["detail"]

    # Remove camera dev-1 (frees a slot without deleting the row -- see
    # test_customer_camera_removal.py for the removal route itself; here
    # a direct, minimal DB update to device_key=NULL stands in for it,
    # since this file's own scope is quota enforcement, not the removal
    # route's own contract).
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE cameras SET device_key=NULL WHERE customer_id='cust-1' AND device_key='dev-1'")
        conn.commit()
    assert _configured_count(db_path) == 4

    # 4/5 -> exactly one replacement permitted.
    replacement = _provision_and_confirm(client, appliance_id="appl-1", cloud_id="AIC-QUOTA1", credential=credential, device_key="dev-1-replacement")
    assert replacement.status_code == 200, replacement.text
    assert _configured_count(db_path) == 5

    # 5/5 again -> the next one still rejected.
    still_full = _provision(client, appliance_id="appl-1", device_key="dev-7")
    assert still_full.status_code == 403

    # RDM raises the quota 5 -> 8.
    assert _set_quota(client, "cust-1", 8).status_code == 200

    for i in range(6, 9):
        result = _provision_and_confirm(client, appliance_id="appl-1", cloud_id="AIC-QUOTA1", credential=credential, device_key=f"dev-{i}", name=f"Camera {i}")
        assert result.status_code == 200, result.text
    assert _configured_count(db_path) == 8

    # 8/8 -> camera 9 rejected.
    ninth = _provision(client, appliance_id="appl-1", device_key="dev-9")
    assert ninth.status_code == 403
    assert "licensed for 8 camera" in ninth.json()["detail"]
    assert _configured_count(db_path) == 8


def test_quota_decrease_never_deletes_or_disables_already_provisioned_cameras(client, db_path):
    """Explicit product-policy requirement: lowering RDM's quota below
    a customer's current active-camera count must never arbitrarily
    delete/disable existing cameras -- it only blocks NEW additions
    until the customer (or RDM) brings the count back within the new,
    lower quota."""
    _seed_tenant(db_path)
    token = _seed_and_activate_appliance(db_path)
    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-QUOTA1", "activation_token": token})
    credential = activate.json()["credential"]

    _set_quota(client, "cust-1", 8)
    for i in range(1, 9):
        result = _provision_and_confirm(client, appliance_id="appl-1", cloud_id="AIC-QUOTA1", credential=credential, device_key=f"dev-{i}")
        assert result.status_code == 200, result.text
    assert _configured_count(db_path) == 8

    # RDM lowers the quota 8 -> 5, below the current active count.
    response = _set_quota(client, "cust-1", 5)
    assert response.status_code == 200

    # All 8 existing cameras are completely untouched -- still configured,
    # still with their device_key, nothing deleted or disabled.
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cameras = conn.execute("SELECT device_key FROM cameras WHERE customer_id='cust-1'").fetchall()
    assert {c["device_key"] for c in cameras} == {f"dev-{i}" for i in range(1, 9)}
    assert _configured_count(db_path) == 8

    # But no NEW camera can be added while over the new, lower quota.
    ninth = _provision(client, appliance_id="appl-1", device_key="dev-9")
    assert ninth.status_code == 403
    assert "licensed for 5 camera" in ninth.json()["detail"]
