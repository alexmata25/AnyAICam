"""Provisioning Phase 5: one-time HARDWARE order records (Ryzen
appliances, the Numato relay module), and -- most importantly -- proof
that they stay completely separate from customer_entitlements' recurring
Local/Hybrid camera-slot subscriptions in both directions. See hardware_
orders.py's module docstring for the full separation contract.
"""
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import customer_entitlements as ce
import hardware_orders as ho


RYZEN_STARTER_TEST_PRICE = "price_test_ryzen_starter"
RYZEN_ENTERPRISE_TEST_PRICE = "price_test_ryzen_enterprise"
RYZEN_AAC_TEST_PRICE = "price_test_ryzen_aac_facial"
RELAY_TEST_PRICE = "price_test_relay_numato_3ch"
CAMERA_SLOT_TEST_PRICE = "price_test_local_1_8"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_hardware_orders.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        yield


@pytest.fixture()
def _hardware_price_map(monkeypatch):
    monkeypatch.setattr(ho, "HARDWARE_PRICE_MAP", {
        RYZEN_STARTER_TEST_PRICE: {"sku": "AIC-APPLIANCE-RYZEN-STARTER", "product": "ryzen_starter", "name": "AnyAiCam Ryzen Starter Appliance", "amount_cents": 124999},
        RYZEN_ENTERPRISE_TEST_PRICE: {"sku": "AIC-APPLIANCE-RYZEN-ENTERPRISE", "product": "ryzen_enterprise", "name": "AnyAiCam Ryzen Enterprise Appliance", "amount_cents": 174999},
        RYZEN_AAC_TEST_PRICE: {"sku": "AIC-APPLIANCE-RYZEN-AAC-FACIAL", "product": "ryzen_aac_facial_recognition", "name": "AnyAiCam Ryzen AAC Facial Recognition Appliance", "amount_cents": 224999},
        RELAY_TEST_PRICE: {"sku": "AIC-RELAY-NUMATO-3CH", "product": "numato_3_channel_relay", "name": "Numato 3-Channel Relay Module", "amount_cents": 14999},
    })


@pytest.fixture()
def _camera_slot_price_map(monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
        CAMERA_SLOT_TEST_PRICE: {"product": "camera_slots_local", "camera_slot_maximum": 8},
    })


def _seed_customer(db_path, customer_id="cust-1", email="real-customer@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _hardware_checkout_event(event_id, price_id, *, email="real-customer@example.test", stripe_customer="cus_1", customer_id=None, mode="payment", payment_status="paid"):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_{event_id}", "mode": mode, "customer": stripe_customer,
            "customer_details": {"email": email}, "payment_status": payment_status, "metadata": metadata,
        }},
    }


# --------------------------------------------------------------- catalog


def test_catalog_has_exactly_the_four_expected_skus_at_the_verified_prices():
    rows = ho._catalog_rows()
    assert len(rows) == 4
    by_product = {r["product"]: (r["sku"], r["amount_cents"]) for r in rows}
    assert by_product == {
        "ryzen_starter": ("AIC-APPLIANCE-RYZEN-STARTER", 124999),
        "ryzen_enterprise": ("AIC-APPLIANCE-RYZEN-ENTERPRISE", 174999),
        "ryzen_aac_facial_recognition": ("AIC-APPLIANCE-RYZEN-AAC-FACIAL", 224999),
        "numato_3_channel_relay": ("AIC-RELAY-NUMATO-3CH", 14999),
    }


def test_no_sku_ships_with_a_stripe_price_id_configured_by_default():
    """Core audit finding: no Stripe TEST/SANDBOX secret key was
    available in this environment, so none of these four Price IDs was
    created or verified -- every entry must ship unset, never a
    fabricated placeholder."""
    for r in ho._catalog_rows():
        assert r["stripe_price_id"] is None
    assert ho.HARDWARE_PRICE_MAP == {}


def test_ryzen_enterprise_product_key_never_collides_with_the_legacy_software_enterprise_key():
    """The AAC-vs-Enterprise / hardware-vs-software naming guard: this
    codebase's pre-existing LICENSE_PLAN_FEATURES/STRIPE_PRICE_ENTERPRISE
    uses the bare key 'enterprise' for an unrelated SOFTWARE feature tier.
    HARDWARE_CATALOG must never reuse that bare string for the physical
    Ryzen Enterprise appliance."""
    product_keys = {product for _, product, _, _, _ in ho.HARDWARE_CATALOG}
    assert "enterprise" not in product_keys
    assert "ryzen_enterprise" in product_keys


def test_aac_facial_recognition_is_a_distinct_sku_and_product_from_plain_enterprise():
    rows = ho._catalog_rows()
    enterprise = next(r for r in rows if r["product"] == "ryzen_enterprise")
    aac = next(r for r in rows if r["product"] == "ryzen_aac_facial_recognition")
    assert enterprise["sku"] != aac["sku"]
    assert enterprise["amount_cents"] != aac["amount_cents"]


# -------------------------------------------- resolve_hardware_sku() fail-closed


def test_resolve_hardware_sku_returns_none_for_an_unrecognized_price_id(_hardware_price_map):
    assert ho.resolve_hardware_sku("price_totally_unknown") is None
    assert ho.resolve_hardware_sku("") is None


def test_resolve_hardware_sku_resolves_each_of_the_four_configured_prices(_hardware_price_map):
    assert ho.resolve_hardware_sku(RYZEN_STARTER_TEST_PRICE)["sku"] == "AIC-APPLIANCE-RYZEN-STARTER"
    assert ho.resolve_hardware_sku(RELAY_TEST_PRICE)["amount_cents"] == 14999


# --------------------------------------------- checkout.session.completed sync


def test_hardware_checkout_creates_an_order_for_a_signed_in_customer(db_path, _hardware_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ho.sync_hardware_order_from_stripe_event(
            _hardware_checkout_event("evt_ryzen_starter", RYZEN_STARTER_TEST_PRICE, customer_id="cust-1")
        )
        orders = ho.get_orders_for_customer("cust-1")
    assert result["status"] == "order_recorded"
    assert len(orders) == 1
    assert orders[0]["sku"] == "AIC-APPLIANCE-RYZEN-STARTER"
    assert orders[0]["status"] == "paid"
    assert orders[0]["amount_cents"] == 124999


def test_unrecognized_price_id_creates_no_order_at_all(db_path, _hardware_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ho.sync_hardware_order_from_stripe_event(
            _hardware_checkout_event("evt_unknown", "price_not_in_any_catalog", customer_id="cust-1")
        )
        orders = ho.get_orders_for_customer("cust-1")
    assert result["status"] == "ignored"
    assert orders == []


def test_replayed_webhook_for_the_same_session_and_sku_updates_in_place_not_a_duplicate(db_path, _hardware_price_map):
    _seed_customer(db_path)
    event = _hardware_checkout_event("evt_replay", RYZEN_ENTERPRISE_TEST_PRICE, customer_id="cust-1", stripe_customer="cus_replay")
    with override_target(sqlite_path=db_path):
        ho.sync_hardware_order_from_stripe_event(event)
        ho.sync_hardware_order_from_stripe_event(event)  # exact same event, replayed
        orders = ho.get_orders_for_customer("cust-1")
    assert len(orders) == 1


def test_checkout_before_registration_creates_a_pending_link_not_a_duplicate_customer(db_path, _hardware_price_map):
    with override_target(sqlite_path=db_path):
        result = ho.sync_hardware_order_from_stripe_event(
            _hardware_checkout_event("evt_before_reg", RELAY_TEST_PRICE, email="new-buyer@example.test")
        )
    assert result["status"] == "pending_link_created"


def test_non_payment_mode_session_is_never_treated_as_a_hardware_purchase(db_path, _hardware_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ho.sync_hardware_order_from_stripe_event(
            _hardware_checkout_event("evt_sub_mode", RYZEN_STARTER_TEST_PRICE, customer_id="cust-1", mode="subscription")
        )
        orders = ho.get_orders_for_customer("cust-1")
    assert result["status"] == "ignored"
    assert orders == []


# ------------------------------------------ THE SEPARATION CONTRACT (fail-closed)


def test_a_hardware_price_id_never_grants_a_camera_slot(db_path, _hardware_price_map, _camera_slot_price_map):
    """The core requirement: buying Ryzen Starter must never touch
    customer_entitlements, in either direction of the sync."""
    _seed_customer(db_path)
    event = _hardware_checkout_event("evt_hw_no_slots", RYZEN_STARTER_TEST_PRICE, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        # Run BOTH syncs, exactly as main.py's webhook route does.
        ce.sync_entitlement_from_stripe_event(event)
        ho.sync_hardware_order_from_stripe_event(event)
        total_slots = ce.total_camera_slots("cust-1")
        entitlements = ce.get_entitlements_for_customer("cust-1")
        orders = ho.get_orders_for_customer("cust-1")
    assert total_slots == 0
    assert entitlements == []
    assert len(orders) == 1
    assert orders[0]["sku"] == "AIC-APPLIANCE-RYZEN-STARTER"


def test_a_camera_slot_price_id_never_creates_a_hardware_order(db_path, _hardware_price_map, _camera_slot_price_map):
    """The mirror-image requirement: buying a Local 1-8 subscription must
    never create a hardware_orders row."""
    _seed_customer(db_path)
    event = _hardware_checkout_event("evt_slots_no_hw", CAMERA_SLOT_TEST_PRICE, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)
        ho.sync_hardware_order_from_stripe_event(event)
        total_slots = ce.total_camera_slots("cust-1")
        orders = ho.get_orders_for_customer("cust-1")
    assert total_slots == 8
    assert orders == []


def test_buying_hardware_and_a_camera_slot_subscription_together_creates_exactly_one_of_each(db_path, _hardware_price_map, _camera_slot_price_map):
    _seed_customer(db_path)
    hw_event = _hardware_checkout_event("evt_combo_hw", RELAY_TEST_PRICE, customer_id="cust-1", stripe_customer="cus_hw")
    slot_event = _hardware_checkout_event("evt_combo_slot", CAMERA_SLOT_TEST_PRICE, customer_id="cust-1", stripe_customer="cus_slot")
    with override_target(sqlite_path=db_path):
        for event in (hw_event, slot_event):
            ce.sync_entitlement_from_stripe_event(event)
            ho.sync_hardware_order_from_stripe_event(event)
        total_slots = ce.total_camera_slots("cust-1")
        orders = ho.get_orders_for_customer("cust-1")
    assert total_slots == 8
    assert len(orders) == 1
    assert orders[0]["sku"] == "AIC-RELAY-NUMATO-3CH"


def test_no_hardware_order_test_creates_a_real_stripe_charge():
    """Sanity guard for this whole file: every test above exercises
    sync_hardware_order_from_stripe_event() against a hand-built event
    dict -- none of them calls Stripe, creates a live/test Checkout
    Session, or moves real money. This test exists purely to make that
    property explicit and grep-able."""
    import inspect
    source = inspect.getsource(ho)
    assert "api.stripe.com" not in source
    assert "stripe_api_post" not in source
