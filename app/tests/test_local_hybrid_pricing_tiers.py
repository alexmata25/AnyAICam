"""Webhook entitlement lines for camera_slots_local / camera_slots_hybrid.

The fixed 8/16/32/64-camera Local/Hybrid subscription tiers were removed on
2026-10-08 (owner decision; camera plans are per camera). What stays is the
Stripe webhook machinery for camera_slots_* entitlements: these tests inject
their own Price-ID map and prove Local and Hybrid lines stay independent and
that an unrecognised Price ID grants nothing end to end."""
import os
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import customer_entitlements as ce


# Approved working pricing, 2026-09-30 (pricing_catalog.BASE_PLANS).
# Billing reads Stripe's current state and applies nothing without it (Codex
# audit of 3f5b9c4, finding 9): these tests get a Stripe that follows their
# own events (conftest.stripe_follows_events).
pytestmark = pytest.mark.usefixtures("stripe_follows_events")


EXPECTED = {
    ("local", "1-8"): (8, 14.99),
    ("local", "9-16"): (16, 24.99),
    ("local", "17-32"): (32, 39.99),
    ("local", "33-64"): (64, 69.99),
    ("hybrid", "1-8"): (8, 24.99),
    ("hybrid", "9-16"): (16, 39.99),
    ("hybrid", "17-32"): (32, 69.99),
    ("hybrid", "33-64"): (64, 99.99),
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
    # Owner, 2026-10-02: Local and Hybrid are alternative base plans, never
    # additive -- capacity is the larger one, not the sum.
    assert total == 8


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
# STALE AS OF 2026-09-30: every one of these eight Price objects was
# created at the PREVIOUS amounts (Local $14.99/$19.99/$29.99/$49.99,
# Hybrid $29.99/$49.99/$89.99/$149.99). Only Local 1-8 ($14.99/month)
# matches the approved pricing; do not configure any of the others, and
# confirm Local 1-8's amount and interval in Stripe before reusing it.
# Earlier history -- as of 2026-09-21 Local became a one-time purchase (billing_type
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
    "price_1UDee3GllhK80H2nK4uQLBKe": ("camera_slots_local", 8, "recurring"),
    "price_1UDeehGllhK80H2nf61pHif8": ("camera_slots_local", 16, "recurring"),
    "price_1UDeeqGllhK80H2nyZ9Mz8BE": ("camera_slots_local", 32, "recurring"),
    "price_1UDeeQGllhK80H2nVRRrsksb": ("camera_slots_local", 64, "recurring"),
    "price_1UDecwGllhK80H2nEDSd6dww": ("camera_slots_hybrid", 8, "recurring"),
    "price_1UDef0GllhK80H2n5NcWtqDK": ("camera_slots_hybrid", 16, "recurring"),
    "price_1UDef2GllhK80H2nbIf7ZTHY": ("camera_slots_hybrid", 32, "recurring"),
    "price_1UDef5GllhK80H2nNqYcCbMs": ("camera_slots_hybrid", 64, "recurring"),
}


# --------------------------------- Phase 4: the $1 live plumbing-test product


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
