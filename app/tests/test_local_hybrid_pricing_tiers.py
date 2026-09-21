"""Provisioning Phase 4: PLAN_TIERS -- the verified Local/Hybrid camera-
slot pricing structure from AnyAiCam_AWS_Cost_Model_2026-09.xlsx (read
directly from the workbook's "Local"/"Hybrid" sheets, not retyped from a
description). Proves:

- the exact 8 verified prices are present and correctly shaped for both
  entitlement resolution and the website pricing table,
- Local and Hybrid are genuinely separate entitlement lines (own
  `product` key) even at camera_slot_maximum values they share,
- every tier's Stripe Price ID is unset by default (none is proven to
  exist -- see customer_entitlements.py's module docstring), and setting
  one via its own env var is what's required before that tier can ever
  grant a real entitlement,
- an env-configured Local Price ID and Hybrid Price ID resolve
  independently and update independent customer_entitlements rows.
"""
import os
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import customer_entitlements as ce


EXPECTED = {
    ("local", "1-8"): (8, 14.99),
    ("local", "9-16"): (16, 19.99),
    ("local", "17-32"): (32, 29.99),
    ("local", "33-64"): (64, 49.99),
    ("hybrid", "1-8"): (8, 29.99),
    ("hybrid", "9-16"): (16, 49.99),
    ("hybrid", "17-32"): (32, 89.99),
    ("hybrid", "33-64"): (64, 149.99),
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_local_hybrid_pricing_tiers.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        yield


def _seed_customer(db_path, customer_id="cust-1", email="real-customer@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


# ------------------------------------------------------------ structure


def test_all_eight_verified_tiers_are_present_with_the_exact_workbook_prices():
    rows = ce._plan_tier_rows()
    assert len(rows) == 8
    seen = {(r["plan_type"], r["tier_label"]): (r["camera_slot_maximum"], r["monthly_retail_usd"]) for r in rows}
    assert seen == EXPECTED


def test_pricing_table_for_website_omits_stripe_internals():
    table = ce.pricing_table_for_website()
    assert len(table) == 8
    for row in table:
        assert set(row.keys()) == {"plan_type", "tier_label", "min_cameras", "max_cameras", "monthly_retail_usd", "billing_type"}
    local_1_8 = next(r for r in table if r["plan_type"] == "local" and r["tier_label"] == "1-8")
    assert local_1_8["monthly_retail_usd"] == 14.99
    hybrid_33_64 = next(r for r in table if r["plan_type"] == "hybrid" and r["tier_label"] == "33-64")
    assert hybrid_33_64["monthly_retail_usd"] == 149.99


def test_local_is_one_time_and_hybrid_is_recurring():
    """Business decision confirmed 2026-09-21: Local became a one-time
    purchase, Hybrid stayed a recurring subscription. billing_type is
    what create_camera_slot_checkout() uses to pick Stripe Checkout
    mode=payment vs mode=subscription -- this locks the decision in so
    it can't silently drift back."""
    for row in ce._plan_tier_rows():
        if row["plan_type"] == "local":
            assert row["billing_type"] == "one_time"
        elif row["plan_type"] == "hybrid":
            assert row["billing_type"] == "recurring"


def test_local_and_hybrid_have_distinct_product_keys_even_at_the_same_slot_maximum():
    rows = ce._plan_tier_rows()
    local_64 = next(r for r in rows if r["plan_type"] == "local" and r["camera_slot_maximum"] == 64)
    hybrid_64 = next(r for r in rows if r["plan_type"] == "hybrid" and r["camera_slot_maximum"] == 64)
    assert local_64["product"] != hybrid_64["product"]
    assert local_64["product"] == "camera_slots_local"
    assert hybrid_64["product"] == "camera_slots_hybrid"


def test_no_tier_has_a_stripe_price_id_configured_by_default():
    """The core audit finding: none of these 8 prices has a proven
    Stripe Price ID anywhere in existing configuration -- every tier
    must ship with stripe_price_id unset until real configuration
    exists, never a fabricated placeholder."""
    for row in ce._plan_tier_rows():
        assert row["stripe_price_id"] is None
    assert ce.PRICE_ID_CAMERA_SLOT_MAP == {}


def test_setting_a_tiers_price_id_env_var_makes_it_resolvable(monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", "price_real_local_1_8")
    mapping = ce._load_price_tier_map()
    assert mapping == {"price_real_local_1_8": {"product": "camera_slots_local", "camera_slot_maximum": 8, "billing_type": "one_time"}}


# --------------------------------------------- Local vs Hybrid entitlements


TIER_LOCAL_1_8 = "price_local_1_8"
TIER_LOCAL_9_16 = "price_local_9_16"
TIER_HYBRID_1_8 = "price_hybrid_1_8"


@pytest.fixture()
def _real_tier_map(monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
        TIER_LOCAL_1_8: {"product": "camera_slots_local", "camera_slot_maximum": 8},
        TIER_LOCAL_9_16: {"product": "camera_slots_local", "camera_slot_maximum": 16},
        TIER_HYBRID_1_8: {"product": "camera_slots_hybrid", "camera_slot_maximum": 8},
    })


def _checkout_event(event_id, price_id, *, email="real-customer@example.test", stripe_customer="cus_1", customer_id=None):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {"id": f"cs_{event_id}", "customer": stripe_customer, "customer_details": {"email": email}, "metadata": metadata}},
    }


def test_buying_local_and_hybrid_creates_two_independent_entitlements(db_path, _real_tier_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_local", TIER_LOCAL_1_8, stripe_customer="cus_local", customer_id="cust-1"))
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_hybrid", TIER_HYBRID_1_8, stripe_customer="cus_hybrid", customer_id="cust-1"))
        entitlements = {e["product"]: e["camera_slot_quantity"] for e in ce.get_entitlements_for_customer("cust-1")}
        total = ce.total_camera_slots("cust-1")
    assert entitlements == {"camera_slots_local": 8, "camera_slots_hybrid": 8}
    assert total == 16  # both count toward total camera-slot capacity


def test_upgrading_local_never_touches_an_existing_hybrid_entitlement(db_path, _real_tier_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_hybrid", TIER_HYBRID_1_8, stripe_customer="cus_hybrid", customer_id="cust-1"))
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_local_1", TIER_LOCAL_1_8, stripe_customer="cus_local", customer_id="cust-1"))

        upgrade_event = {
            "id": "evt_local_upgrade", "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_local", "customer": "cus_local", "status": "active", "items": {"data": [{"price": {"id": TIER_LOCAL_9_16}}]}, "metadata": {}}},
        }
        ce.sync_entitlement_from_stripe_event(upgrade_event)

        entitlements = {e["product"]: e["camera_slot_quantity"] for e in ce.get_entitlements_for_customer("cust-1")}
    assert entitlements == {"camera_slots_local": 16, "camera_slots_hybrid": 8}  # hybrid untouched


def test_cancelling_hybrid_leaves_local_active(db_path, _real_tier_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_local", TIER_LOCAL_1_8, stripe_customer="cus_local", customer_id="cust-1"))
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_hybrid", TIER_HYBRID_1_8, stripe_customer="cus_hybrid", customer_id="cust-1"))

        cancel_event = {
            "id": "evt_cancel_hybrid", "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_hybrid", "customer": "cus_hybrid", "status": "canceled", "items": {"data": [{"price": {"id": TIER_HYBRID_1_8}}]}, "metadata": {}}},
        }
        ce.sync_entitlement_from_stripe_event(cancel_event)

        entitlements = {e["product"]: (e["camera_slot_quantity"], e["status"]) for e in ce.get_entitlements_for_customer("cust-1")}
    assert entitlements["camera_slots_hybrid"] == (0, "cancelled")
    assert entitlements["camera_slots_local"] == (8, "active")


# --------------------------------------- verified production Stripe Price IDs
#
# The actual 8 LIVE-mode Price IDs from the production Stripe account,
# confirmed via the Stripe Dashboard (Sandbox/Test toggle off, /products)
# -- not secrets, safe to keep in source (same treatment as the
# pre-existing hardware Price IDs already in stripe-config.php). These
# live in ecs-task-definition.production.json's environment block, not
# yet deployed. This test proves PLAN_TIERS resolves each one to the
# exact right product/camera-slot-maximum when set as env vars -- it
# never calls Stripe itself.
#
# STALE AS OF 2026-09-21: Local became a one-time purchase (billing_type
# "one_time" -> Stripe Checkout mode="payment"), but these four LOCAL
# Price IDs were created as RECURRING-monthly Price objects under the
# old model. A Stripe Price's recurring/one-time nature is fixed
# permanently at creation and cannot be edited, so these four cannot be
# reused -- mode="payment" against a recurring Price ID is rejected by
# Stripe. They are kept here only as a historical/disjointness record
# (see test_live_price_ids_never_collide_with_the_staging_test_ids());
# do not set any of the four ANYAICAM_STRIPE_PRICE_LOCAL_* env vars to
# these values. The four HYBRID Price IDs are unaffected and remain
# valid for mode="subscription".

LIVE_PRICE_IDS = {
    "ANYAICAM_STRIPE_PRICE_LOCAL_1_8": "price_1UDee3GllhK80H2nK4uQLBKe",
    "ANYAICAM_STRIPE_PRICE_LOCAL_9_16": "price_1UDeehGllhK80H2nf61pHif8",
    "ANYAICAM_STRIPE_PRICE_LOCAL_17_32": "price_1UDeeqGllhK80H2nyZ9Mz8BE",
    "ANYAICAM_STRIPE_PRICE_LOCAL_33_64": "price_1UDeeQGllhK80H2nVRRrsksb",
    "ANYAICAM_STRIPE_PRICE_HYBRID_1_8": "price_1UDecwGllhK80H2nEDSd6dww",
    "ANYAICAM_STRIPE_PRICE_HYBRID_9_16": "price_1UDef0GllhK80H2n5NcWtqDK",
    "ANYAICAM_STRIPE_PRICE_HYBRID_17_32": "price_1UDef2GllhK80H2nbIf7ZTHY",
    "ANYAICAM_STRIPE_PRICE_HYBRID_33_64": "price_1UDef5GllhK80H2nNqYcCbMs",
}

EXPECTED_FOR_LIVE_ID = {
    "price_1UDee3GllhK80H2nK4uQLBKe": ("camera_slots_local", 8, "one_time"),
    "price_1UDeehGllhK80H2nf61pHif8": ("camera_slots_local", 16, "one_time"),
    "price_1UDeeqGllhK80H2nyZ9Mz8BE": ("camera_slots_local", 32, "one_time"),
    "price_1UDeeQGllhK80H2nVRRrsksb": ("camera_slots_local", 64, "one_time"),
    "price_1UDecwGllhK80H2nEDSd6dww": ("camera_slots_hybrid", 8, "recurring"),
    "price_1UDef0GllhK80H2n5NcWtqDK": ("camera_slots_hybrid", 16, "recurring"),
    "price_1UDef2GllhK80H2nbIf7ZTHY": ("camera_slots_hybrid", 32, "recurring"),
    "price_1UDef5GllhK80H2nNqYcCbMs": ("camera_slots_hybrid", 64, "recurring"),
}


def test_all_eight_live_price_ids_are_distinct():
    assert len(set(LIVE_PRICE_IDS.values())) == 8


def test_all_eight_live_price_ids_resolve_to_the_correct_tier(monkeypatch):
    for env_var, price_id in LIVE_PRICE_IDS.items():
        monkeypatch.setenv(env_var, price_id)
    mapping = ce._load_price_tier_map()
    assert len(mapping) == 8
    # PRICE_ID_CAMERA_SLOT_MAP is computed once at process/import time in
    # production (matching how ECS actually injects env vars before the
    # app starts) -- monkeypatch.setenv() alone doesn't retroactively
    # change the already-imported module global, so re-point it at the
    # freshly loaded mapping here to exercise resolve_tier()'s real
    # lookup logic against these exact values, the same way it would run
    # against a freshly started production process.
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", mapping)
    for price_id, (expected_product, expected_max, expected_billing_type) in EXPECTED_FOR_LIVE_ID.items():
        tier = ce.resolve_tier(price_id)
        assert tier == {"product": expected_product, "camera_slot_maximum": expected_max, "billing_type": expected_billing_type}


def test_live_price_ids_never_collide_with_the_staging_test_ids():
    """Sanity guard: the live and test-mode Price ID sets must be
    disjoint -- if they ever collided, a sandbox purchase could
    accidentally resolve against a production tier or vice versa."""
    test_ids = {
        "price_1UD2xKGllhK80H2nFJwtFJvw", "price_1UD2yLGllhK80H2n7Z2q8AM3",
        "price_1UD2z4GllhK80H2ncjHZVhms", "price_1UD30bGllhK80H2nfmvPZnSA",
        "price_1UD31AGllhK80H2nMKtYEmVw", "price_1UD31mGllhK80H2nJqorGzU6",
        "price_1UD32kGllhK80H2nGaOQB9XI", "price_1UD33SGllhK80H2nxrbBT2ch",
    }
    assert test_ids.isdisjoint(LIVE_PRICE_IDS.values())


# --------------------------------- Phase 4: the $1 live plumbing-test product


def test_the_dollar_test_products_price_id_is_excluded_from_slot_entitlement():
    """Phase 4: a real $1.00 live checkout against "AnyAiCam Test System"
    (price_1UDegIGllhK80H2nHCfGvcz8) already succeeded, proving live
    Stripe/payment/webhook plumbing -- but it must NEVER be treated as
    proof of, or a source of, camera-slot entitlement. It is deliberately
    absent from PLAN_TIERS/PRICE_ID_CAMERA_SLOT_MAP, so resolve_tier()
    correctly returns None for it even if a real webhook event for this
    exact purchase were replayed through sync_entitlement_from_stripe_
    event() -- the event would be recorded as "ignored", not granted."""
    dollar_test_price_id = "price_1UDegIGllhK80H2nHCfGvcz8"
    assert dollar_test_price_id not in ce.PRICE_ID_CAMERA_SLOT_MAP
    assert ce.resolve_tier(dollar_test_price_id) is None
    for _, _, _, _, _, _, env_var, _ in ce.PLAN_TIERS:
        assert os.environ.get(env_var, "") != dollar_test_price_id


def test_dollar_test_checkout_event_grants_nothing_end_to_end(db_path):
    """Full sync_entitlement_from_stripe_event() path with the real
    Price ID, proving the whole pipeline -- not just resolve_tier() in
    isolation -- treats it as unrecognized."""
    _seed_customer(db_path, email="real-customer@example.test")
    event = {
        "id": "evt_dollar_test_1", "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_dollar_test", "customer": "cus_dollar_test",
            "customer_details": {"email": "real-customer@example.test"},
            "metadata": {"anyaicam_stripe_price_id": "price_1UDegIGllhK80H2nHCfGvcz8", "anyaicam_customer_id": "cust-1"},
        }},
    }
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(event)
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "ignored"
    assert total == 0
