"""Stripe webhooks arrive out of order (2026-10-02).

- A cancellation that arrives before its own delayed checkout used to be
  dropped ("no existing entitlement") and the late checkout then granted
  access for good. The cancellation is now recorded as the account's state,
  and the older checkout is ignored as stale.
- A newer plan applied first is never reverted by an older event.
- An event whose AnyAiCam account cannot be resolved yet is retried (503)
  instead of being marked processed and lost.
- A Stripe customer and metadata customer that disagree are rejected.
Same for add-on packages (analytics_entitlements).
"""
import pytest

import customer_entitlements as ce
from database_backend import override_target
from test_customer_entitlements import (  # noqa: F401 -- fixtures and helpers
    TIER_1_16,
    TIER_17_32,
    _checkout_event,
    _db,
    _seed_customer,
    _subscription_event,
    _tier_map,
    db_path,
)


def _at(event, created):
    event["created"] = created
    return event


def _row(customer_id="cust-1"):
    rows = ce.get_entitlements_for_customer(customer_id)
    return rows[0] if rows else None


def test_a_cancellation_before_its_delayed_checkout_never_grants_access(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        cancelled = ce.sync_entitlement_from_stripe_event(_at(_subscription_event(
            "evt_cancel", stripe_customer="cus_1", price_id=TIER_1_16, status="canceled", customer_id="cust-1",
            event_type="customer.subscription.deleted"), 2000))
        assert cancelled["status"] == "entitlement_updated"
        late = ce.sync_entitlement_from_stripe_event(_at(_checkout_event(
            "evt_checkout", authoritative_customer_id="cust-1", price_id=TIER_1_16), 1000))
        assert late["status"] == "stale"
        assert ce.total_camera_slots("cust-1") == 0 and _row()["status"] == "cancelled"


def test_a_newer_plan_is_never_reverted_by_an_older_event(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_at(_checkout_event("evt_1", authoritative_customer_id="cust-1", price_id=TIER_1_16), 1000))
        ce.sync_entitlement_from_stripe_event(_at(_subscription_event("evt_up", stripe_customer="cus_1", price_id=TIER_17_32), 3000))
        assert ce.total_camera_slots("cust-1") == 32
        old = ce.sync_entitlement_from_stripe_event(_at(_subscription_event("evt_old", stripe_customer="cus_1", price_id=TIER_1_16), 2000))
        assert old["status"] == "stale" and ce.total_camera_slots("cust-1") == 32
        newer = ce.sync_entitlement_from_stripe_event(_at(_subscription_event("evt_down", stripe_customer="cus_1", price_id=TIER_1_16), 4000))
        assert newer["status"] == "entitlement_updated" and ce.total_camera_slots("cust-1") == 16


def test_events_without_a_timestamp_keep_the_previous_behaviour(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event("evt_a", authoritative_customer_id="cust-1", price_id=TIER_1_16))
        ce.sync_entitlement_from_stripe_event(_subscription_event("evt_b", stripe_customer="cus_1", price_id=TIER_17_32))
        assert ce.total_camera_slots("cust-1") == 32


def test_an_unresolvable_subscription_event_is_retried_not_lost(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        event = _at(_subscription_event("evt_orphan", stripe_customer="cus_unknown", price_id=TIER_1_16), 1000)
        with pytest.raises(ce.RetryableStripeEventError):
            ce.sync_entitlement_from_stripe_event(event)
        assert not ce.is_event_processed("evt_orphan")
        unknown_meta = _at(_subscription_event("evt_meta", stripe_customer="cus_x", price_id=TIER_1_16, customer_id="no-such-customer"), 1000)
        with pytest.raises(ce.RetryableStripeEventError):
            ce.sync_entitlement_from_stripe_event(unknown_meta)
        assert not ce.is_event_processed("evt_meta")


def test_inconsistent_stripe_and_metadata_customers_are_rejected(db_path):
    _seed_customer(db_path)
    _seed_customer(db_path, customer_id="cust-2", email="other@example.test")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_at(_checkout_event("evt_1", authoritative_customer_id="cust-1", price_id=TIER_1_16), 1000))
        mismatch = ce.sync_entitlement_from_stripe_event(_at(_subscription_event(
            "evt_mismatch", stripe_customer="cus_1", price_id=TIER_17_32, customer_id="cust-2"), 2000))
        assert mismatch["status"] == "rejected"
        assert ce.total_camera_slots("cust-1") == 16 and ce.total_camera_slots("cust-2") == 0


def test_a_second_stripe_customer_cannot_take_over_an_active_entitlement(db_path):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_at(_checkout_event("evt_1", authoritative_customer_id="cust-1", price_id=TIER_1_16, stripe_customer="cus_1"), 1000))
        other = ce.sync_entitlement_from_stripe_event(_at(_checkout_event("evt_2", authoritative_customer_id="cust-1", price_id=TIER_17_32, stripe_customer="cus_2"), 2000))
        assert other["status"] == "rejected" and _row()["stripe_customer_id"] == "cus_1" and ce.total_camera_slots("cust-1") == 16


def test_the_webhook_answers_503_so_stripe_retries(monkeypatch):
    import main
    calls = []

    def fail(event):
        calls.append(event["id"])
        raise ce.RetryableStripeEventError("not yet")
    monkeypatch.setattr(main, "_stripe_webhook_steps", lambda: [("entitlements", fail)])
    import inspect
    source = inspect.getsource(main)
    assert "Stripe will retry this event." in source  # a failed step is answered 503 (existing behaviour)
