"""RDM Hybrid cloud-cost policy: POST /api/admin/customers/{customer_id}/cloud-policy.

The two real cost-control levers -- daily_cloud_seconds (the Hybrid
6-hour/day continuous-segment cloud-upload allowance) and retention_days
(7/14/30, the real S3 lifecycle-expiration window) -- are now directly,
administrator-only settable per customer, the same shape as the camera-
quota and cloud-recording-mode admin routes. Reuses event_media_policy.
cloud_policy_for_customer() as the one real read path both the cloud-
side event-media gate and GET /api/appliance/configuration (the edge
sync channel) consult, so a value set here becomes effective everywhere
immediately, with zero appliance-side or per-customer special-casing.

Real HTTP throughout (TestClient(main.app)).
"""

import secrets
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_rdm_cloud_policy.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(db_path, customer_id, partner_id=None):
    partner_id = partner_id or f"partner-{customer_id}"
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", f"{customer_id}@example.test", "active", "2026-01-01"),
        )
        conn.commit()


def _admin_cookie():
    import partner_portal
    return partner_portal._token("admin@example.test", "administrator")


def _viewer_cookie(customer_id):
    import partner_portal
    return partner_portal._token("viewer@example.test", "customer_viewer", None, customer_id, None)


def _session_cookie_name():
    import partner_portal
    return partner_portal.SESSION_COOKIE


def _set_policy(client, customer_id, **payload):
    return client.post(
        f"/api/admin/customers/{customer_id}/cloud-policy",
        cookies={_session_cookie_name(): _admin_cookie()},
        json=payload,
    )


def _policy_row(db_path, customer_id):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM customer_cloud_policy WHERE customer_id=?", (customer_id,)).fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------- the route itself


def test_admin_can_set_both_fields(client, db_path):
    _seed_tenant(db_path, "cust-1")
    response = _set_policy(client, "cust-1", daily_cloud_seconds=14400, retention_days=14)
    assert response.status_code == 200
    assert response.json() == {"customer_id": "cust-1", "daily_cloud_seconds": 14400, "retention_days": 14}


def test_retention_days_must_be_7_14_or_30(client, db_path):
    _seed_tenant(db_path, "cust-1")
    response = _set_policy(client, "cust-1", retention_days=15)
    assert response.status_code == 400


def test_daily_cloud_seconds_must_be_positive(client, db_path):
    _seed_tenant(db_path, "cust-1")
    response = _set_policy(client, "cust-1", daily_cloud_seconds=0)
    assert response.status_code == 400


def test_partial_update_leaves_the_other_field_unchanged(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _set_policy(client, "cust-1", daily_cloud_seconds=14400, retention_days=14)
    response = _set_policy(client, "cust-1", retention_days=30)
    assert response.status_code == 200
    assert response.json() == {"customer_id": "cust-1", "daily_cloud_seconds": 14400, "retention_days": 30}


def test_setting_retention_null_clears_back_to_the_legacy_default(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _set_policy(client, "cust-1", retention_days=30)
    response = _set_policy(client, "cust-1", retention_days=None)
    assert response.status_code == 200
    assert response.json()["retention_days"] is None


def test_unknown_customer_is_404(client, db_path):
    response = _set_policy(client, "cust-does-not-exist", daily_cloud_seconds=21600)
    assert response.status_code == 404


def test_non_admin_role_is_rejected(client, db_path):
    _seed_tenant(db_path, "cust-1")
    response = client.post(
        "/api/admin/customers/cust-1/cloud-policy",
        cookies={_session_cookie_name(): _viewer_cookie("cust-1")},
        json={"retention_days": 30},
    )
    assert response.status_code == 403


# --------------------------------------------------------------- reused everywhere: cloud_policy_for_customer()


def test_no_override_returns_no_cap_at_all(client, db_path):
    # No RDM override means no cap -- Hybrid never uses this value at
    # all (event-clips-only, no continuous-segment upload), and the
    # Continuous/Cloud tier's own product purpose is unlimited
    # continuous cloud recording by default.
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        from event_media_policy import cloud_policy_for_customer
        with connection() as db:
            policy = cloud_policy_for_customer(db, "cust-1")
    assert policy == {"daily_cloud_seconds": None, "retention_days": None}


def test_rdm_override_is_the_real_effective_value(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _set_policy(client, "cust-1", daily_cloud_seconds=3600, retention_days=7)
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        from event_media_policy import cloud_policy_for_customer
        with connection() as db:
            policy = cloud_policy_for_customer(db, "cust-1")
    assert policy == {"daily_cloud_seconds": 3600, "retention_days": 7}


# --------------------------------------------------------------- tenant isolation (two customers, simultaneously, independently)


def test_two_customers_have_fully_independent_entitlements_simultaneously(client, db_path):
    _seed_tenant(db_path, "cust-a", partner_id="partner-a")
    _seed_tenant(db_path, "cust-b", partner_id="partner-b")
    _set_policy(client, "cust-a", daily_cloud_seconds=3600, retention_days=7)
    _set_policy(client, "cust-b", daily_cloud_seconds=28800, retention_days=30)

    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        from event_media_policy import cloud_policy_for_customer
        with connection() as db:
            policy_a = cloud_policy_for_customer(db, "cust-a")
            policy_b = cloud_policy_for_customer(db, "cust-b")
    assert policy_a == {"daily_cloud_seconds": 3600, "retention_days": 7}
    assert policy_b == {"daily_cloud_seconds": 28800, "retention_days": 30}

    # Changing customer A afterward never touches customer B's own row.
    _set_policy(client, "cust-a", retention_days=14)
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        from event_media_policy import cloud_policy_for_customer
        with connection() as db:
            policy_b_again = cloud_policy_for_customer(db, "cust-b")
    assert policy_b_again == {"daily_cloud_seconds": 28800, "retention_days": 30}


# --------------------------------------------------------------- edge sync channel: GET /api/appliance/configuration


def _seed_appliance(db_path, customer_id, appliance_id, cloud_id, credential):
    from partner_db import connection, password_hash
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Site", "2026-01-01"))
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                (appliance_id, customer_id, f"site-{customer_id}", cloud_id, "2026-01-01"),
            )
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-01-01"))


def _appliance_auth_headers(appliance_id, credential):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def test_configuration_route_exposes_no_cap_with_no_override(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-POLICY1", "test-credential")
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    assert response.json()["cloud_policy"] == {"daily_cloud_seconds": None, "retention_days": None}


def test_configuration_route_reflects_an_rdm_override_immediately(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-POLICY1", "test-credential")
    _set_policy(client, "cust-1", daily_cloud_seconds=3600, retention_days=7)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["cloud_policy"] == {"daily_cloud_seconds": 3600, "retention_days": 7}


def test_two_appliances_under_different_customers_see_their_own_policy_only(client, db_path):
    _seed_tenant(db_path, "cust-a", partner_id="partner-a")
    _seed_tenant(db_path, "cust-b", partner_id="partner-b")
    _seed_appliance(db_path, "cust-a", "appl-a", "AIC-POLICYA", "cred-a")
    _seed_appliance(db_path, "cust-b", "appl-b", "AIC-POLICYB", "cred-b")
    _set_policy(client, "cust-a", daily_cloud_seconds=3600)
    _set_policy(client, "cust-b", daily_cloud_seconds=28800)

    response_a = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-a", "cred-a"))
    response_b = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-b", "cred-b"))
    assert response_a.json()["cloud_policy"]["daily_cloud_seconds"] == 3600
    assert response_b.json()["cloud_policy"]["daily_cloud_seconds"] == 28800
