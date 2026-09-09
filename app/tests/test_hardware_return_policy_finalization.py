"""Provisioning Phase 8 follow-up: the approved business values --
HARDWARE_PREPARATION_MAX_DAYS=14 (already shipped), ANYAICAM_HARDWARE_
RESTOCKING_PERCENT=15, ANYAICAM_HARDWARE_RETURN_WINDOW_DAYS=30 -- plus
the defective/damaged-on-arrival fee-waiver exception, 30-day return-
window enforcement, and wiring the Getting Started email to the
customer's own appliance-claim action instead of hardware-payment
success. STAGING ONLY: deploy/.env.staging.example is the only place
these values are set; production is untouched by this file.
"""
import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

import email_service
import hardware_orders as ho
import hardware_fulfillment as hf
import hardware_returns as hr
import purchase_notifications as pn


RYZEN_STARTER_PRICE = "price_finalize_ryzen_starter"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_hardware_return_policy_finalization.db"


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
        RYZEN_STARTER_PRICE: {"sku": "AIC-APPLIANCE-RYZEN-STARTER", "product": "ryzen_starter", "name": "AnyAiCam Ryzen Starter Appliance", "amount_cents": 124999},
    })


@pytest.fixture()
def _approved_values(monkeypatch):
    """The values approved this pass -- set exactly as deploy/.env.
    staging.example now documents them, staging-only."""
    monkeypatch.setenv("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "15")
    monkeypatch.setenv("ANYAICAM_HARDWARE_RETURN_WINDOW_DAYS", "30")


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


def _place_and_ship_order(db_path, *, event_id="evt_hw", delivered_days_ago=None, shipped_days_ago=None):
    with override_target(sqlite_path=db_path):
        event = _hardware_checkout_event(event_id, RYZEN_STARTER_PRICE)
        ho.sync_hardware_order_from_stripe_event(event)
        order = ho.get_orders_for_customer("cust-1")[0]
        hf.mark_shipped(order["id"], carrier="UPS", tracking_number="1Z1")
        if shipped_days_ago is not None or delivered_days_ago is not None:
            conn = sqlite3.connect(db_path)
            if shipped_days_ago is not None:
                conn.execute("UPDATE hardware_orders SET shipped_at=? WHERE id=?", ((datetime.now() - timedelta(days=shipped_days_ago)).isoformat(), order["id"]))
            if delivered_days_ago is not None:
                conn.execute("UPDATE hardware_orders SET delivered_at=? WHERE id=?", ((datetime.now() - timedelta(days=delivered_days_ago)).isoformat(), order["id"]))
            conn.commit()
        order = ho.get_orders_for_customer("cust-1")[0]
    return order


def _previews(tmp_path):
    preview_dir = tmp_path / "email-preview"
    if not preview_dir.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(preview_dir.glob("*.json"))]


# ============================================================ configured values


def test_the_approved_15_percent_restocking_fee_is_configured(_approved_values):
    assert hr.restocking_fee_percent() == 15.0


def test_the_approved_30_day_return_window_is_configured(_approved_values):
    assert hr.return_window_days() == 30


def test_preparation_period_remains_the_previously_approved_14_days():
    assert hf.HARDWARE_PREPARATION_MAX_DAYS == 14
    assert "up to 14 days" in hf.PREPARATION_TIMEFRAME_TEXT


# ============================================================ refund math


def test_refund_math_matches_the_approved_worked_example(_approved_values):
    result = hr.calculate_refund(124999)  # $1,249.99
    assert result["restocking_fee_cents"] == 18750  # $187.50
    assert result["final_refund_cents"] == 106249  # $1,062.49
    assert result["restocking_fee_cents"] + result["final_refund_cents"] == 124999  # currency-safe: no cents lost or invented


@pytest.mark.parametrize("amount_cents", [100, 999, 1, 124999, 174999, 224999, 14999, 33333])
def test_refund_math_is_currency_safe_for_a_range_of_amounts(_approved_values, amount_cents):
    result = hr.calculate_refund(amount_cents)
    assert result["restocking_fee_cents"] + result["final_refund_cents"] == amount_cents
    assert result["restocking_fee_cents"] >= 0
    assert result["final_refund_cents"] >= 0


# ============================================================ defective/damaged exception


def test_defective_or_damaged_on_arrival_waives_the_restocking_fee_entirely(db_path, tmp_path, _hardware_map, _approved_values):
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-DEFECTIVE")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"], damage_notes="Arrived with cracked enclosure")
        inspected = hr.record_inspection_and_calculate_refund(
            hardware_return["id"], inspected_by="admin@anyaicam.com", defective_or_damaged_on_arrival=True,
        )
    assert bool(inspected["is_defective_or_damaged_on_arrival"]) is True
    assert inspected["restocking_fee_cents"] == 0

    with override_target(sqlite_path=db_path):
        preview = hr.preview_refund(hardware_return["id"])
        approved = hr.approve_refund(hardware_return["id"], approved_by="admin@anyaicam.com")
    assert preview["fee_waived"] is True
    assert preview["final_refund_cents"] == order["amount_cents"]  # full refund, fee waived
    assert approved["approved_refund_cents"] == order["amount_cents"]
    assert approved["restocking_fee_cents"] == 0


def test_normal_eligible_return_still_gets_the_restocking_fee_applied(db_path, tmp_path, _hardware_map, _approved_values):
    """The exception is opt-in and explicit -- a normal (non-defective)
    return still gets the approved 15% fee, proving the two code paths
    are genuinely distinct, not one silently disabling the other."""
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-NORMAL")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com")  # defective_or_damaged_on_arrival defaults False
        approved = hr.approve_refund(hardware_return["id"], approved_by="admin@anyaicam.com")
    assert approved["restocking_fee_cents"] == 18750
    assert approved["approved_refund_cents"] == 106249


def test_refund_processed_email_reflects_a_waived_fee_correctly(db_path, tmp_path, _hardware_map, _approved_values):
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-DEF-EMAIL")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com", defective_or_damaged_on_arrival=True)
        hr.approve_refund(hardware_return["id"], approved_by="admin@anyaicam.com")
        hr.finalize_refund(hardware_return["id"])
    refund_email = [p for p in _previews(tmp_path) if p["type"] == "refund_processed"][0]
    assert "$0.00" in refund_email["text"]  # restocking fee line reads $0.00
    assert "$1,249.99" in refund_email["text"]  # both original and final read the full amount


# ============================================================ 30-day return window


def test_return_request_allowed_well_within_the_30_day_window(db_path, tmp_path, _hardware_map, _approved_values):
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path, delivered_days_ago=10)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
    assert hardware_return["status"] == "return_requested"


def test_return_request_rejected_after_the_30_day_window_has_passed(db_path, tmp_path, _hardware_map, _approved_values):
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path, delivered_days_ago=45)
    with override_target(sqlite_path=db_path):
        with pytest.raises(ValueError, match="return window"):
            hr.request_return(order["id"])


def test_return_window_measures_from_delivery_not_shipment_when_both_known(db_path, tmp_path, _hardware_map, _approved_values):
    """Shipped 60 days ago but delivered only 5 days ago (e.g. a slow
    carrier) -- must use delivery, the customer's own real clock."""
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path, shipped_days_ago=60, delivered_days_ago=5)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
    assert hardware_return["status"] == "return_requested"


def test_without_the_window_configured_a_late_request_is_still_accepted(db_path, tmp_path, _hardware_map, monkeypatch):
    """Sanity check that the window enforcement is genuinely gated on
    configuration, not a hidden always-on 30-day default."""
    monkeypatch.delenv("ANYAICAM_HARDWARE_RETURN_WINDOW_DAYS", raising=False)
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path, delivered_days_ago=200)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
    assert hardware_return["status"] == "return_requested"


# ============================================================ admin approval still required


def test_admin_approval_still_required_even_with_a_real_percent_configured(db_path, tmp_path, _hardware_map, _approved_values):
    _seed_customer(db_path)
    order = _place_and_ship_order(db_path)
    with override_target(sqlite_path=db_path):
        hardware_return = hr.request_return(order["id"])
        hr.authorize_return(hardware_return["id"], return_reference="RMA-ADMIN")
        hr.mark_return_shipped_back(hardware_return["id"])
        hr.mark_return_received(hardware_return["id"])
        hr.record_inspection_and_calculate_refund(hardware_return["id"], inspected_by="admin@anyaicam.com")
        with pytest.raises(ValueError, match="explicit admin identity"):
            hr.approve_refund(hardware_return["id"], approved_by="")
        # Still not refunded/finalized without that approval.
        with pytest.raises(ValueError):
            hr.finalize_refund(hardware_return["id"])


# ============================================================ old live policy untouched


def test_the_live_shipping_returns_page_was_not_modified():
    """This session's work never touches the live Bluehost docroot --
    confirms the specific live policy page this task explicitly said not
    to edit still has its old (pre-Ryzen-appliance) content: the old
    7-day timeframe and the Videoloft/fulfillment-provider wording that
    the new staging drafts deliberately never mention."""
    import os
    live_path = r"C:\Users\Alejandro Mata\OneDrive\Documents\anyaicam\shipping-returns.html"
    if not os.path.exists(live_path):
        pytest.skip("live docroot mirror not present in this environment")
    content = open(live_path, encoding="utf-8").read()
    assert "no more than 7 calendar days" in content  # old timeframe, unchanged
    assert "Videoloft" in content  # old supplier wording, unchanged -- confirms untouched, not that this is acceptable wording for the NEW policy


# ============================================================ Getting Started trigger


def test_getting_started_email_does_not_fire_merely_because_hardware_payment_succeeded(db_path, tmp_path, _hardware_map):
    _seed_customer(db_path)
    event = _hardware_checkout_event("evt_hw_no_getting_started", RYZEN_STARTER_PRICE)
    with override_target(sqlite_path=db_path):
        ho.sync_hardware_order_from_stripe_event(event)
        pn.notify_from_stripe_event(event)
    previews = _previews(tmp_path)
    assert any(p["type"] == "hardware_order_confirmation" for p in previews)
    assert not any(p["type"] == "getting_started" for p in previews)


def test_getting_started_email_fires_once_after_a_real_appliance_claim(db_path, tmp_path, monkeypatch):
    """Real route call (not a direct function-call shortcut): POST
    /api/customer/appliances/link, the customer's own browser-facing
    'claim my appliance' action -- the approved trigger."""
    import main
    import partner_workspace
    from provisioning_service import MockProvisioningBackend, reset_provisioning_backend_for_tests
    import provisioning_service

    def _route(path, method="GET"):
        for r in main.app.routes:
            if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
                return r.endpoint
        raise AssertionError(f"no route registered for {method} {path}")

    def _fake_request():
        from types import SimpleNamespace
        return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))

    reset_provisioning_backend_for_tests()
    backend = MockProvisioningBackend(path=tmp_path / "mock_aws_provisioning.json")
    monkeypatch.setattr(provisioning_service, "_backend_instance", backend)

    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-getting-started")

    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Partner','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Jane Doe','jane@example.test','active','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Site','2026-01-01')")
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
            ("appl-1", "cust-1", "site-1", provisioned["cloud_id"], "pending", "2026-01-01"),
        )
        conn.commit()

        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "jane@example.test"})
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
    assert result["message"] == "Appliance linked to customer account."

    previews = _previews(tmp_path)
    getting_started = [p for p in previews if p["type"] == "getting_started"]
    assert len(getting_started) == 1

    # Linking a SECOND appliance for the same customer must not send a second one.
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        provisioned_2 = backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-getting-started-2")
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
            ("appl-2", "cust-1", "site-1", provisioned_2["cloud_id"], "pending", "2026-01-01"),
        )
        conn.commit()
        link_appliance(_fake_request(), {"cloud_id": provisioned_2["cloud_id"], "activation_token": provisioned_2["activation_token"]})
    assert len([p for p in _previews(tmp_path) if p["type"] == "getting_started"]) == 1

    reset_provisioning_backend_for_tests()
