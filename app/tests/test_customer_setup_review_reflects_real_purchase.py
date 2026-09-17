"""Regression coverage for a confirmed-live bug: the customer setup
wizard's Step 6 ("Review subscription") showed a legacy, partner-quoted
per-camera/resolution/retention estimate (plans.*, e.g. "4MP · motion ·
7 days · 1 camera / Estimated monthly subscription: $8.59") instead of
what the customer actually bought through Stripe TEST checkout (an
AnyAiCam Starter appliance, a Local 1-8 camera-slot subscription with 8
licensed slots, and Smart Motion/People Counting/LPR analytics). The
plans table predates the Local/Hybrid camera-slot architecture, is
populated once at onboarding from whatever pricing_selection the
onboarding operator happened to submit (often a throwaway placeholder,
as it was for this exact customer), and is never touched by a real
Stripe purchase -- see customer_entitlements.py's own module docstring,
audit finding #2.

Also covers a second, related bug on Step 5: a camera row with no
device_key (a purchased-slot placeholder created at onboarding time,
never a physically discovered device) was rendered identically to a
real, provisioned camera's status text, making an offline/uninstalled
appliance look like it had already discovered a camera.

Fix: Step 6 now shows LICENSING/BILLING (hardware, camera-slot tier +
licensed capacity, analytics) from the same Stripe-verified sources the
rest of the app already uses for billing, with no dollar estimate of
its own -- Stripe is the only source of what the customer is actually
charged. plans.* is still shown, but relabeled as local RECORDING
CONFIGURATION (resolution/mode/retention-days), never as a subscription
charge; recording_retention_sweep.py's own use of plans.retention_days
for real retention enforcement is untouched. Step 5 now labels a
device_key-less row as a licensed slot that hasn't been discovered yet.
"""

import sqlite3

import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_setup_review_reflects_real_purchase.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id="cust-1", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Real Customer", "customer@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Main Site", "2026-01-01"))
    conn.commit()


def _seed_legacy_plan(conn, customer_id="cust-1", resolution="4mp", recording_mode="motion", retention_days=7, camera_quantity=1, retail_monthly=8.59):
    """The exact onboarding-placeholder shape this bug report was filed
    against."""
    conn.execute(
        "INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"plan-{customer_id}", customer_id, resolution, recording_mode, retention_days, camera_quantity, retail_monthly, "quote", "2026-01-01"),
    )
    conn.commit()


def _seed_camera_slot_entitlement(conn, customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8, session_id="cs_test_local"):
    conn.execute(
        "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_checkout_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (f"ent-{customer_id}-{product}", customer_id, product, camera_slot_quantity, "active", session_id, "2026-01-01", "2026-01-01"),
    )
    conn.commit()


def _seed_hardware_order(conn, customer_id="cust-1", sku="AIC-APPLIANCE-RYZEN-STARTER", product_name="AnyAiCam Starter", status="paid"):
    conn.execute(
        "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,currency,status,fulfillment_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"hw-{customer_id}", customer_id, sku, product_name, "price_test_starter", 1, 124999, "usd", status, status, "2026-01-01", "2026-01-01"),
    )
    conn.commit()


def _seed_analytics(conn, customer_id="cust-1", keys=()):
    for key in keys:
        conn.execute(
            "INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) VALUES(?,?,NULL,?,?,?)",
            (f"an-{customer_id}-{key}", customer_id, key, "active", "2026-01-01"),
        )
    conn.commit()


def _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-TEST0001", activation_status="linked"):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,software_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", cloud_id, activation_status, "offline", "Not installed", "2026-01-01"),
    )
    conn.commit()


def _seed_placeholder_camera(conn, camera_id, *, customer_id="cust-1", name="Camera 1"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,device_key,camera_number,created_at) VALUES(?,?,?,?,?,NULL,NULL,?)",
        (camera_id, customer_id, "site-1", name, "pending_installation", "2026-01-01"),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _setup_page(http_client, customer_id="cust-1"):
    return http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie(customer_id)})


# --------------------------------------------------------- Step 6: review


def test_review_shows_the_purchased_hardware(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_legacy_plan(conn)
    _seed_hardware_order(conn)
    response = _setup_page(http_client)
    assert response.status_code == 200
    assert "AnyAiCam Starter" in response.text


def test_review_shows_the_purchased_local_tier_and_licensed_slot_count(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_legacy_plan(conn, camera_quantity=1)  # deliberately mismatched from the real entitlement
    _seed_camera_slot_entitlement(conn, product="camera_slots_local", camera_slot_quantity=8)
    response = _setup_page(http_client)
    assert "8 licensed camera slots" in response.text
    assert "Local" in response.text


def test_review_shows_the_purchased_hybrid_tier_when_thats_what_was_bought(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_camera_slot_entitlement(conn, product="camera_slots_hybrid", camera_slot_quantity=16)
    response = _setup_page(http_client)
    assert "Hybrid" in response.text
    assert "16 licensed camera slots" in response.text


def test_review_shows_every_purchased_analytic_and_no_others(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_analytics(conn, keys=["smart_motion", "people_counting", "lpr"])
    response = _setup_page(http_client)
    # 2026-09-16: scoped past the sidebar <nav>, not the whole page body.
    # AAC added a "Facial Recognition" nav link gated by role/permission
    # (partner_db.ROLE_PERMISSIONS's facial.view), the same way every
    # other nav item on this legacy nav system works -- it is not, and
    # was never, gated by per-customer analytics purchase the way this
    # review step's own purchased-analytics summary is. A customer_owner
    # always sees that nav link regardless of what they purchased, so a
    # whole-body substring check coincidentally collided with it purely
    # because its label text matches the analytic's own display name.
    body = response.text.split("</nav>", 1)[-1]
    assert "Smart Motion" in body
    assert "People Counting" in body
    assert "License Plate Recognition" in body
    # Unpurchased analytics must not appear as if they were enabled.
    assert "Talk Down" not in body
    assert "PPE Detection" not in body
    assert "Facial Recognition" not in body


def test_review_never_shows_the_legacy_dollar_estimate(http_client, db_path):
    """The exact bug: $8.59/month shown as though it were a real,
    separately-purchased per-camera charge on top of the Local 1-8
    subscription the customer actually bought."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_legacy_plan(conn, retail_monthly=8.59)
    _seed_camera_slot_entitlement(conn)
    response = _setup_page(http_client)
    assert "$8.59" not in response.text
    assert "Estimated monthly subscription" not in response.text


def test_review_labels_local_recording_as_configuration_not_billing(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_legacy_plan(conn, resolution="4mp", recording_mode="motion", retention_days=7)
    response = _setup_page(http_client)
    body = response.text
    assert "Local recording configuration" in body
    assert "not a separate charge" in body
    assert "4MP" in body and "7" in body  # the underlying config is still shown, just not billed


def test_review_reports_no_purchases_yet_without_manufacturing_defaults(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    response = _setup_page(http_client)
    body = response.text
    assert "No hardware purchased yet" in body
    assert "No camera-slot plan purchased yet" in body
    assert "None purchased" in body


# ------------------------------------------------ Step 5: placeholder camera


def test_placeholder_camera_is_labeled_not_a_discovered_device(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    _seed_placeholder_camera(conn, "ph-1", name="Camera 1")
    response = _setup_page(http_client)
    body = response.text
    assert "Licensed slot" in body
    assert "not yet discovered" in body


def test_offline_uninstalled_appliance_with_only_placeholders_does_not_look_discovered(http_client, db_path):
    """The Samsung in this exact scenario: software Not installed,
    offline, never checked in. Its customer still has one onboarding
    placeholder camera. The page must not present that placeholder the
    same way a real, discovered device would be."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn, activation_status="linked")
    _seed_placeholder_camera(conn, "ph-1", name="Camera 1")
    response = _setup_page(http_client)
    body = response.text
    assert "pending_installation" not in body  # the raw status string is no longer shown for a placeholder
    assert "Licensed slot" in body


# ------------------------------------ retention configuration never touches billing


def test_expected_camera_count_endpoint_is_unaffected_by_local_retention_days(http_client, db_path):
    """Local retention configuration (resolution/mode/retention_days) and
    the Stripe entitlement that drives licensed camera-slot capacity are
    two independent facts -- changing/seeding one must never change the
    other's own API surface."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_legacy_plan(conn, retention_days=30, retail_monthly=49.99)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    response = http_client.get("/api/customer/cameras", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.json()["expected_camera_count"] == 8
