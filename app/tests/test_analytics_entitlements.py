"""Stripe TEST analytics-add-on wiring: proves the 7 purchasable addon_keys
resolve to the existing analytic_key scheme (customer_analytics_panel.
ANALYTIC_LABELS's own keys for the 4 pre-existing analytics, extended
consistently for the others), that purchase/cancel drives the pre-existing
analytics_subscriptions table (never a second architecture), that multiple
add-ons coexist additively, and that analytics stays completely independent
of camera-slot entitlements and hardware orders in both directions -- see
analytics_entitlements.py's module docstring for the full design, including
the 2026-09-21 correction that "advanced_analytics" is ONE addon_key
granting FOUR analytic_keys (smart_motion/people_counting/lpr/ppe) together
from a single Stripe Price, unlike every other addon_key here which grants
exactly one analytic_key of its own name.
"""
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import analytics_entitlements as ae
import customer_entitlements as ce
import hardware_orders as ho


ADVANCED_ANALYTICS_TEST_PRICE = "price_test_advanced_analytics"
FACIAL_RECOGNITION_TEST_PRICE = "price_test_analytics_facial_recognition"
TALK_DOWN_TEST_PRICE = "price_test_analytics_talk_down"
AI_ESSENTIALS_TEST_PRICE = "price_test_analytics_ai_essentials"
AI_PROFESSIONAL_TEST_PRICE = "price_test_analytics_ai_professional"
VEHICLE_INTELLIGENCE_TEST_PRICE = "price_test_analytics_vehicle_intelligence"
CLOUD_OVERFLOW_TEST_PRICE = "price_test_analytics_cloud_overflow"

TEST_PRICE_MAP = {
    ADVANCED_ANALYTICS_TEST_PRICE: {
        "addon_key": "advanced_analytics", "label": "Advanced Analytics",
        "analytic_keys": ("smart_motion", "people_counting", "lpr", "ppe"),
    },
    FACIAL_RECOGNITION_TEST_PRICE: {"addon_key": "facial_recognition", "label": "Face Access", "analytic_keys": ("facial_recognition",)},
    TALK_DOWN_TEST_PRICE: {"addon_key": "talk_down", "label": "Talk Down", "analytic_keys": ("talk_down",)},
    AI_ESSENTIALS_TEST_PRICE: {"addon_key": "ai_essentials", "label": "AnyAiCam AI Essentials", "analytic_keys": ("ai_essentials",)},
    AI_PROFESSIONAL_TEST_PRICE: {"addon_key": "ai_professional", "label": "AnyAiCam AI Professional", "analytic_keys": ("ai_professional",)},
    VEHICLE_INTELLIGENCE_TEST_PRICE: {"addon_key": "vehicle_intelligence", "label": "Vehicle Intelligence", "analytic_keys": ("vehicle_intelligence",)},
    CLOUD_OVERFLOW_TEST_PRICE: {"addon_key": "cloud_overflow", "label": "Cloud Overflow", "analytic_keys": ("cloud_overflow",)},
}

ALL_ANALYTIC_KEYS = sorted({key for item in TEST_PRICE_MAP.values() for key in item["analytic_keys"]})


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


def test_all_seven_addon_price_ids_resolve_to_the_expected_addon(_analytics_price_map):
    for price_id, expected in TEST_PRICE_MAP.items():
        resolved = ae.resolve_addon(price_id)
        assert resolved is not None
        assert resolved["addon_key"] == expected["addon_key"]
        assert set(resolved["analytic_keys"]) == set(expected["analytic_keys"])


def test_advanced_analytics_resolves_to_all_four_preexisting_analytics_panel_keys(_analytics_price_map):
    """smart_motion/people_counting/lpr/ppe must match customer_analytics_
    panel.ANALYTIC_LABELS exactly -- that is the table assign_entitlement()
    actually validates a per-camera analytic_key against."""
    import customer_analytics_panel as cap

    resolved = ae.resolve_addon(ADVANCED_ANALYTICS_TEST_PRICE)
    for analytic_key in resolved["analytic_keys"]:
        assert analytic_key in cap.ANALYTIC_LABELS


def test_unknown_price_id_resolves_to_none(_analytics_price_map):
    assert ae.resolve_addon("price_totally_unknown") is None
    assert ae.resolve_addon("") is None
    assert ae.resolve_addon(None) is None


def test_no_analytics_price_id_configured_by_default():
    """Fail-closed by default, same audit posture as PRICE_ID_CAMERA_SLOT_
    MAP/HARDWARE_PRICE_MAP: an unset env var grants nothing."""
    mapping = ae._load_price_map()
    assert mapping == {}


def test_setting_the_advanced_analytics_env_var_makes_that_addon_resolvable(monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_real_advanced_analytics")
    mapping = ae._load_price_map()
    entry = mapping["price_real_advanced_analytics"]
    assert entry["addon_key"] == "advanced_analytics"
    assert set(entry["analytic_keys"]) == {"smart_motion", "people_counting", "lpr", "ppe"}


# --------------------------------------------------------- grant / revoke


def test_checkout_completed_grants_all_four_advanced_analytics_keys(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        result = ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE))
        assert result["status"] == "analytics_subscription_updated"
        assert result["addon_key"] == "advanced_analytics"
        assert set(result["analytic_keys"]) == {"smart_motion", "people_counting", "lpr", "ppe"}
        assert len(result["subscription_ids"]) == 4
        assert ae.get_active_analytics_for_customer("cust-1") == ["lpr", "people_counting", "ppe", "smart_motion"]


def test_subscription_deleted_revokes_all_four_advanced_analytics_keys_together(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE))
        assert len(ae.get_active_analytics_for_customer("cust-1")) == 4

        ae.sync_analytics_from_stripe_event(
            _subscription_event("evt_2", "customer.subscription.deleted", ADVANCED_ANALYTICS_TEST_PRICE)
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
            _subscription_event("evt_1", "customer.subscription.created", TALK_DOWN_TEST_PRICE)
        )
        assert result["status"] == "ignored"


def test_redelivering_the_same_checkout_event_does_not_duplicate_the_subscription_row(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", TALK_DOWN_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", TALK_DOWN_TEST_PRICE))
        rows = ae.get_analytics_subscriptions_for_customer("cust-1")
        assert len(rows) == 1


def test_redelivering_the_same_advanced_analytics_checkout_does_not_duplicate_any_of_the_four_rows(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE))
        rows = ae.get_analytics_subscriptions_for_customer("cust-1")
        assert len(rows) == 4


# ------------------------------------------------------- multiple analytics


def test_multiple_addons_coexist_additively(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_2", TALK_DOWN_TEST_PRICE))
        assert ae.get_active_analytics_for_customer("cust-1") == ["lpr", "people_counting", "ppe", "smart_motion", "talk_down"]


def test_cancelling_one_addon_preserves_the_others(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_2", TALK_DOWN_TEST_PRICE))

        ae.sync_analytics_from_stripe_event(
            _subscription_event("evt_3", "customer.subscription.deleted", TALK_DOWN_TEST_PRICE)
        )
        remaining = ae.get_active_analytics_for_customer("cust-1")
        assert "talk_down" not in remaining
        assert remaining == ["lpr", "people_counting", "ppe", "smart_motion"]


def test_all_seven_addons_can_be_purchased_simultaneously(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        for i, price_id in enumerate(TEST_PRICE_MAP):
            ae.sync_analytics_from_stripe_event(_checkout_event(f"evt_{i}", price_id))
        active = ae.get_active_analytics_for_customer("cust-1")
        assert active == ALL_ANALYTIC_KEYS


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
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", TALK_DOWN_TEST_PRICE))
        assert ce.get_entitlements_for_customer("cust-1") == []
        assert ce.total_camera_slots("cust-1") == 0


def test_an_analytics_purchase_creates_no_hardware_order(db_path, _analytics_price_map):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", TALK_DOWN_TEST_PRICE))
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


def test_an_advanced_analytics_purchase_under_an_unknown_email_creates_one_pending_link_per_key(db_path, _analytics_price_map):
    """Before this fix, an analytics checkout with no matching customer
    just returned 'ignored' and the purchase was silently dropped --
    unlike camera-slot and hardware purchases, which already had this
    safety net (customer_entitlements.create_pending_link()/hardware_
    orders.create_pending_link()). One purchase can grant more than one
    analytic_key (advanced_analytics grants four), so pending_analytics_
    links -- keyed one row per analytic_key -- gets one pending row per
    key from this single purchase."""
    with override_target(sqlite_path=db_path):
        result = ae.sync_analytics_from_stripe_event(
            _checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE, email="brand-new@example.test")
        )
        assert result["status"] == "pending_link_created"
        assert result["addon_key"] == "advanced_analytics"
        assert len(result["pending_link_ids"]) == 4
        assert set(result["analytic_keys"]) == {"smart_motion", "people_counting", "lpr", "ppe"}


def test_resolving_pending_advanced_analytics_links_grants_all_four_entitlements(db_path, _analytics_price_map):
    # Purchase happens BEFORE the customer account exists (no matching row
    # at checkout time) -- then the account is created/approved afterward,
    # under the same email, exactly like a real checkout-before-
    # registration website visitor.
    with override_target(sqlite_path=db_path):
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_1", ADVANCED_ANALYTICS_TEST_PRICE, email="brand-new@example.test"))
        assert ae.get_active_analytics_for_customer("cust-2") == []

    _seed_customer(db_path, customer_id="cust-2", email="brand-new@example.test")
    with override_target(sqlite_path=db_path):
        resolved = ae.resolve_pending_links_for_customer("cust-2", "brand-new@example.test")
        assert len(resolved) == 4
        assert ae.get_active_analytics_for_customer("cust-2") == ["lpr", "people_counting", "ppe", "smart_motion"]


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
