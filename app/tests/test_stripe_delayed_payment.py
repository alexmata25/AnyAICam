"""Delayed-payment Checkout Sessions (2026-09-25, stripe_checkout_payment.py).

With a delayed payment method, checkout.session.completed arrives with
payment_status "unpaid" before any money has moved; the outcome comes later
as checkout.session.async_payment_succeeded / ..._failed. Before this fix
camera slots and analytics were granted, and a hardware order was created
straight into the fulfillment queue (fulfillment_status "paid"), on the
first event -- with nothing to undo it if the payment then failed."""
import sqlite3

import pytest

import analytics_entitlements as ae
import customer_entitlements as ce
import hardware_orders as ho
from database_backend import override_target
from partner_db import initialize_database

SLOT_PRICE = "price_test_camera_slots_local_8"
ANALYTICS_PRICE = "price_test_advanced_analytics"
HARDWARE_PRICE = "price_test_ryzen_starter"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_stripe_delayed_payment.db"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {SLOT_PRICE: {"product": "camera_slots_local", "camera_slot_maximum": 8, "billing_type": "one_time"}})
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", {ANALYTICS_PRICE: {
        "addon_key": "advanced_analytics", "label": "Advanced Analytics", "analytic_keys": ("smart_motion", "people_counting", "lpr", "ppe")}})
    monkeypatch.setattr(ho, "HARDWARE_PRICE_MAP", {HARDWARE_PRICE: {
        "sku": "AIC-APPLIANCE-RYZEN-STARTER", "product": "ryzen_starter", "name": "AnyAiCam Starter", "amount_cents": 124999}})
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Partner','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
                     "VALUES('cust-1','partner-1','Customer','buyer@example.test','active','2026-01-01')")
        conn.commit()
        conn.close()
        yield


def _session_event(event_id, event_type, price_id, *, payment_status, customer_id="cust-1", email="buyer@example.test", session_id="cs_delayed_1"):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {"id": event_id, "type": event_type, "data": {"object": {
        "id": session_id, "mode": "payment", "customer": "cus_1", "payment_status": payment_status,
        "customer_details": {"email": email}, "metadata": metadata}}}


def _completed_unpaid(price_id, **kwargs):
    return _session_event("evt_completed", "checkout.session.completed", price_id, payment_status="unpaid", **kwargs)


def _async_succeeded(price_id, **kwargs):
    return _session_event("evt_async_ok", "checkout.session.async_payment_succeeded", price_id, payment_status="paid", **kwargs)


def test_camera_slots_are_granted_only_once_a_delayed_payment_succeeds():
    assert ce.sync_entitlement_from_stripe_event(_completed_unpaid(SLOT_PRICE))["status"] == "awaiting_payment"
    assert ce.total_camera_slots("cust-1") == 0
    assert ce.sync_entitlement_from_stripe_event(_async_succeeded(SLOT_PRICE))["status"] == "entitlement_updated"
    assert ce.total_camera_slots("cust-1") == 8
    assert ce.sync_entitlement_from_stripe_event(_async_succeeded(SLOT_PRICE))["status"] == "already_processed"  # Stripe retry


def test_analytics_are_granted_only_once_a_delayed_payment_succeeds():
    assert ae.sync_analytics_from_stripe_event(_completed_unpaid(ANALYTICS_PRICE))["status"] == "awaiting_payment"
    assert ae.get_active_analytics_for_customer("cust-1") == []
    ae.sync_analytics_from_stripe_event(_async_succeeded(ANALYTICS_PRICE))
    assert ae.get_active_analytics_for_customer("cust-1") == ["lpr", "people_counting", "ppe", "smart_motion"]


def test_an_unpaid_hardware_order_never_enters_the_fulfillment_queue():
    assert ho.sync_hardware_order_from_stripe_event(_completed_unpaid(HARDWARE_PRICE))["status"] == "awaiting_payment"
    assert ho.get_orders_for_customer("cust-1") == []
    result = ho.sync_hardware_order_from_stripe_event(_async_succeeded(HARDWARE_PRICE))
    assert result["status"] == "order_recorded"
    orders = ho.get_orders_for_customer("cust-1")
    assert len(orders) == 1 and orders[0]["status"] == "paid"


def test_a_failed_delayed_payment_leaves_nothing_granted_or_ordered():
    ce.sync_entitlement_from_stripe_event(_completed_unpaid(SLOT_PRICE))
    ae.sync_analytics_from_stripe_event(_completed_unpaid(ANALYTICS_PRICE))
    ho.sync_hardware_order_from_stripe_event(_completed_unpaid(HARDWARE_PRICE, session_id="cs_hw"))
    failed = _session_event("evt_async_failed", "checkout.session.async_payment_failed", SLOT_PRICE, payment_status="unpaid")
    for sync in (ce.sync_entitlement_from_stripe_event, ae.sync_analytics_from_stripe_event, ho.sync_hardware_order_from_stripe_event):
        assert sync(failed)["status"] == "ignored"
    assert ce.total_camera_slots("cust-1") == 0 and ae.get_active_analytics_for_customer("cust-1") == [] and ho.get_orders_for_customer("cust-1") == []


def test_an_unpaid_checkout_before_registration_creates_no_pending_link():
    """A pending link is claimed at registration as if paid -- so none may
    exist for a session whose payment has not settled."""
    unpaid = _completed_unpaid(SLOT_PRICE, customer_id=None, email="not-yet-registered@example.test")
    assert ce.sync_entitlement_from_stripe_event(unpaid)["status"] == "awaiting_payment"
    from partner_db import rows
    assert rows("SELECT id FROM pending_customer_links WHERE normalized_email='not-yet-registered@example.test'") == []


@pytest.mark.parametrize("payment_status", ["paid", "no_payment_required", None])
def test_paid_free_and_older_payloads_still_grant_immediately(payment_status):
    event = _session_event("evt_now", "checkout.session.completed", SLOT_PRICE, payment_status=payment_status)
    if payment_status is None:
        del event["data"]["object"]["payment_status"]
    assert ce.sync_entitlement_from_stripe_event(event)["status"] == "entitlement_updated"
    assert ce.total_camera_slots("cust-1") == 8


def test_the_webhook_endpoint_runs_the_async_success_event_through_every_step(monkeypatch, tmp_path):
    """End to end through the real route: signature, dedup, every
    provisioning step, and a 200 so Stripe does not retry."""
    import hashlib
    import hmac
    import json
    import time

    from fastapi.testclient import TestClient

    import main
    monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", "whsec_test_delayed")
    for name in ("PAYMENT_WEBHOOK_EVENTS_FILE", "PAYMENT_SESSIONS_FILE", "BILLING_ACCOUNTS_FILE"):
        monkeypatch.setattr(main, name, tmp_path / f"{name.lower()}.json")  # never the shared recordings folder
    body = json.dumps(_async_succeeded(SLOT_PRICE)).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"whsec_test_delayed", f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    with TestClient(main.app) as client:
        response = client.post("/api/payments/stripe/webhook", content=body,
                               headers={"stripe-signature": f"t={stamp},v1={signature}", "Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    assert ce.total_camera_slots("cust-1") == 8
