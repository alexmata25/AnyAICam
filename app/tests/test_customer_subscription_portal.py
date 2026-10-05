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

import re
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


def _viewer_cookie(customer_id):
    return partner_portal._token("viewer@example.test", "customer_viewer", None, customer_id, None)


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
    assert "Local 8 cameras &middot; 8 licensed camera slots" in response.text


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


def test_customer_with_no_plan_gets_the_billing_v2_plan_picker(http_client, db_path):
    """2026-10-05 owner decision: new purchases are billing v2 only. A customer
    with no camera plan is offered the per-camera plans, never the legacy
    fixed-capacity chooser (legacy plan holders keep the legacy page)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "No per-camera plan selected yet" in html
    assert 'id="v2-checkout-button"' in html and "/api/v2/customer/subscription/checkout" in html
    for option in ("Basic Local · $9.99 per camera/month", "AI Local · $14.99 per camera/month", "Hybrid · $24.99 per camera/month"):
        assert option in html
    assert 'id="choose-plan-button"' not in html and "Local vs Hybrid" not in html


def test_local_vs_hybrid_comparison_is_shown_to_legacy_plan_customers(http_client, db_path):
    """Grandfathered legacy customers keep the legacy page and its comparison."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Local vs Hybrid" in html
    # 2026-09-30 pricing: Local and Hybrid are both monthly.
    assert "<strong>Local</strong> &middot; monthly subscription" in html
    assert "<strong>Hybrid</strong> &middot; monthly subscription" in html
    assert "one-time purchase" not in html


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


def test_upgrade_button_upgrades_the_existing_subscription(http_client, db_path, monkeypatch):
    """Owner, 2026-10-02: Local -> Hybrid is an upgrade of the SAME
    subscription (plan_changes.py), never a second Hybrid checkout that left
    Local billing too."""
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert "/api/customer/plan/upgrade-to-hybrid" in response.text
    assert "plan_type:'hybrid'" not in response.text


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
    assert "Complete analytics: People Counting + LPR + PPE" in html
    assert re.search(r'<span>Complete analytics: People Counting \+ LPR \+ PPE<br><span class="health-detail">\$24\.99/mo · [^<]+</span></span><span class="pill">Active</span>', html)


def test_an_inactive_but_priced_addon_shows_a_buy_button(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS", "price_test_ai_essentials")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert 'data-addon-key="ai_essentials"' in html
    assert "$7.99/mo · Adds: People Counting (advanced rules and reports)" in html


def test_face_access_offers_one_size_per_door_and_never_the_old_sku(http_client, db_path, monkeypatch):
    """2026-09-30: Face Access is sold per door in the size matching the
    customer's enrolled people (none enrolled = Small). The old
    single-price SKU is no longer sold, so it never gets a buy button."""
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_FACIAL_RECOGNITION", "price_test_facial_recognition")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert html.count("<span>Face Access / Facial Recognition — Small<br>") == 1
    assert "$39.99/mo per door" in html
    assert "Facial Recognition — Medium" not in html and "Facial Recognition — Large" not in html
    assert 'data-addon-key="facial_recognition"' not in html
    assert 'data-addon-key="face_access_' not in html  # no Face Access Price ID configured here


def test_an_unpriced_addon_shows_an_honest_coming_soon_state_not_a_buy_button(http_client, db_path):
    """2026-09-23 fix: this catalog entry (no Stripe Price ID env var
    configured in this environment yet) used to be silently omitted from
    the page entirely -- a customer had no way to know the SKU existed.
    User-authorized product decision: show it honestly instead, matching
    the codebase's own aaco_product_status() "sellable: false" precedent.
    Never a buy button (clicking it would 404/fail -- there's no price
    to check out with)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Complete analytics: People Counting + LPR + PPE" in html
    # 2026-09-25: shown as clearly unavailable (not an active-looking
    # "Coming soon"), with what it includes from the catalog mapping.
    assert re.search(r'<span>Complete analytics: People Counting \+ LPR \+ PPE<br><span class="health-detail">\$24\.99/mo · Adds: People Counting \(advanced rules and reports\), License Plate Recognition \(LPR\), PPE detection</span></span>'
                     r'<span class="pending-badge" aria-disabled="true"[^>]*>Not available yet</span>', html)
    assert 'data-addon-key="advanced_analytics"' not in html


def test_an_addon_already_active_without_its_price_id_configured_still_shows_active(http_client, db_path):
    """2026-09-23 fix, other half: an addon granted some way other than
    this Stripe Price ID (e.g. a partner-provisioned entitlement) used to
    vanish from the page entirely once its price env var was unset,
    hiding a real active feature from the customer. It must still show
    Active, never "Coming soon" and never a buy button, regardless of
    whether the price env var happens to be configured."""
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
    assert re.search(r'<span>Complete analytics: People Counting \+ LPR \+ PPE<br><span class="health-detail">\$24\.99/mo · [^<]+</span></span><span class="pill">Active</span>', html)


# ------------------------------------------------------- customer_viewer role


def test_viewer_sees_the_new_page_with_real_data_but_no_purchase_actions(http_client, db_path, monkeypatch):
    """create_camera_slot_checkout()/create_analytics_addon_checkout()
    are both customer_owner-only (403 for any other role) -- a viewer
    must never be shown a button that can only ever 403 when clicked.
    The underlying data (plan badge, add-on active/inactive state) is
    identical to what an owner sees; only the actionable buttons differ."""
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS", "price_test_ai_essentials")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("cust-1")})
    assert response.status_code == 200
    html = response.text
    assert "My subscription" in html
    assert '<span class="pill">Local</span>' in html
    assert 'id="upgrade-to-hybrid"' not in html
    assert 'id="subscription-upgrade-button"' not in html
    assert 'data-addon-key="ai_essentials"' not in html
    assert 'id="friends-family-panel"' not in html
    assert "Not purchased" in html


def test_owner_still_sees_purchase_actions_unaffected_by_the_viewer_fix(http_client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert 'id="subscription-upgrade-button"' in response.text


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
