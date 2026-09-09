"""Provisioning Phase 6: post-purchase customer notifications. Every
email in this file goes through the PreviewEmail backend (the default --
never SMTP unless ANYAICAM_EMAIL_BACKEND=smtp is explicitly set, which no
test here does), writing a JSON file to a temp directory instead of
sending anything real -- see email_service.py. No test in this file sends
a real email.
"""
import dataclasses
import json
import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import email_service
import customer_entitlements as ce
import hardware_orders as ho
import purchase_notifications as pn


LOCAL_1_8 = "price_test_local_1_8"
LOCAL_9_16 = "price_test_local_9_16"
HYBRID_1_8 = "price_test_hybrid_1_8"
HYBRID_17_32 = "price_test_hybrid_17_32"
DOLLAR_TEST_PRICE = "price_1UDegIGllhK80H2nHCfGvcz8"  # deliberately unmapped, see customer_entitlements
RYZEN_STARTER_PRICE = "price_test_ryzen_starter"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_purchase_notifications.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        yield


@pytest.fixture(autouse=True)
def _preview_email_dir(tmp_path, monkeypatch):
    """Every test's emails land in an isolated temp directory, never the
    process-wide default preview dir, and definitely never real SMTP.
    Settings is a frozen dataclass, so this replaces email_service's own
    bound `settings` reference with a modified copy rather than mutating
    a field in place."""
    patched = dataclasses.replace(email_service.settings, email_backend="preview", email_preview_dir=str(tmp_path / "email-preview"))
    monkeypatch.setattr(email_service, "settings", patched)


@pytest.fixture()
def _tier_map(monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
        LOCAL_1_8: {"product": "camera_slots_local", "camera_slot_maximum": 8},
        LOCAL_9_16: {"product": "camera_slots_local", "camera_slot_maximum": 16},
        HYBRID_1_8: {"product": "camera_slots_hybrid", "camera_slot_maximum": 8},
        HYBRID_17_32: {"product": "camera_slots_hybrid", "camera_slot_maximum": 32},
    })


@pytest.fixture()
def _hardware_map(monkeypatch):
    monkeypatch.setattr(ho, "HARDWARE_PRICE_MAP", {
        RYZEN_STARTER_PRICE: {"sku": "AIC-APPLIANCE-RYZEN-STARTER", "product": "ryzen_starter", "name": "AnyAiCam Ryzen Starter Appliance", "amount_cents": 124999},
    })


def _seed_customer(db_path, customer_id="cust-1", email="jane@example.test", name="Jane Doe", partner_id="partner-1"):
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, name, email, "active", "2026-01-01"),
        )
        conn.commit()


def _seed_appliance(db_path, *, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-NOTIFY1"):
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Site", "2026-01-01"))
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,activation_status,created_at) VALUES(?,?,?,?,?,?,?)",
            (appliance_id, customer_id, "site-1", cloud_id, "windows_byo_pc", "activated", "2026-01-01"),
        )
        conn.commit()


def _checkout_event(event_id, price_id, *, email="jane@example.test", stripe_customer="cus_1", customer_id=None, mode="subscription"):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_{event_id}", "mode": mode, "customer": stripe_customer,
            "customer_details": {"email": email}, "payment_status": "paid", "metadata": metadata,
        }},
    }


def _subscription_event(event_id, price_id, *, stripe_customer, event_type="customer.subscription.updated", status="active"):
    return {
        "id": event_id, "type": event_type,
        "data": {"object": {
            "id": f"sub_{event_id}", "customer": stripe_customer, "status": status,
            "items": {"data": [{"price": {"id": price_id}}]}, "metadata": {},
        }},
    }


def _preview_files(tmp_path):
    preview_dir = tmp_path / "email-preview"
    if not preview_dir.exists():
        return []
    return sorted(preview_dir.glob("*.json"))


def _read_previews(tmp_path):
    return [json.loads(p.read_text(encoding="utf-8")) for p in _preview_files(tmp_path)]


# ------------------------------------------------------------- happy paths


def test_local_1_8_sends_exactly_one_ready_email_with_8_slots(db_path, tmp_path, _tier_map):
    _seed_customer(db_path)
    event = _checkout_event("evt_local_1_8", LOCAL_1_8, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)
        result = pn.notify_from_stripe_event(event)
    assert result["status"] == "sent"
    previews = _read_previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "account_ready"
    assert "8" in previews[0]["text"]
    assert "Local" in previews[0]["text"]


def test_hybrid_1_8_sends_exactly_one_ready_email_with_8_hybrid_slots(db_path, tmp_path, _tier_map):
    _seed_customer(db_path)
    event = _checkout_event("evt_hybrid_1_8", HYBRID_1_8, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)
        pn.notify_from_stripe_event(event)
    previews = _read_previews(tmp_path)
    assert len(previews) == 1
    assert "8" in previews[0]["text"]
    assert "Hybrid" in previews[0]["text"]


@pytest.mark.parametrize("price_id,expected_max", [(LOCAL_9_16, 16), (HYBRID_17_32, 32)])
def test_higher_tiers_reflect_the_correct_camera_capacity_in_the_email(db_path, tmp_path, _tier_map, price_id, expected_max):
    _seed_customer(db_path)
    event = _checkout_event(f"evt_{expected_max}", price_id, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)
        pn.notify_from_stripe_event(event)
    previews = _read_previews(tmp_path)
    assert len(previews) == 1
    assert str(expected_max) in previews[0]["text"]


# ----------------------------------------------------------------- exclusions


def test_dollar_test_product_grants_zero_slots_and_sends_no_ready_email(db_path, tmp_path):
    _seed_customer(db_path)
    event = _checkout_event("evt_dollar", DOLLAR_TEST_PRICE, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)
        result = pn.notify_from_stripe_event(event)
        total = ce.total_camera_slots("cust-1")
    assert total == 0
    assert result["status"] == "ignored"
    assert _read_previews(tmp_path) == []


def test_hardware_only_purchase_sends_order_confirmation_never_a_camera_slot_email(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    event = _checkout_event("evt_hw_only", RYZEN_STARTER_PRICE, customer_id="cust-1", mode="payment")
    with override_target(sqlite_path=db_path):
        ho.sync_hardware_order_from_stripe_event(event)
        result = pn.notify_from_stripe_event(event)
    assert result["status"] == "sent"
    previews = _read_previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "hardware_order_confirmation"
    assert "Ryzen Starter" in previews[0]["text"]
    assert "camera" not in previews[0]["text"].lower() or "does not include any camera-slot" in previews[0]["text"]


# --------------------------------------------------------------- idempotency


def test_duplicate_webhook_delivery_never_sends_a_duplicate_email(db_path, tmp_path, _tier_map):
    _seed_customer(db_path)
    event = _checkout_event("evt_replay", LOCAL_1_8, customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)
        first = pn.notify_from_stripe_event(event)
        # Simulate Stripe redelivering the SAME event (entitlement sync
        # itself short-circuits to already_processed on this second call,
        # exactly as it does live).
        ce.sync_entitlement_from_stripe_event(event)
        second = pn.notify_from_stripe_event(event)
    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert len(_read_previews(tmp_path)) == 1


# ------------------------------------------------------ pending/new customer


def test_pending_customer_gets_setup_email_then_ready_email_only_after_registration(db_path, tmp_path, _tier_map):
    # 1) Checkout completes before any customer account exists.
    event = _checkout_event("evt_pending", LOCAL_1_8, email="new-buyer@example.test", customer_id=None)
    with override_target(sqlite_path=db_path):
        sync_result = ce.sync_entitlement_from_stripe_event(event)
        assert sync_result["status"] == "pending_link_created"
        setup_result = pn.notify_from_stripe_event(event)
    assert setup_result["status"] == "sent"
    previews = _read_previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "setup_required"

    # 2) Customer completes registration; the pending purchase resolves.
    _seed_customer(db_path, customer_id="cust-new", email="new-buyer@example.test", name="Nia Buyer")
    with override_target(sqlite_path=db_path):
        ce.resolve_pending_links_for_customer("cust-new", "new-buyer@example.test")
        ready_results = pn.notify_registration_resolved("cust-new")

    assert len(ready_results) == 1
    assert ready_results[0]["status"] == "sent"
    previews_after = _read_previews(tmp_path)
    assert len(previews_after) == 2  # the earlier setup_required + this new account_ready
    types = sorted(p["type"] for p in previews_after)
    assert types == ["account_ready", "setup_required"]

    # 3) Calling registration-resolved notification again (e.g. a retried
    # approval request) must not double-send the ready email.
    with override_target(sqlite_path=db_path):
        again = pn.notify_registration_resolved("cust-new")
    assert again[0]["status"] == "skipped"
    assert len(_read_previews(tmp_path)) == 2


# ------------------------------------------------------------------ cancellation


def test_cancellation_zeroes_slots_preserves_installation_and_sends_one_notification(db_path, tmp_path, _tier_map):
    _seed_customer(db_path)
    _seed_appliance(db_path)
    checkout_event = _checkout_event("evt_before_cancel", LOCAL_1_8, customer_id="cust-1", stripe_customer="cus_cancel")
    cancel_event = _subscription_event("evt_cancel", LOCAL_1_8, stripe_customer="cus_cancel", event_type="customer.subscription.deleted", status="canceled")

    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(checkout_event)
        pn.notify_from_stripe_event(checkout_event)  # the initial "ready" email, not under test here
        ce.sync_entitlement_from_stripe_event(cancel_event)
        result = pn.notify_from_stripe_event(cancel_event)

        total = ce.total_camera_slots("cust-1")
        conn = sqlite3.connect(db_path)
        customer_row = conn.execute("SELECT status FROM customers WHERE id='cust-1'").fetchone()
        appliance_row = conn.execute("SELECT cloud_id,activation_status FROM appliances WHERE id='appl-1'").fetchone()

    assert total == 0
    assert customer_row is not None
    assert appliance_row == ("AIC-NOTIFY1", "activated")
    assert result["status"] == "sent"
    previews = _read_previews(tmp_path)
    cancellation_emails = [p for p in previews if p["type"] == "plan_cancelled"]
    assert len(cancellation_emails) == 1

    # Redelivering the cancellation event must not send a second one.
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(cancel_event)
        again = pn.notify_from_stripe_event(cancel_event)
    assert again["status"] == "skipped"
    assert len([p for p in _read_previews(tmp_path) if p["type"] == "plan_cancelled"]) == 1


def test_upgrade_sends_a_plan_updated_email_not_a_second_ready_email(db_path, tmp_path, _tier_map):
    _seed_customer(db_path)
    checkout_event = _checkout_event("evt_before_upgrade", LOCAL_1_8, customer_id="cust-1", stripe_customer="cus_up")
    upgrade_event = _subscription_event("evt_upgrade", LOCAL_9_16, stripe_customer="cus_up", event_type="customer.subscription.updated", status="active")
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(checkout_event)
        pn.notify_from_stripe_event(checkout_event)
        ce.sync_entitlement_from_stripe_event(upgrade_event)
        result = pn.notify_from_stripe_event(upgrade_event)
    assert result["status"] == "sent"
    previews = _read_previews(tmp_path)
    by_type = {p["type"]: p for p in previews}
    assert set(by_type) == {"account_ready", "plan_updated"}
    assert "16" in by_type["plan_updated"]["text"]


# --------------------------------------------------------- delivery failure


def test_email_delivery_failure_never_affects_the_entitlement_and_is_retried_on_redelivery(db_path, tmp_path, _tier_map, monkeypatch):
    _seed_customer(db_path)
    event = _checkout_event("evt_flaky_email", LOCAL_1_8, customer_id="cust-1")

    class _FailingBackend:
        def send(self, *a, **k):
            raise RuntimeError("SMTP connection refused (simulated)")

    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)

        monkeypatch.setattr(pn, "get_email_service", lambda: _FailingBackend())
        first = pn.notify_from_stripe_event(event)
        total_after_failure = ce.total_camera_slots("cust-1")

    assert first["status"] == "failed"
    assert total_after_failure == 8  # entitlement is correct regardless of email outcome
    assert _read_previews(tmp_path) == []

    # Redelivery of the SAME event (Stripe retries because the webhook
    # route itself still returns 200 -- email failure is swallowed at the
    # main.py call site, never surfaced to Stripe) must retry the send,
    # now that the backend is healthy again.
    monkeypatch.undo()
    patched = dataclasses.replace(email_service.settings, email_backend="preview", email_preview_dir=str(tmp_path / "email-preview"))
    monkeypatch.setattr(email_service, "settings", patched)
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(event)  # already_processed, as live
        second = pn.notify_from_stripe_event(event)
    assert second["status"] == "sent"
    assert len(_read_previews(tmp_path)) == 1


def test_no_notification_test_creates_a_real_stripe_charge_or_sends_real_email():
    import inspect
    source = inspect.getsource(pn)
    assert "api.stripe.com" not in source
    assert "smtplib" not in source
