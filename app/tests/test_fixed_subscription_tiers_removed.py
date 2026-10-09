"""Owner decision 2026-10-08: camera plans are per camera only. The fixed
8/16/32/64-camera Local and Hybrid subscription tiers are removed from the
catalog, the customer pages and every checkout / plan-change path. The
per-camera plans (Basic Local $9.99, AI Local $14.99, Hybrid $24.99 per
camera per month) and the separate one-time VMS software licences stay
exactly as they were, Stripe Price ID env vars included."""
import inspect
import sqlite3

import pytest

import customer_entitlements
import main
import partner_portal
import per_camera_billing
import pricing_catalog
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_fixed_tiers_removed.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed(db_path, customer_id="cust-1", legacy_product=None):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                 (customer_id, "partner-1", "Customer", f"{customer_id}@example.test", "active", "2026-01-01"))
    if legacy_product:
        # A row of the removed fixed-tier kind (as a stale/test database could
        # still hold): the pages must still never offer fixed-tier actions.
        conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
                     "VALUES('ent-1',?,?,8,'active','2026-10-01','2026-10-01')", (customer_id, legacy_product))
    conn.commit()


def _owner(customer_id="cust-1"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)}


FIXED_TIER_MARKERS = ("data-tier-label", "upgrade-to-hybrid", "buy-camera-plan", "camera-tier-select",
                      "/api/customer/camera-slots/checkout", "/api/customer/plan/")


# ------------------------------------------------------------- catalog
def test_the_catalog_has_no_fixed_subscription_tiers():
    for name in ("BASE_PLANS", "base_plans", "find_base_plan", "quote"):
        assert not hasattr(pricing_catalog, name), name
    assert "base_plans" not in pricing_catalog.public_catalog()
    source = inspect.getsource(pricing_catalog) + inspect.getsource(customer_entitlements)
    for env_var in ("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", "ANYAICAM_STRIPE_PRICE_HYBRID_33_64"):
        assert env_var not in source  # no fixed-tier Price ID is read any more
    assert not hasattr(customer_entitlements, "PLAN_TIERS")
    assert customer_entitlements.PRICE_ID_CAMERA_SLOT_MAP == {}
    assert customer_entitlements.resolve_tier("price_anything") is None  # grants nothing (fail closed)


def test_the_per_camera_plans_are_unchanged():
    prices = {key: plan["monthly_cents_per_camera"] for key, plan in per_camera_billing.PLANS.items()}
    assert prices == {"basic_local": 999, "ai_local": 1499, "hybrid": 2499}
    assert per_camera_billing.PRICE_ENV == {"basic_local": "ANYAICAM_STRIPE_PRICE_BASIC_LOCAL",
                                            "ai_local": "ANYAICAM_STRIPE_PRICE_AI_LOCAL",
                                            "hybrid": "ANYAICAM_STRIPE_PRICE_HYBRID"}
    assert (per_camera_billing.MIN_CAMERA_QUANTITY, per_camera_billing.MAX_CAMERA_QUANTITY) == (1, 64)


def test_the_one_time_vms_licences_are_unchanged():
    assert pricing_catalog.VMS_LICENSES == [
        (8, 4999, "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8"), (16, 7999, "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_16"),
        (32, 12999, "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_32"), (64, 19999, "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_64")]
    assert [lic["capacity"] for lic in pricing_catalog.public_catalog()["vms_licenses"]] == [8, 16, 32, 64]


def test_the_public_catalog_api_lists_licences_but_no_fixed_tiers(http_client):
    body = http_client.get("/api/pricing/catalog").json()
    assert "base_plans" not in body
    assert [lic["one_time"] for lic in body["vms_licenses"]] == [49.99, 79.99, 129.99, 199.99]


# ------------------------------------------------------------- checkout and plan-change paths
@pytest.mark.parametrize("path", ["/api/customer/camera-slots/checkout", "/api/customer/plan/upgrade-to-hybrid",
                                  "/api/customer/plan/downgrade-to-local", "/api/customer/plan/cancel",
                                  "/api/customer/plan/resume", "/api/customer/plan/cancel-downgrade"])
def test_fixed_tier_checkout_and_plan_change_paths_are_gone(http_client, db_path, path):
    _seed(db_path, legacy_product="camera_slots_local")
    response = http_client.post(path, json={"plan_type": "local", "tier_label": "1-8"}, cookies=_owner())
    assert response.status_code in (404, 405), (path, response.status_code)


def test_the_per_camera_checkout_and_licence_checkout_still_exist(http_client, db_path):
    _seed(db_path)
    v2 = http_client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "nope", "camera_quantity": 1}, cookies=_owner())
    assert v2.status_code not in (404, 405)
    licence = http_client.post("/api/customer/vms-license/checkout", json={"capacity": 7}, cookies=_owner())
    assert licence.status_code == 400 and "Unknown VMS license capacity" in licence.text


# ------------------------------------------------------------- customer pages
@pytest.mark.parametrize("legacy_product", [None, "camera_slots_local", "camera_slots_hybrid"])
def test_my_subscription_shows_only_the_per_camera_plan_picker(http_client, db_path, legacy_product):
    _seed(db_path, legacy_product=legacy_product)
    html = http_client.get("/subscription-portal", cookies=_owner()).text
    for marker in FIXED_TIER_MARKERS:
        assert marker not in html, marker
    for plan in ("Basic Local", "AI Local", "Hybrid"):
        assert plan in html


@pytest.mark.parametrize("legacy_product", [None, "camera_slots_local"])
def test_the_setup_review_step_offers_no_fixed_tier_purchase_or_upgrade(http_client, db_path, legacy_product):
    _seed(db_path, legacy_product=legacy_product)
    html = http_client.get("/customer/setup", cookies=_owner()).text
    assert 'id="customer-subscription-review"' in html
    for marker in FIXED_TIER_MARKERS:
        assert marker not in html, marker
    if legacy_product is None:
        assert 'href="/subscription-portal">Choose a camera plan</a>' in html
