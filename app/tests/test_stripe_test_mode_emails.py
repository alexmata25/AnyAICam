"""Stripe TEST-mode purchases are unmistakable (2026-09-28). A sandbox
hardware order produced a normal-looking "order confirmed" email. Test
mode is decided from trusted server-side Stripe state only -- the signed
event's livemode, Stripe's own cs_test_/cs_live_ ids, the configured key
-- never from anything the browser supplies. Live wording is unchanged,
and a test order can never enter live fulfillment."""
import sqlite3

import pytest

import hardware_fulfillment as hf
import hardware_orders as ho
import purchase_notifications as pn
import stripe_mode
from database_backend import override_target
from test_purchase_notifications import (  # noqa: F401  (fixtures)
    RYZEN_STARTER_PRICE, _db, _hardware_map, _preview_email_dir, _read_previews, _seed_customer, db_path,
)

LIVE_SUBJECT = "Your AnyAiCam hardware order is confirmed"


def _event(event_id, *, livemode, session_prefix, metadata_extra=None):
    metadata = {"anyaicam_stripe_price_id": RYZEN_STARTER_PRICE, "anyaicam_customer_id": "cust-1"}
    metadata.update(metadata_extra or {})
    event = {"id": event_id, "type": "checkout.session.completed", "livemode": livemode,
             "data": {"object": {"id": f"{session_prefix}{event_id}", "mode": "payment", "customer": "cus_1",
                                 "customer_details": {"email": "jane@example.test"}, "payment_status": "paid",
                                 "livemode": livemode, "metadata": metadata}}}
    return event


def _purchase(db_path, event):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        ho.sync_hardware_order_from_stripe_event(event)
        return pn.notify_from_stripe_event(event)


@pytest.fixture()
def live_key(monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_SECRET_KEY", "sk_live_" + "x" * 24)


@pytest.fixture()
def test_key(monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_SECRET_KEY", "sk_test_" + "x" * 24)


# ---------------------------------------------------------------- the decision itself

@pytest.mark.parametrize("event,order,key,expected", [
    ({"livemode": False}, None, "sk_live_x", True),              # signed event says test
    ({"livemode": True}, {"stripe_checkout_session_id": "cs_live_1"}, "sk_live_x", False),
    (None, {"stripe_checkout_session_id": "cs_test_1"}, "sk_live_x", True),   # Stripe's own id says test
    (None, {"stripe_checkout_session_id": "cs_live_1"}, "sk_live_x", False),
    (None, {"stripe_checkout_session_id": None}, "sk_test_x", True),          # test-key server
    (None, {"stripe_checkout_session_id": None}, "sk_live_x", False),
])
def test_test_mode_comes_only_from_trusted_stripe_state(monkeypatch, event, order, key, expected):
    monkeypatch.setenv("ANYAICAM_STRIPE_SECRET_KEY", key)
    assert stripe_mode.is_test_mode(event=event, order=order) is expected


# ---------------------------------------------------------------- order confirmation email

def test_test_mode_order_email_has_test_subject_and_banner(db_path, tmp_path, _hardware_map, live_key):
    assert _purchase(db_path, _event("evt_t1", livemode=False, session_prefix="cs_test_"))["status"] == "sent"
    email = _read_previews(tmp_path)[0]
    assert email["subject"] == "[TEST] " + LIVE_SUBJECT
    assert email["text"].startswith("*** TEST MODE — NO REAL CHARGE")
    assert "TEST MODE — NO REAL CHARGE" in email["html"]
    assert email["html"].index("TEST MODE") < email["html"].index("AnyAiCam Starter")  # banner is on top


def test_live_order_email_is_unchanged(db_path, tmp_path, _hardware_map, live_key):
    assert _purchase(db_path, _event("evt_l1", livemode=True, session_prefix="cs_live_"))["status"] == "sent"
    email = _read_previews(tmp_path)[0]
    assert email["subject"] == LIVE_SUBJECT
    assert "TEST MODE" not in email["text"] and "TEST MODE" not in (email.get("html") or "")


def test_browser_supplied_metadata_cannot_mark_a_live_order_as_test_or_vice_versa(db_path, tmp_path, _hardware_map, live_key):
    live = _event("evt_l2", livemode=True, session_prefix="cs_live_", metadata_extra={"livemode": "false", "test_mode": "true"})
    _purchase(db_path, live)
    assert _read_previews(tmp_path)[0]["subject"] == LIVE_SUBJECT


def test_a_test_key_server_marks_every_purchase_email_as_test(db_path, tmp_path, _hardware_map, test_key):
    event = _event("evt_t2", livemode=None, session_prefix="cs_")  # no livemode, no recognisable id
    event.pop("livemode")
    _purchase(db_path, event)
    assert _read_previews(tmp_path)[0]["subject"].startswith("[TEST] ")


# ---------------------------------------------------------------- admin-triggered order emails and fulfillment

def _seed_order(db_path, session_id):
    _seed_customer(db_path)
    with override_target(sqlite_path=db_path):
        return ho.upsert_order(customer_id="cust-1", sku="AIC-RELAY-NUMATO-3CH", product_name="Numato 3-Channel Relay Module",
                               stripe_price_id="price_x", amount_cents=14999, stripe_checkout_session_id=session_id, status="paid")


def test_shipping_email_for_a_test_order_is_marked_test(db_path, tmp_path, test_key):
    order = _seed_order(db_path, "cs_test_ship1")
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z999")
    assert _read_previews(tmp_path)[-1]["subject"].startswith("[TEST] ")


def test_a_test_order_cannot_enter_live_fulfillment(db_path, tmp_path, live_key):
    order = _seed_order(db_path, "cs_test_live_server")
    with override_target(sqlite_path=db_path):
        for action in (lambda: hf.advance_fulfillment_status(order["id"], "preparing"),
                       lambda: hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1"),
                       lambda: hf.mark_cancelled(order["id"])):
            with pytest.raises(ValueError, match="test-mode order"):
                action()
        con = sqlite3.connect(db_path)
        assert con.execute("SELECT fulfillment_status, shipped_at FROM hardware_orders WHERE id=?", (order["id"],)).fetchone() == ("paid", None)
    assert _read_previews(tmp_path) == []  # no shipping/cancellation email either


def test_live_orders_still_move_through_live_fulfillment(db_path, tmp_path, live_key):
    order = _seed_order(db_path, "cs_live_real1")
    with override_target(sqlite_path=db_path):
        assert hf.advance_fulfillment_status(order["id"], "preparing")["fulfillment_status"] == "preparing"
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z2")
    subject = _read_previews(tmp_path)[-1]["subject"]
    assert not subject.startswith("[TEST]")


def test_staging_test_key_keeps_the_fulfillment_workflow_testable(db_path, test_key):
    order = _seed_order(db_path, "cs_test_staging1")
    with override_target(sqlite_path=db_path):
        assert hf.advance_fulfillment_status(order["id"], "preparing")["fulfillment_status"] == "preparing"
