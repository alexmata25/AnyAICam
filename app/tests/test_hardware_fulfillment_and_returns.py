"""Provisioning Phase 8: hardware order fulfillment lifecycle (paid ->
... -> shipped -> delivered / cancelled) and the separate return/refund
workflow (return_requested -> ... -> refunded), plus the restocking-fee
calculator. Every email in this file goes through PreviewEmail (JSON
file, never SMTP/never a real send) -- see test_purchase_notifications.py
for the same convention.
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
import hardware_fulfillment as hf
import hardware_returns as hr
import purchase_notifications as pn


RYZEN_STARTER_PRICE = "price_fulfillment_ryzen_starter"
LOCAL_1_8 = "price_fulfillment_local_1_8"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_hardware_fulfillment_and_returns.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        yield


@pytest.fixture(autouse=True)
def _preview_email_dir(tmp_path, monkeypatch):
    patched = dataclasses.replace(email_service.settings, email_backend="preview", email_preview_dir=str(tmp_path / "email-preview"))
    monkeypatch.setattr(email_service, "settings", patched)


@pytest.fixture()
def _hardware_map(monkeypatch):
    monkeypatch.setattr(ho, "HARDWARE_PRICE_MAP", {
        RYZEN_STARTER_PRICE: {"sku": "AIC-APPLIANCE-RYZEN-STARTER", "product": "ryzen_starter", "name": "AnyAiCam Starter", "amount_cents": 124999},
    })


@pytest.fixture()
def _tier_map(monkeypatch):
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
        LOCAL_1_8: {"product": "camera_slots_local", "camera_slot_maximum": 8},
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


def _hardware_checkout_event(event_id, price_id, *, email="jane@example.test", stripe_customer="cus_1", customer_id="cust-1"):
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_{event_id}", "mode": "payment", "customer": stripe_customer,
            "customer_details": {"email": email}, "payment_status": "paid",
            "metadata": {"anyaicam_stripe_price_id": price_id, "anyaicam_customer_id": customer_id},
        }},
    }


def _place_hardware_order(db_path, event_id="evt_hw", **kwargs):
    with override_target(sqlite_path=db_path):
        event = _hardware_checkout_event(event_id, RYZEN_STARTER_PRICE, **kwargs)
        ho.sync_hardware_order_from_stripe_event(event)
        order = ho.get_orders_for_customer(kwargs.get("customer_id", "cust-1"))[0]
        pn.notify_from_stripe_event(event)
    return order


def _previews(tmp_path):
    preview_dir = tmp_path / "email-preview"
    if not preview_dir.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(preview_dir.glob("*.json"))]


# ============================================================ order confirmation


def test_successful_hardware_order_sends_exactly_one_confirmation_email(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    _place_hardware_order(db_path)
    previews = _previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "hardware_order_confirmation"
    assert previews[0]["subject"] == "Your AnyAiCam hardware order is confirmed"


def test_order_confirmation_states_up_to_14_days_preparation(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    _place_hardware_order(db_path)
    text = _previews(tmp_path)[0]["text"]
    assert "up to 14 days for preparation and shipment" in text
    assert hf.HARDWARE_PREPARATION_MAX_DAYS == 14


def test_order_confirmation_never_claims_active_camera_slots(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    _place_hardware_order(db_path)
    text = _previews(tmp_path)[0]["text"]
    assert "does not activate camera slots" in text


def test_duplicate_hardware_checkout_event_never_sends_a_duplicate_order_email(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    event = _hardware_checkout_event("evt_dup", RYZEN_STARTER_PRICE)
    with override_target(sqlite_path=db_path):
        ho.sync_hardware_order_from_stripe_event(event)
        pn.notify_from_stripe_event(event)
        ho.sync_hardware_order_from_stripe_event(event)  # replayed
        pn.notify_from_stripe_event(event)
    assert len(_previews(tmp_path)) == 1


# ============================================================ shipping


def test_shipped_order_sends_shipping_email_with_tracking(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z999AA10123456784", tracking_link="https://ups.example/track/1Z999AA10123456784")
        updated = ho.get_orders_for_customer("cust-1")[0]
    assert updated["fulfillment_status"] == "shipped"
    assert updated["carrier"] == "UPS"
    previews = _previews(tmp_path)
    shipped = [p for p in previews if p["type"] == "hardware_shipped"]
    assert len(shipped) == 1
    assert "1Z999AA10123456784" in shipped[0]["text"]
    assert "UPS" in shipped[0]["text"]
    assert "sign in to your AnyAiCam account to complete setup and connect your cameras" in shipped[0]["text"]


# ============================================================ cancellation vs return


def test_unshipped_order_can_be_cancelled_directly(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_cancelled(order["id"])
        updated = ho.get_orders_for_customer("cust-1")[0]
    assert updated["fulfillment_status"] == "cancelled"
    assert updated["cancelled_at"] is not None
    previews = _previews(tmp_path)
    cancellation = [p for p in previews if p["type"] == "hardware_cancellation"]
    assert len(cancellation) == 1
    assert "had not yet shipped" in cancellation[0]["text"]


def test_shipped_order_cannot_be_cancelled_directly_must_use_return_process(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        with pytest.raises(ValueError, match="Cannot cancel"):
            hf.mark_cancelled(order["id"])
        # The correct path for a shipped order:
        hardware_return = hr.request_return(order["id"])
    assert hardware_return["status"] == "return_requested"


def test_return_cannot_be_requested_for_an_order_that_never_shipped(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        with pytest.raises(ValueError, match="has not shipped"):
            hr.request_return(order["id"])


# ============================================================ return / inspection


def test_return_received_moves_to_inspection_state(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-0001")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"], condition_notes="Good", accessories_included="Power adapter")
        inspected = hr.record_inspection_and_calculate_refund(hardware_return["id"], serial_number="SN-123", inspected_by="admin@anyaicam.com")
    assert inspected["status"] == "inspection"
    assert inspected["serial_number"] == "SN-123"

    previews = _previews(tmp_path)
    assert any(p["type"] == "return_authorized" for p in previews)
    assert any(p["type"] == "return_received" for p in previews)
    received_email = [p for p in previews if p["type"] == "return_received"][0]
    assert "final refund amount will be confirmed" in received_email["text"].lower()


def test_refund_cannot_be_issued_merely_because_a_return_was_requested(db_path, tmp_path, _hardware_map):
    """Support inspection first -- the state machine itself refuses to
    jump straight from return_requested to refunded."""
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        with pytest.raises(ValueError):
            hr._transition(hardware_return["id"], "refunded")
        with pytest.raises(ValueError):
            hr.finalize_refund(hardware_return["id"])


# ============================================================ restocking fee / refund calculation


def test_refund_calculation_with_no_restocking_percent_configured_is_a_full_refund_flagged_unconfigured(monkeypatch):
    monkeypatch.delenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", raising=False)
    result = hr.calculate_refund(124999)
    assert result["configured"] is False
    assert result["restocking_fee_cents"] == 0
    assert result["final_refund_cents"] == 124999


def test_refund_calculation_correctly_applies_a_configured_restocking_percent(monkeypatch):
    monkeypatch.setenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "15")
    result = hr.calculate_refund(124999)
    assert result["configured"] is True
    assert result["restocking_fee_percent"] == 15.0
    assert result["restocking_fee_cents"] == round(124999 * 0.15)
    assert result["final_refund_cents"] == 124999 - round(124999 * 0.15)
    assert result["final_refund_cents"] + result["restocking_fee_cents"] == 124999


def test_restocking_fee_can_never_exceed_the_original_amount(monkeypatch):
    monkeypatch.setenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "100")
    result = hr.calculate_refund(124999)
    assert result["final_refund_cents"] == 0
    assert result["restocking_fee_cents"] == 124999


def test_preview_refund_matches_what_approve_refund_would_record(db_path, tmp_path, _hardware_map, monkeypatch):
    monkeypatch.setenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "15")
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-0002")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com")

        preview = hr.preview_refund(hardware_return["id"])
        approved = hr.approve_refund(hardware_return["id"], approved_by="admin@anyaicam.com")
    assert approved["approved_refund_cents"] == preview["final_refund_cents"]
    assert approved["refund_status"] == "approved"
    assert approved["refund_approved_by"] == "admin@anyaicam.com"


def test_refund_requires_an_explicit_admin_identity(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-0003")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com")
        with pytest.raises(ValueError, match="explicit admin identity"):
            hr.approve_refund(hardware_return["id"], approved_by="")


def test_refund_processed_email_shows_original_fee_and_final_amount(db_path, tmp_path, _hardware_map, monkeypatch):
    monkeypatch.setenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "15")
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-0004")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com")
        hr.approve_refund(hardware_return["id"], approved_by="admin@anyaicam.com")
        hr.finalize_refund(hardware_return["id"])

    previews = _previews(tmp_path)
    refund_email = [p for p in previews if p["type"] == "refund_processed"][0]
    assert "$1,249.99" in refund_email["text"]  # original
    expected_fee = round(124999 * 0.15) / 100
    assert f"${expected_fee:,.2f}" in refund_email["text"]  # restocking fee
    expected_final = (124999 - round(124999 * 0.15)) / 100
    assert f"${expected_final:,.2f}" in refund_email["text"]  # final amount


def test_refund_cannot_be_finalized_before_being_approved(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    order = _place_hardware_order(db_path)
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        with pytest.raises(ValueError):
            hr.finalize_refund(hardware_return["id"])


# ============================================================ THE SEPARATION CONTRACT


def test_hardware_restocking_fee_never_touches_camera_slot_entitlement(db_path, tmp_path, _hardware_map, _tier_map, monkeypatch):
    monkeypatch.setenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "20")
    _seed_customer(db_path)
    # Customer has an ACTIVE Local 1-8 subscription entirely separate from the hardware purchase.
    with override_target(sqlite_path=db_path):
        ce.upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8, status="active")

    order = _place_hardware_order(db_path, event_id="evt_hw_sep")
    with override_target(sqlite_path=db_path):
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-SEP")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com")
        hr.approve_refund(hardware_return["id"], approved_by="admin@anyaicam.com")
        hr.finalize_refund(hardware_return["id"])
        total_slots = ce.total_camera_slots("cust-1")
    assert total_slots == 8  # completely unaffected by the hardware return/refund


def test_hardware_returns_module_never_imports_customer_entitlements():
    # Checks actual import statements and function calls, not the
    # module's own docstring (which mentions customer_entitlements.py
    # and camera_slot_quantity descriptively, explaining this exact
    # guarantee in prose).
    import inspect
    lines = inspect.getsource(hr).splitlines()
    import_lines = [ln.strip() for ln in lines if ln.strip().startswith(("import ", "from "))]
    assert not any("customer_entitlements" in ln for ln in import_lines)
    call_lines = [ln for ln in lines if "(" in ln and not ln.strip().startswith(("#", '"', "'"))]
    assert not any("total_camera_slots(" in ln or "upsert_entitlement(" in ln for ln in call_lines)


def test_subscription_cancellation_does_not_invoke_hardware_return_logic(db_path, tmp_path, _tier_map):
    """Cancelling a Local/Hybrid subscription must never touch
    hardware_orders/hardware_returns in any way -- it's a webhook-driven
    entitlement-status change, not a hardware order."""
    _seed_customer(db_path)
    checkout_event = {
        "id": "evt_sub_checkout", "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_sub", "mode": "subscription", "customer": "cus_sub", "customer_details": {"email": "jane@example.test"}, "payment_status": "paid", "metadata": {"anyaicam_stripe_price_id": LOCAL_1_8, "anyaicam_customer_id": "cust-1"}}},
    }
    cancel_event = {
        "id": "evt_sub_cancel", "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_1", "customer": "cus_sub", "status": "canceled", "items": {"data": [{"price": {"id": LOCAL_1_8}}]}, "metadata": {}}},
    }
    with override_target(sqlite_path=db_path):
        ce.sync_entitlement_from_stripe_event(checkout_event)
        ce.sync_entitlement_from_stripe_event(cancel_event)
        pn.notify_from_stripe_event(checkout_event)
        pn.notify_from_stripe_event(cancel_event)
        orders = ho.get_orders_for_customer("cust-1")
        returns_count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM hardware_returns").fetchone()[0]
        total_slots = ce.total_camera_slots("cust-1")
    assert orders == []  # no hardware order was ever created
    assert returns_count == 0  # no hardware return was ever created
    assert total_slots == 0  # subscription genuinely cancelled
    previews = _previews(tmp_path)
    assert any(p["type"] == "plan_cancelled" for p in previews)
    assert not any(p["type"] in ("hardware_cancellation", "return_authorized", "return_received", "refund_processed") for p in previews)


def test_hardware_returns_module_never_calls_stripe():
    import inspect
    source = inspect.getsource(hr)
    assert "api.stripe.com" not in source
    assert "stripe_api_post" not in source
