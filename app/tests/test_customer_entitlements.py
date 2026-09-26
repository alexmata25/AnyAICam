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
#
# Phase 3: fixed camera-slot tiers, server-side Price-ID-keyed only. Every
# test below runs with a synthetic PRICE_ID_CAMERA_SLOT_MAP (monkeypatched)
# standing in for real, ops-configured tier Price IDs -- see customer_
# entitlements.py's module docstring for the audit finding that no such
# Price ID is proven anywhere in existing production configuration today.

TIER_1_16 = "price_test_1_16"
TIER_17_32 = "price_test_17_32"


@pytest.fixture(autouse=True)
def _tier_map(monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
        TIER_1_16: {"product": "camera_slots", "camera_slot_maximum": 16},
        TIER_17_32: {"product": "camera_slots", "camera_slot_maximum": 32},
    })


def _checkout_event(event_id="evt_checkout_1", email="real-customer@example.test", price_id=TIER_1_16, stripe_customer="cus_1",
                     authoritative_customer_id=None):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if authoritative_customer_id is not None:
        metadata["anyaicam_customer_id"] = authoritative_customer_id
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_1",
            "customer": stripe_customer,
            "customer_details": {"email": email},
            "metadata": metadata,
        }},
    }


def _subscription_event(event_id, *, stripe_customer, price_id, status="active", customer_id=None, event_type="customer.subscription.updated"):
    metadata = {}
    if customer_id is not None:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": {
            "id": "sub_1", "customer": stripe_customer, "status": status,
            "items": {"data": [{"price": {"id": price_id}}]},
            "metadata": metadata,
        }},
    }


def test_checkout_completed_grants_the_servers_verified_tier_maximum(db_path):
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(_checkout_event(price_id=TIER_1_16))
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "entitlement_updated"
    assert total == 16


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


def test_checkout_completed_ignored_when_price_id_has_no_verified_tier(db_path):
    """The core Phase 3 security fix: an unmapped Price ID -- including
    one an attacker crafted -- must never grant any camera slots. Fail
    closed, not a guess."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(_checkout_event(price_id="price_totally_unconfigured"))
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "ignored"
    assert total == 0


def test_checkout_completed_ignored_without_any_price_id_metadata(db_path):
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
        ce.sync_entitlement_from_stripe_event(_checkout_event(stripe_customer="cus_sub_1", price_id=TIER_1_16))
        cancel_event = _subscription_event("evt_cancel_1", stripe_customer="cus_sub_1", price_id=TIER_1_16, status="canceled", event_type="customer.subscription.deleted")
        result = ce.sync_entitlement_from_stripe_event(cancel_event)
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "entitlement_updated"
    assert total == 0


def test_subscription_update_for_an_unknown_stripe_customer_is_ignored_not_fabricated(db_path):
    with override_target(sqlite_path=db_path):
        event = _subscription_event("evt_unknown_1", stripe_customer="cus_never_seen", price_id=TIER_1_16)
        result = ce.sync_entitlement_from_stripe_event(event)
    assert result["status"] == "ignored"


# ------------------------------------- Phase 2 (still true under Phase 3): identity


def test_checkout_completed_prefers_authoritative_customer_id_metadata_over_email(db_path):
    """A signed-in checkout must never be resolved by re-deriving
    identity from customer-supplied email when the checkout already
    carries the real customers.id."""
    _seed_customer(db_path, customer_id="cust-1", email="real-customer@example.test")
    _seed_customer(db_path, customer_id="cust-2", email="a-different-email-entirely@example.test")
    with override_target(sqlite_path=db_path):
        # Email on the Stripe session points at cust-1's email, but the
        # authoritative metadata says cust-2 -- metadata must win.
        event = _checkout_event(email="real-customer@example.test", price_id=TIER_1_16, authoritative_customer_id="cust-2")
        result = ce.sync_entitlement_from_stripe_event(event)
        cust1_total = ce.total_camera_slots("cust-1")
        cust2_total = ce.total_camera_slots("cust-2")
    assert result["customer_id"] == "cust-2"
    assert cust1_total == 0
    assert cust2_total == 16


def test_checkout_completed_falls_back_to_email_when_no_authoritative_customer_id(db_path):
    """Checkout-before-registration path: no signed-in session, so no
    anyaicam_customer_id metadata -- email is the only signal, and that's
    the expected, documented fallback (not a bug)."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(_checkout_event())
    assert result["status"] == "entitlement_updated"
    assert result["customer_id"] == "cust-1"


def test_checkout_completed_with_unknown_authoritative_customer_id_falls_back_to_email(db_path):
    """Defensive: a stale/deleted customer_id in metadata must not crash
    or silently drop the purchase -- email reconciliation still applies."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        event = _checkout_event(authoritative_customer_id="cust-deleted")
        result = ce.sync_entitlement_from_stripe_event(event)
    assert result["status"] == "entitlement_updated"
    assert result["customer_id"] == "cust-1"


def test_subscription_change_self_heals_via_metadata_customer_id_when_stripe_customer_id_doesnt_match_yet(db_path):
    """Stripe does not guarantee webhook delivery order relative to which
    stripe_customer_id ends up recorded locally. If the direct
    (stripe_customer_id, product) lookup misses, the subscription's own
    metadata still carries the authoritative customer_id -- fall back to
    that instead of giving up."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        # Entitlement exists (e.g. created via a pending-link resolution
        # that never recorded a stripe_customer_id) but under no
        # stripe_customer_id this subscription event's own value matches.
        ce.upsert_entitlement(customer_id="cust-1", product="camera_slots", camera_slot_quantity=16, status="active")
        event = _subscription_event("evt_sub_selfheal", stripe_customer="cus_not_previously_recorded", price_id=TIER_1_16, customer_id="cust-1")
        result = ce.sync_entitlement_from_stripe_event(event)
        entitlement = ce.get_entitlements_for_customer("cust-1")[0]
    assert result["status"] == "entitlement_updated"
    assert entitlement["stripe_customer_id"] == "cus_not_previously_recorded"  # now backfilled
    assert entitlement["camera_slot_quantity"] == 16


def test_subscription_event_never_fabricates_an_entitlement_out_of_nothing(db_path):
    """The self-heal fallback only ever repairs the LOOKUP for an
    entitlement that already exists -- it must never create one from a
    bare subscription event with no prior checkout at all."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        event = _subscription_event("evt_sub_first", stripe_customer="cus_never_seen_yet", price_id=TIER_1_16, customer_id="cust-1")
        result = ce.sync_entitlement_from_stripe_event(event)
        entitlements = ce.get_entitlements_for_customer("cust-1")
    assert result["status"] == "ignored"
    assert entitlements == []


# ------------------------------------------------------- Phase 3: fixed tiers


def test_subscription_updated_with_the_same_tier_is_a_pure_status_refresh(db_path):
    """A plain subscription.updated for the SAME price/tier (e.g. Stripe
    re-confirming an active subscription) must update in place, active,
    with the same verified maximum -- not accumulate a second row."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event(stripe_customer="cus_refresh_1", price_id=TIER_1_16))
        update_event = _subscription_event("evt_refresh_1", stripe_customer="cus_refresh_1", price_id=TIER_1_16)
        result = ce.sync_entitlement_from_stripe_event(update_event)
        entitlements = ce.get_entitlements_for_customer("cust-1")
    assert result["status"] == "entitlement_updated"
    assert len(entitlements) == 1
    assert entitlements[0]["status"] == "active"
    assert entitlements[0]["camera_slot_quantity"] == 16


def test_upgrade_to_a_higher_tier_updates_the_same_entitlement_row(db_path):
    """The exact Phase 3 upgrade requirement: 1-16 -> 17-32, same
    customer_entitlements record, no duplicate row, VMS refresh sees the
    new maximum."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(_checkout_event(stripe_customer="cus_upgrade_1", price_id=TIER_1_16))
        before = ce.get_entitlements_for_customer("cust-1")
        upgrade_event = _subscription_event("evt_upgrade_1", stripe_customer="cus_upgrade_1", price_id=TIER_17_32)
        result = ce.sync_entitlement_from_stripe_event(upgrade_event)
        after = ce.get_entitlements_for_customer("cust-1")
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "entitlement_updated"
    assert len(after) == 1
    assert after[0]["id"] == before[0]["id"]  # same row, not a second one
    assert after[0]["stripe_price_id"] == TIER_17_32
    assert total == 32


def test_tampered_camera_slot_quantity_metadata_is_never_read(db_path):
    """Even if a checkout.session.completed event carries a forged/stale
    anyaicam_camera_slot_quantity field claiming an enormous number
    (Phase 2's now-removed mechanism), the Phase 3 entitlement sync must
    never read it -- only the server-verified Price ID tier matters."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        event = _checkout_event(price_id=TIER_1_16)
        event["data"]["object"]["metadata"]["anyaicam_camera_slot_quantity"] = "999999"
        result = ce.sync_entitlement_from_stripe_event(event)
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "entitlement_updated"
    assert total == 16  # the real 1-16 tier maximum, not the forged 999999


def test_duplicate_checkout_session_replayed_under_a_different_event_id_does_not_double_count(db_path):
    """Distinct from plain event-id idempotency: even if Stripe somehow
    redelivers the SAME checkout session's completion under two
    different event ids (a stricter case than a same-event-id retry),
    upsert_entitlement()'s per-(customer_id, product) idempotency must
    still prevent double-counting."""
    _seed_customer(db_path, email="real-customer@example.test")
    with override_target(sqlite_path=db_path):
        first = ce.sync_entitlement_from_stripe_event(_checkout_event(event_id="evt_a", price_id=TIER_1_16))
        second = ce.sync_entitlement_from_stripe_event(_checkout_event(event_id="evt_b", price_id=TIER_1_16))
        total = ce.total_camera_slots("cust-1")
        entitlements = ce.get_entitlements_for_customer("cust-1")
    assert first["status"] == "entitlement_updated"
    assert second["status"] == "entitlement_updated"  # a different event id, so processed -- but idempotent by design
    assert total == 16  # not 32
    assert len(entitlements) == 1


def test_resolve_tier_rejects_a_malformed_map_entry(db_path, monkeypatch):
    """Defensive: a bad ANYAICAM_STRIPE_PRICE_TIER_MAP entry (missing
    camera_slot_maximum, non-numeric, empty product) must never resolve
    to a usable tier -- fail closed, not a crash or a guessed number."""
    with override_target(sqlite_path=db_path):
        assert ce.resolve_tier("price_totally_unconfigured") is None
        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {"price_bad": {"product": "camera_slots"}})
        assert ce.resolve_tier("price_bad") is None
        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {"price_bad2": {"camera_slot_maximum": 16}})
        assert ce.resolve_tier("price_bad2") is None
