"""Real-customer /subscription-portal branch (2026-09-21): the existing
"My subscription" nav item (main.py NAV_ITEMS, unchanged) pointed at a
page keyed entirely by current_user() -- the legacy local-VMS auth,
never partner_identity() -- so a real customer_owner/customer_viewer
session (the partner-portal cookie every other customer-facing page
already checks) fell through to that page's anonymous/viewer stub and
saw a different, older subscription concept (Starter/Professional/
Enterprise license tiers via billing_account_for_user()/license_
enforcement_snapshot()) with no relationship to their real Local/Hybrid
camera-slot entitlement or analytics add-ons.

_customer_subscription_portal_page() is the real-data branch this file
tests: current plan badge, a Local/Hybrid comparison, an entitlement/
billing-driven Upgrade-to-Hybrid action (reusing the existing camera-
slot checkout endpoint, same as the customer setup page's own proven
panel), and real analytics add-ons (analytics_entitlements.
ANALYTICS_CATALOG / get_active_analytics_for_customer()). A staff/
legacy session must keep seeing the exact original page, untouched.

Reuses test_dashboard_camera_tenant_scoping.py's own established harness.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_subscription_portal.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator")


# ---------------------------------------------------------- current plan


def test_local_customer_sees_the_local_plan_badge(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    assert "My subscription" in response.text
    assert '<span class="pill">Local</span>' in response.text
    assert "Local 1-8" in response.text


def test_hybrid_customer_sees_the_hybrid_plan_badge_and_no_upgrade_panel(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert '<span class="pill">Hybrid</span>' in response.text
    assert 'id="upgrade-to-hybrid"' not in response.text


def test_customer_with_no_plan_sees_no_active_plan(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert '<span class="pill">No active plan</span>' in response.text


def test_local_vs_hybrid_comparison_is_always_shown(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Local vs Hybrid" in html
    assert "one-time purchase" in html
    assert "recurring subscription" in html


# --------------------------------------------------------- upgrade to hybrid


def test_upgrade_panel_shown_for_a_local_customer_with_a_priced_hybrid_tier(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert 'id="upgrade-to-hybrid"' in html
    assert 'id="subscription-upgrade-button"' in html
    assert 'data-tier-label="1-8"' in html


def test_upgrade_panel_hidden_when_no_matching_hybrid_price_is_configured(http_client, db_path):
    """Fail-closed, same discipline as the setup page's own camera_tier_
    options -- never offer an upgrade this endpoint would then reject
    with PRICE_ID_REQUIRED."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert 'id="upgrade-to-hybrid"' not in response.text


def test_upgrade_button_calls_the_existing_camera_slot_checkout_endpoint(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert "/api/customer/camera-slots/checkout" in response.text
    assert "plan_type:'hybrid'" in response.text


# ------------------------------------------------------------------ add-ons


def test_an_active_addon_shows_an_active_pill_not_a_buy_button(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_test_advanced_analytics")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from analytics_entitlements import upsert_analytics_subscription
        for key in ("smart_motion", "people_counting", "lpr", "ppe"):
            upsert_analytics_subscription(customer_id="cust-1", analytic_key=key, status="active")
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Advanced Analytics" in html
    assert '<span>Advanced Analytics</span><span class="pill">Active</span>' in html


def test_an_inactive_but_priced_addon_shows_a_buy_button(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_FACIAL_RECOGNITION", "price_test_facial_recognition")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Face Access" in html
    assert 'data-addon-key="facial_recognition"' in html


def test_an_unpriced_addon_is_not_offered_at_all(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert "No analytics add-ons are configured for purchase yet." in response.text


# ------------------------------------------------------- legacy path untouched


def test_staff_session_still_sees_the_original_legacy_page(http_client, db_path):
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()})
    assert response.status_code == 200
    assert 'id="subscription-summary"' in response.text
    assert 'id="subscription-upgrade-button"' not in response.text


def test_no_session_at_all_is_unaffected_pre_existing_redirect(http_client, db_path):
    """Pre-existing, unrelated to this branch: a completely anonymous
    request never reaches subscription_portal_page()'s body at all (a
    login-required middleware redirect) -- this proves the new
    partner_identity() check added at the top of that function doesn't
    change that gate, not that the legacy body itself renders."""
    response = http_client.get("/subscription-portal")
    assert response.status_code == 303
