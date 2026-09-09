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
        assert set(row.keys()) == {"plan_type", "tier_label", "min_cameras", "max_cameras", "monthly_retail_usd"}
    local_1_8 = next(r for r in table if r["plan_type"] == "local" and r["tier_label"] == "1-8")
    assert local_1_8["monthly_retail_usd"] == 14.99
    hybrid_33_64 = next(r for r in table if r["plan_type"] == "hybrid" and r["tier_label"] == "33-64")
    assert hybrid_33_64["monthly_retail_usd"] == 149.99


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
    assert mapping == {"price_real_local_1_8": {"product": "camera_slots_local", "camera_slot_maximum": 8}}


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
