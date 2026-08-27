"""Regression coverage for "Add New Customer" in both the Partner Portal
and the Administrator Portal sharing one backend/customer-creation
workflow (partner_workspace.py's onboard_customer()/render_partner_
workspace()/customer_detail()), with role-based scoping:

  - Administrator = global scope (every partner's customers).
  - Partner (partner_owner/salesperson) = own customers only, including
    the customer *detail* page, which had no ownership check at all
    before this change -- any partner could open any other partner's
    customer record just by knowing/guessing its id.
  - Technician = permission-based limited access (partner_db.
    ROLE_PERMISSIONS already excludes customer.create for technician;
    this only confirms it holds at the route).
  - Customer = no Add New Customer access at all.
  - An Admin Portal identity with no direct Partner Portal session can
    reuse the exact same workflow via the existing admin_partner_links
    bridge (task 4) -- no second implementation, no second manual login.

Same pattern and fixtures as test_admin_partner_bridge_integration.py:
TestClient(main.app) with a real, signed session cookie for the legacy
side and partner_portal._token() for the direct partner side.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
import partner_workspace
from database_backend import override_target
from partner_db import initialize_database


class _StubRequest:
    headers: dict = {}
    client = None


def _stub_request():
    return _StubRequest()


def _seed_partner(conn, partner_id):
    # approval_status='approved' -- authenticate_detailed() joins to this
    # row and requires it for administrator/partner_owner/salesperson/
    # technician roles (partner_db.py line ~167); left unset it defaults
    # to NULL -> 'pending', which rejects every fresh login attempt
    # regardless of password, before the account even exists in a real
    # deployment's eyes.
    conn.execute(
        "INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES(?,?,?,?)",
        (partner_id, f"Partner {partner_id}", "approved", "2026-01-01"),
    )
    conn.commit()


def _seed_customer(conn, customer_id, partner_id, name="Existing Customer"):
    # company/trial_status left as '' rather than NULL -- real onboarding
    # (onboard_customer()) always writes a string there via payload.get(...,''),
    # so render_partner_workspace()'s escape(customer.get('company','')) never
    # sees a real NULL; matching that shape here instead of exercising an
    # unreachable-in-practice code path.
    _seed_partner(conn, partner_id)
    conn.execute(
        "INSERT INTO customers(id,partner_id,name,company,email,status,trial_status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (customer_id, partner_id, name, "", f"{customer_id}@example.com", "active", "eligible", "2026-01-01"),
    )
    conn.commit()


def _seed_partner_user(conn, user_id, email, role, partner_id="partner-1"):
    _seed_partner(conn, partner_id)
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "User", role, "x", 1, "2026-01-01"),
    )
    conn.commit()


def _seed_link(conn, admin_user_id, admin_email, partner_user_id, partner_email, now="2026-08-27T00:00:00"):
    conn.execute(
        "INSERT INTO admin_partner_links(admin_user_id,admin_email,partner_user_id,partner_email,linked_at,linked_by,revoked_at) VALUES(?,?,?,?,?,?,NULL)",
        (admin_user_id, admin_email, partner_user_id, partner_email, now, admin_email),
    )
    conn.commit()


def _onboard_payload(email="new@example.com", name="New Customer", **extra):
    payload = {
        "name": name, "company": "Acme", "email": email, "phone": "555-0100",
        "status": "trial", "sites": [{"name": "HQ"}],
        "appliance_type": "AnyAiCam mini PC", "deployment_mode": "local",
        "pricing": {"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 4, "addons": []},
    }
    payload.update(extra)
    return payload


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    # calculate_partner_quote()'s default pricing config (no real
    # pricing_config.json present) uses pricing_mode='fixed' with every
    # partner_monthly_price left None -- unrelated to this feature, but
    # onboard_customer() always calls through it, so tests that create a
    # real customer need a pricing mode that doesn't require pre-filled
    # wholesale prices.
    import pricing_config
    percentage_config = pricing_config.load_pricing()
    percentage_config["partner"]["pricing_mode"] = "percentage"
    percentage_config["partner"]["percentage_discount"] = 20
    monkeypatch.setattr(pricing_config, "load_pricing", lambda: percentage_config)
    db_path = tmp_path / "test_customers.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client, db_path


def _admin_session_cookie(client_or_none=None, admin_id="admin-1", role="administrator", email="admin@example.test"):
    main.save_users([{"id": admin_id, "email": email, "role": role, "enabled": True, "camera_ids": []}])
    return main.create_session(admin_id)


# =============================================================== partner-role scoping (list + detail)


def test_partner_owner_sees_only_own_customers(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, "cust-mine", "partner-1", "Mine")
    _seed_customer(conn, "cust-theirs", "partner-2", "Theirs")
    token = partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)

    response = client.get("/partner", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 200
    assert "Mine" in response.text
    assert "Theirs" not in response.text


def test_partner_cannot_view_another_partners_customer_detail(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, "cust-theirs", "partner-2", "Theirs")
    token = partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)

    response = client.get("/partner/customers/cust-theirs", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 404  # never confirms/denies existence to an unauthorized caller


def test_partner_can_view_own_customer_detail(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, "cust-mine", "partner-1", "Mine")
    token = partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)

    response = client.get("/partner/customers/cust-mine", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 200
    assert "Mine" in response.text


def test_partner_owner_can_create_a_customer_scoped_to_their_own_partner_id(http_client):
    client, db_path = http_client
    _seed_partner(sqlite3.connect(db_path), "partner-1")
    token = partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)

    response = client.post(
        "/api/partner/customers/onboard", json=_onboard_payload(),
        cookies={partner_portal.SESSION_COOKIE: token},
    )
    assert response.status_code == 200
    row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE email='new@example.com'").fetchone()
    assert row[0] == "partner-1"  # not an arbitrary/attacker-supplied partner_id


def test_partner_owner_cannot_steer_partner_id_via_payload(http_client):
    """Only an administrator identity may target a different partner_id
    -- see the comment in onboard_customer(). A non-admin sending
    partner_id in the payload must be silently ignored, not honored."""
    client, db_path = http_client
    _seed_partner(sqlite3.connect(db_path), "partner-1")
    token = partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)

    response = client.post(
        "/api/partner/customers/onboard", json=_onboard_payload(partner_id="partner-2"),
        cookies={partner_portal.SESSION_COOKIE: token},
    )
    assert response.status_code == 200
    row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE email='new@example.com'").fetchone()
    assert row[0] == "partner-1"


# =============================================================== administrator = global scope


def test_administrator_sees_customers_across_every_partner(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, "cust-a", "partner-1", "CustomerA")
    _seed_customer(conn, "cust-b", "partner-2", "CustomerB")
    token = partner_portal._token("admin@example.test", "administrator", "partner-1", None, None)

    response = client.get("/partner", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 200
    assert "CustomerA" in response.text
    assert "CustomerB" in response.text


def test_administrator_can_view_any_partners_customer_detail(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, "cust-b", "partner-2", "CustomerB")
    token = partner_portal._token("admin@example.test", "administrator", "partner-1", None, None)

    response = client.get("/partner/customers/cust-b", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 200
    assert "CustomerB" in response.text


def test_administrator_can_create_a_customer_for_an_explicit_partner_id(http_client):
    client, db_path = http_client
    _seed_partner(sqlite3.connect(db_path), "partner-9")
    token = partner_portal._token("admin@example.test", "administrator", "anyaicam-primary", None, None)

    response = client.post(
        "/api/partner/customers/onboard", json=_onboard_payload(partner_id="partner-9"),
        cookies={partner_portal.SESSION_COOKIE: token},
    )
    assert response.status_code == 200
    row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE email='new@example.com'").fetchone()
    assert row[0] == "partner-9"


def test_admin_customers_page_lists_customers_across_every_partner(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, "cust-a", "partner-1", "CustomerA")
    _seed_customer(conn, "cust-b", "partner-2", "CustomerB")
    token = _admin_session_cookie()

    response = client.get("/admin-customers", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200
    assert "CustomerA" in response.text
    assert "CustomerB" in response.text
    assert 'href="/partner/onboarding"' in response.text  # Add New Customer opens the shared wizard, not a second implementation


# =============================================================== technician / customer denial


def test_technician_cannot_create_a_customer(http_client):
    client, db_path = http_client
    token = partner_portal._token("tech@example.test", "technician", "partner-1", None, None)

    response = client.post(
        "/api/partner/customers/onboard", json=_onboard_payload(),
        cookies={partner_portal.SESSION_COOKIE: token},
    )
    assert response.status_code == 403


def test_technician_onboarding_page_redirects_home_not_to_the_wizard(http_client):
    client, db_path = http_client
    token = partner_portal._token("tech@example.test", "technician", "partner-1", None, None)

    response = client.get("/partner/onboarding", cookies={partner_portal.SESSION_COOKIE: token}, follow_redirects=False)
    assert response.status_code == 403


def test_customer_owner_cannot_create_a_customer(http_client):
    client, db_path = http_client
    token = partner_portal._token("cust@example.test", "customer_owner", None, "cust-1", None)

    response = client.post(
        "/api/partner/customers/onboard", json=_onboard_payload(),
        cookies={partner_portal.SESSION_COOKIE: token},
    )
    assert response.status_code == 403


def test_customer_owner_onboarding_page_is_redirected_away(http_client):
    client, db_path = http_client
    token = partner_portal._token("cust@example.test", "customer_owner", None, "cust-1", None)

    response = client.get("/partner/onboarding", cookies={partner_portal.SESSION_COOKIE: token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/partner-login"


def test_anonymous_cannot_create_a_customer(http_client):
    client, db_path = http_client
    response = client.post("/api/partner/customers/onboard", json=_onboard_payload())
    assert response.status_code in (401, 403)


# =============================================================== legacy Admin Portal identity via the bridge, not a second implementation


def test_legacy_admin_without_a_bridge_link_is_redirected_not_silently_failed(http_client):
    client, db_path = http_client
    token = _admin_session_cookie()

    response = client.get("/partner/onboarding", cookies={main.SESSION_COOKIE_NAME: token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/partner-login"


def test_legacy_admin_with_a_live_bridge_link_reuses_the_same_onboarding_workflow(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_partner_user(conn, "pu-1", "bridged@example.test", "administrator", partner_id="partner-7")
    _seed_link(conn, "admin-1", "admin@example.test", "pu-1", "bridged@example.test")
    token = _admin_session_cookie()

    response = client.post(
        "/api/partner/customers/onboard", json=_onboard_payload(),
        cookies={main.SESSION_COOKIE_NAME: token},  # no Partner Portal cookie at all
    )
    assert response.status_code == 200
    row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE email='new@example.com'").fetchone()
    assert row[0] == "partner-7"  # created under the bridged partner identity's own partner_id (no override sent)


# =============================================================== POST /api/portal-login -- same email, both identities, selected portal wins


def test_same_email_administrator_and_partner_owner_selecting_administrator_reaches_admin_portal(http_client):
    client, db_path = http_client
    email = "dual@example.test"
    main.save_users([{"id": "dual-1", "email": email, "role": "administrator", "enabled": True,
                       "password_hash": main.hash_password("Sup3rSecret!"), "camera_ids": []}])
    conn = sqlite3.connect(db_path)
    _seed_partner_user(conn, "pu-dual", email, "partner_owner", partner_id="partner-3")
    conn.execute("UPDATE partner_users SET password_hash=? WHERE id='pu-dual'", (partner_portal.password_hash("Sup3rSecret!"),))
    conn.commit()

    response = client.post(
        "/api/portal-login", json={"email": email, "password": "Sup3rSecret!", "portal": "administrator"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin-portal"
    assert main.SESSION_COOKIE_NAME in response.cookies
    assert partner_portal.SESSION_COOKIE not in response.cookies


def test_same_email_administrator_and_partner_owner_selecting_partner_reaches_partner_portal(http_client):
    client, db_path = http_client
    email = "dual2@example.test"
    main.save_users([{"id": "dual-2", "email": email, "role": "administrator", "enabled": True,
                       "password_hash": main.hash_password("Sup3rSecret!"), "camera_ids": []}])
    conn = sqlite3.connect(db_path)
    _seed_partner_user(conn, "pu-dual2", email, "partner_owner", partner_id="partner-3")
    conn.execute("UPDATE partner_users SET password_hash=? WHERE id='pu-dual2'", (partner_portal.password_hash("Sup3rSecret!"),))
    conn.commit()

    response = client.post(
        "/api/portal-login", json={"email": email, "password": "Sup3rSecret!", "portal": "partner"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/partner?tab=customers"
    assert partner_portal.SESSION_COOKIE in response.cookies
    assert main.SESSION_COOKIE_NAME not in response.cookies


def test_same_email_without_a_selection_is_rejected_not_defaulted_to_partner(http_client):
    client, db_path = http_client
    email = "dual3@example.test"
    main.save_users([{"id": "dual-3", "email": email, "role": "administrator", "enabled": True,
                       "password_hash": main.hash_password("Sup3rSecret!"), "camera_ids": []}])
    conn = sqlite3.connect(db_path)
    _seed_partner_user(conn, "pu-dual3", email, "partner_owner", partner_id="partner-3")
    conn.execute("UPDATE partner_users SET password_hash=? WHERE id='pu-dual3'", (partner_portal.password_hash("Sup3rSecret!"),))
    conn.commit()

    response = client.post("/api/portal-login", json={"email": email, "password": "Sup3rSecret!"})
    assert response.status_code == 403
    assert "portal" in response.json()["detail"].lower()
