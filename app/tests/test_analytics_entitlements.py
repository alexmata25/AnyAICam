"""Stripe TEST analytics-add-on wiring: proves the 10 analytics Price IDs
resolve to the existing analytic_key scheme (customer_analytics_panel.
ANALYTIC_LABELS's own keys for the 4 pre-existing analytics, extended
consistently for the 6 new ones), that purchase/cancel drives the
pre-existing analytics_subscriptions table (never a second architecture),
that multiple analytics coexist additively, and that analytics stays
completely independent of camera-slot entitlements and hardware orders in
both directions -- see analytics_entitlements.py's module docstring for
the full design.
"""
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import analytics_entitlements as ae
import customer_entitlements as ce
import hardware_orders as ho


SMART_MOTION_TEST_PRICE = "price_test_analytics_smart_motion"
PEOPLE_COUNTING_TEST_PRICE = "price_test_analytics_people_counting"
LPR_TEST_PRICE = "price_test_analytics_lpr"
PPE_TEST_PRICE = "price_test_analytics_ppe"
TALK_DOWN_TEST_PRICE = "price_test_analytics_talk_down"
AI_ESSENTIALS_TEST_PRICE = "price_test_analytics_ai_essentials"
AI_PROFESSIONAL_TEST_PRICE = "price_test_analytics_ai_professional"
VEHICLE_INTELLIGENCE_TEST_PRICE = "price_test_analytics_vehicle_intelligence"
CLOUD_OVERFLOW_TEST_PRICE = "price_test_analytics_cloud_overflow"
FACIAL_RECOGNITION_TEST_PRICE = "price_test_analytics_facial_recognition"

TEST_PRICE_MAP = {
    SMART_MOTION_TEST_PRICE: {"analytic_key": "smart_motion", "label": "Smart Motion"},
    PEOPLE_COUNTING_TEST_PRICE: {"analytic_key": "people_counting", "label": "People Counting"},
    LPR_TEST_PRICE: {"analytic_key": "lpr", "label": "License Plate Recognition"},
    PPE_TEST_PRICE: {"analytic_key": "ppe", "label": "PPE Detection"},
    TALK_DOWN_TEST_PRICE: {"analytic_key": "talk_down", "label": "Talk Down"},
    AI_ESSENTIALS_TEST_PRICE: {"analytic_key": "ai_essentials", "label": "AnyAiCam AI Essentials"},
    AI_PROFESSIONAL_TEST_PRICE: {"analytic_key": "ai_professional", "label": "AnyAiCam AI Professional"},
    VEHICLE_INTELLIGENCE_TEST_PRICE: {"analytic_key": "vehicle_intelligence", "label": "Vehicle Intelligence"},
    CLOUD_OVERFLOW_TEST_PRICE: {"analytic_key": "cloud_overflow", "label": "Cloud Overflow"},
    FACIAL_RECOGNITION_TEST_PRICE: {"analytic_key": "facial_recognition", "label": "Facial Recognition"},
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_analytics_entitlements.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        yield


@pytest.fixture()
def _analytics_price_map(monkeypatch):
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", dict(TEST_PRICE_MAP))


def _seed_customer(db_path, customer_id="cust-1", email="real-customer@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _checkout_event(event_id, price_id, *, email="real-customer@example.test", stripe_customer="cus_1", customer_id=None):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {"id": f"cs_{event_id}", "customer": stripe_customer, "customer_details": {"email": email}, "metadata": metadata}},
    }


def _subscription_event(event_id, event_type, price_id, *, stripe_customer="cus_1", status="active", subscription_id="sub_1", customer_id=None):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": event_type,
        "data": {"object": {
            "id": subscription_id, "customer": stripe_customer, "status": status, "metadata": metadata,
            "items": {"data": [{"price": {"id": price_id}}]},
        }},
    }


# ------------------------------------------------------------- resolution


def test_all_ten_analytics_price_ids_resolve_to_the_expected_feature_key(_analytics_price_map):
    for price_id, expected in TEST_PRICE_MAP.items():
        resolved = ae.resolve_analytic(price_id)
        assert resolved is not None
        assert resolved["analytic_key"] == expected["analytic_key"]


def test_the_four_preexisting_analytics_reuse_customer_analytics_panels_own_keys(_analytics_price_map):
    """smart_motion/people_counting/lpr/ppe must match customer_analytics_
    panel.ANALYTIC_LABELS exactly -- that is the table assign_entitlement()
    actually validates a per-camera analytic_key against."""
    import customer_analytics_panel as cap

    for price_id in (SMART_MOTION_TEST_PRICE, PEOPLE_COUNTING_TEST_PRICE, LPR_TEST_PRICE, PPE_TEST_PRICE):
        resolved = ae.resolve_analytic(price_id)
        assert resolved["analytic_key"] in cap.ANALYTIC_LABELS


def test_unknown_price_id_resolves_to_none(_analytics_price_map):
    assert ae.resolve_analytic("price_totally_unknown") is None
    assert ae.resolve_analytic("") is None
    assert ae.resolve_analytic(None) is None


def test_no_analytics_price_id_configured_by_default():
    """Fail-closed by default, same audit posture as PRICE_ID_CAMERA_SLOT_
    MAP/HARDWARE_PRICE_MAP: an unset env var grants nothing."""
    mapping = ae._load_price_map()
    assert mapping == {}


def test_setting_an_env_var_makes_that_analytic_resolvable(monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_SMART_MOTION", "price_real_smart_motion")
    mapping = ae._load_price_map()
    assert mapping["price_real_smart_motion"]["analytic_key"] == "smart_motion"


# --------------------------------------------------------- grant / revoke


def test_checkout_completed_grants_the_correct_analytic(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        assert result["status"] == "analytics_subscription_updated"
        assert result["analytic_key"] == "smart_motion"
        assert ae.get_active_analytics_for_customer("cust-1") == ["smart_motion"]


def test_subscription_deleted_revokes_the_analytic(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        assert ae.get_active_analytics_for_customer("cust-1") == ["smart_motion"]

        ae.sync_analytics_from_stripe_event(
            _subscription_event("evt_2", "customer.subscription.deleted", SMART_MOTION_TEST_PRICE)
        )
        assert ae.get_active_analytics_for_customer("cust-1") == []


def test_subscription_updated_to_an_inactive_status_also_revokes(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", TALK_DOWN_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(
            _subscription_event("evt_2", "customer.subscription.updated", TALK_DOWN_TEST_PRICE, status="unpaid")
        )
        assert ae.get_active_analytics_for_customer("cust-1") == []


def test_unknown_price_id_is_ignored_and_grants_nothing(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", "price_totally_unknown"))
        assert result["status"] == "ignored"
        assert ae.get_active_analytics_for_customer("cust-1") == []


def test_subscription_created_event_is_ignored_matching_camera_slot_precedent(db_path, _analytics_price_map):
    """checkout.session.completed is the actual grant trigger (it alone
    carries the metadata needed for first-time identity resolution) --
    customer.subscription.created is deliberately a no-op here, exactly
    like customer_entitlements.sync_entitlement_from_stripe_event()."""
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ae.sync_analytics_from_stripe_event(
            _subscription_event("evt_1", "customer.subscription.created", SMART_MOTION_TEST_PRICE)
        )
        assert result["status"] == "ignored"


def test_redelivering_the_same_checkout_event_does_not_duplicate_the_subscription_row(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        rows = ae.get_analytics_subscriptions_for_customer("cust-1")
        assert len(rows) == 1


# ------------------------------------------------------- multiple analytics


def test_multiple_analytics_coexist_additively(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_2", PEOPLE_COUNTING_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_3", TALK_DOWN_TEST_PRICE))
        assert ae.get_active_analytics_for_customer("cust-1") == ["people_counting", "smart_motion", "talk_down"]


def test_cancelling_one_analytic_preserves_the_others(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_2", PEOPLE_COUNTING_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_3", TALK_DOWN_TEST_PRICE))

        ae.sync_analytics_from_stripe_event(
            _subscription_event("evt_4", "customer.subscription.deleted", TALK_DOWN_TEST_PRICE)
        )
        remaining = ae.get_active_analytics_for_customer("cust-1")
        assert "talk_down" not in remaining
        assert remaining == ["people_counting", "smart_motion"]


def test_all_ten_analytics_can_be_purchased_simultaneously(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        for i, price_id in enumerate(TEST_PRICE_MAP):
            ae.sync_analytics_from_stripe_event(_checkout_event(f"evt_{i}", price_id))
        active = ae.get_active_analytics_for_customer("cust-1")
        assert active == sorted(item["analytic_key"] for item in TEST_PRICE_MAP.values())


# --------------------------------------------------------------- isolation


def test_analytics_module_never_imports_customer_entitlements_or_hardware_orders():
    """Structural proof of the separation contract, matching hardware_
    returns.py's own dedicated import-scan test."""
    import inspect

    import_lines = [line.strip() for line in inspect.getsource(ae).splitlines()
                     if line.strip().startswith(("import ", "from "))]
    assert not any("customer_entitlements" in line for line in import_lines)
    assert not any("hardware_orders" in line for line in import_lines)


def test_an_analytics_purchase_grants_no_camera_slots(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        assert ce.get_entitlements_for_customer("cust-1") == []
        assert ce.total_camera_slots("cust-1") == 0


def test_an_analytics_purchase_creates_no_hardware_order(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) AS n FROM hardware_orders").fetchone()["n"]
        assert count == 0


def test_a_camera_slot_purchase_grants_no_analytics(db_path, _analytics_price_map, monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
        "price_local_1_8": {"product": "camera_slots_local", "camera_slot_maximum": 8},
    })
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        event = _checkout_event("evt_1", "price_local_1_8")
        ce.sync_entitlement_from_stripe_event(event)
        assert ae.get_active_analytics_for_customer("cust-1") == []


# -------------------------------------------- checkout-before-registration


def test_an_analytics_purchase_under_an_unknown_email_creates_a_pending_link_not_a_drop(db_path, _analytics_price_map):
    """Before this fix, an analytics checkout with no matching customer
    just returned 'ignored' and the purchase was silently dropped --
    unlike camera-slot and hardware purchases, which already had this
    safety net (customer_entitlements.create_pending_link()/hardware_
    orders.create_pending_link())."""
    with override_target(sqlite_path=db_path):
        result = ae.sync_analytics_from_stripe_event(
            _checkout_event("evt_1", SMART_MOTION_TEST_PRICE, email="brand-new@example.test")
        )
        assert result["status"] == "pending_link_created"
        assert result["analytic_key"] == "smart_motion"


def test_resolving_a_pending_analytics_link_grants_the_entitlement(db_path, _analytics_price_map):
    # Purchase happens BEFORE the customer account exists (no matching row
    # at checkout time) -- then the account is created/approved afterward,
    # under the same email, exactly like a real checkout-before-
    # registration website visitor.
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", SMART_MOTION_TEST_PRICE, email="brand-new@example.test"))
        assert ae.get_active_analytics_for_customer("cust-2") == []

    _seed_customer(db_path, customer_id="cust-2", email="brand-new@example.test")
    with override_target(sqlite_path=db_path):
        resolved = ae.resolve_pending_links_for_customer("cust-2", "brand-new@example.test")
        assert len(resolved) == 1
        assert ae.get_active_analytics_for_customer("cust-2") == ["smart_motion"]


def test_a_hardware_purchase_grants_no_analytics(db_path, _analytics_price_map, monkeypatch):
    monkeypatch.setattr(ho, "HARDWARE_PRICE_MAP", {
        "price_relay": {"sku": "AIC-RELAY-NUMATO-3CH", "product": "numato_3_channel_relay", "name": "Numato 3-Channel Relay Module", "amount_cents": 14999},
    })
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        event = {
            "id": "evt_1", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_1", "customer": "cus_1", "customer_details": {"email": "real-customer@example.test"},
                "metadata": {"anyaicam_stripe_price_id": "price_relay"},
            }},
        }
        ho.sync_hardware_order_from_stripe_event(event)
        assert ae.get_active_analytics_for_customer("cust-1") == []
