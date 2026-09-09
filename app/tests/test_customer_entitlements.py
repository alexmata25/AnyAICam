"""Pure-logic coverage for customer_entitlements.py: the authoritative
entitlement model this Phase 1 provisioning work introduces, and its
(deliberately not-yet-wired-into-production) Stripe webhook bridge.

See customer_entitlements.py's own module docstring for the audit
finding (three pre-existing, disconnected, non-Stripe-verified
"entitlement-shaped" concepts already in the codebase) that motivates
building this as a new, additive model instead of extending any of them.
"""
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import customer_entitlements as ce


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_entitlements.db"


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


# --------------------------------------------------------------- entitlements


def test_upsert_creates_a_new_entitlement(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        entitlement = ce.upsert_entitlement(customer_id="cust-1", product="starter", camera_slot_quantity=4)
    assert entitlement["customer_id"] == "cust-1"
    assert entitlement["camera_slot_quantity"] == 4
    assert entitlement["status"] == "active"


def test_upsert_updates_the_same_row_in_place_for_the_same_customer_and_product(db_path):
    """An entitlement always reflects 'what Stripe currently says' -- a
    plan upgrade must not accumulate a second row."""
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        first = ce.upsert_entitlement(customer_id="cust-1", product="starter", camera_slot_quantity=4)
        second = ce.upsert_entitlement(customer_id="cust-1", product="starter", camera_slot_quantity=10)
        all_rows = ce.get_entitlements_for_customer("cust-1")
    assert first["id"] == second["id"]
    assert second["camera_slot_quantity"] == 10
    assert len(all_rows) == 1


def test_upsert_never_blanks_a_previously_recorded_stripe_reference(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.upsert_entitlement(customer_id="cust-1", product="starter", camera_slot_quantity=4, stripe_customer_id="cus_123")
        updated = ce.upsert_entitlement(customer_id="cust-1", product="starter", camera_slot_quantity=4, status="active")
    assert updated["stripe_customer_id"] == "cus_123"


def test_total_camera_slots_sums_only_active_entitlements_across_products(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.upsert_entitlement(customer_id="cust-1", product="starter", camera_slot_quantity=4)
        ce.upsert_entitlement(customer_id="cust-1", product="aac_facial_recognition", camera_slot_quantity=0, status="active")
        ce.upsert_entitlement(customer_id="cust-1", product="enterprise", camera_slot_quantity=25, status="cancelled")
        total = ce.total_camera_slots("cust-1")
    assert total == 4  # the cancelled enterprise entitlement must not count


def test_total_camera_slots_is_zero_for_a_customer_with_no_entitlements(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        assert ce.total_camera_slots("cust-1") == 0


# ----------------------------------------------------------- pending links


def test_pending_link_created_when_no_customer_matches_the_email(db_path):
    with override_target(sqlite_path=db_path):
        link = ce.create_pending_link(
            email="Future-Customer@Example.TEST", product="starter", camera_slot_quantity=4, raw_event={"id": "evt_1"},
        )
    assert link["status"] == "pending"
    assert link["normalized_email"] == "future-customer@example.test"  # normalized, not the raw casing


def test_resolving_pending_links_grants_the_entitlement_and_marks_resolved(db_path):
    _seed_customer(db_path, email="new@example.test")
    with override_target(sqlite_path=db_path):
        ce.create_pending_link(email="new@example.test", product="professional", camera_slot_quantity=10, raw_event={"id": "evt_2"})
        resolved_ids = ce.resolve_pending_links_for_customer("cust-1", "new@example.test")
        total = ce.total_camera_slots("cust-1")
    assert len(resolved_ids) == 1
    assert total == 10


def test_resolving_pending_links_is_a_noop_when_none_exist(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        resolved_ids = ce.resolve_pending_links_for_customer("cust-1", "real-customer@example.test")
    assert resolved_ids == []


# --------------------------------------------------------- Stripe webhook bridge


def _checkout_event(event_id="evt_checkout_1", email="real-customer@example.test", plan="starter", stripe_customer="cus_1"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_1",
            "customer": stripe_customer,
            "customer_details": {"email": email},
            "metadata": {"anyaicam_plan": plan},
        }},
    }


def test_checkout_completed_grants_entitlement_for_a_matching_authoritative_customer(db_path):
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(_checkout_event())
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "entitlement_updated"
    assert total == ce.PRODUCT_CAMERA_SLOTS["starter"]


def test_checkout_completed_creates_a_pending_link_when_no_customer_matches(db_path):
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(_checkout_event(email="brand-new@example.test"))
    assert result["status"] == "pending_link_created"


def test_checkout_completed_event_is_idempotent_on_retry(db_path):
    """Stripe retries webhook delivery on anything but a 2xx -- the same
    event id must never grant camera slots twice."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        first = ce.sync_entitlement_from_stripe_event(_checkout_event(event_id="evt_dup"))
        second = ce.sync_entitlement_from_stripe_event(_checkout_event(event_id="evt_dup"))
        entitlements = ce.get_entitlements_for_customer("cust-1")
    assert first["status"] == "entitlement_updated"
    assert second["status"] == "already_processed"
    assert len(entitlements) == 1


def test_checkout_completed_ignored_without_plan_metadata(db_path):
    _seed_customer(db_path)
    event = _checkout_event()
    event["data"]["object"]["metadata"] = {}
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(event)
    assert result["status"] == "ignored"


def test_event_without_an_id_is_rejected_outright(db_path):
    with override_target(sqlite_path=db_path):
        with pytest.raises(ValueError):
            ce.sync_entitlement_from_stripe_event({"type": "checkout.session.completed", "data": {"object": {}}})


def test_subscription_deleted_cancels_the_entitlement_and_zeroes_camera_slots(db_path):
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event(stripe_customer="cus_sub_1"))
        cancel_event = {
            "id": "evt_cancel_1",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_1", "customer": "cus_sub_1", "status": "canceled", "metadata": {"anyaicam_plan": "starter"}}},
        }
        result = ce.sync_entitlement_from_stripe_event(cancel_event)
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "entitlement_updated"
    assert total == 0


def test_subscription_update_for_an_unknown_stripe_customer_is_ignored_not_fabricated(db_path):
    with override_target(sqlite_path=db_path):
        event = {
            "id": "evt_unknown_1",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_x", "customer": "cus_never_seen", "status": "active", "metadata": {"anyaicam_plan": "starter"}}},
        }
        result = ce.sync_entitlement_from_stripe_event(event)
    assert result["status"] == "ignored"
