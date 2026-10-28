"""Safe entitlement recovery for the existing two-item AnyAiCam sandbox sub."""
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

import customer_entitlements as ce


SOFTWARE_PRICE = ce.OWNER_SANDBOX_VMS_SOFTWARE_PRICE_ID
LOCAL_PRICE = ce.OWNER_SANDBOX_LOCAL_CAMERA_PRICE_ID


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "owner_sandbox_entitlement_recovery.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Partner','2026-01-01')")
            db.execute(
                "INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                ("cust-owner", "partner-1", "Owner Test", "owner@example.test", "active", "2026-01-01"),
            )
            db.execute(
                "INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                ("cust-other", "partner-1", "Other Account", "other@example.test", "active", "2026-01-01"),
            )
        yield


@pytest.fixture()
def owner_stripe(monkeypatch):
    import main

    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_" + "test_unit_fixture")
    monkeypatch.setattr(main, "structured_log", lambda *args, **kwargs: None)
    subscription = {
        "id": "sub_owner_sandbox",
        "object": "subscription",
        "livemode": False,
        "customer": "cus_owner_sandbox",
        "status": "active",
        "latest_invoice": {"id": "in_owner_sandbox", "status": "paid", "amount_paid": 160},
        "metadata": {},
        "items": {"data": [
            {"id": "si_software", "quantity": 1, "price": {
                "id": SOFTWARE_PRICE, "unit_amount": 100, "currency": "usd",
                "recurring": {"interval": "month"},
            }},
            {"id": "si_camera", "quantity": 1, "price": {
                "id": LOCAL_PRICE, "unit_amount": 60, "currency": "usd",
                "recurring": {"interval": "month"},
            }},
        ]},
    }
    monkeypatch.setattr(ce, "current_subscription", lambda _subscription_id: dict(subscription))
    monkeypatch.setattr(ce, "subscription_payment_reversal", lambda _subscription: None)
    return main, subscription


@pytest.mark.parametrize("explicit_interval_count", [False, True])
def test_exact_monthly_integer_bundle_remains_valid(owner_stripe, explicit_interval_count):
    _, subscription = owner_stripe
    if explicit_interval_count:
        for item in subscription["items"]["data"]:
            item["price"]["recurring"]["interval_count"] = 1
    tiers = ce._owner_sandbox_bundle_tiers(subscription, event_livemode=False)
    assert tiers is not None
    assert {tier["product"] for tier in tiers} == {"vms_license", "camera_slots_local"}
    assert all(tier["camera_slot_maximum"] == 8 for tier in tiers)


def test_recovery_links_verified_sandbox_subscription_and_authorizes_eight_local_slots(db_path, owner_stripe):
    with override_target(sqlite_path=db_path):
        result = ce.reconcile_owner_sandbox_subscription(
            customer_id="cust-owner", subscription_id="sub_owner_sandbox", approved_by="admin-1",
        )
        entitlements = ce.get_entitlements_for_customer("cust-owner")
        capacity = ce.capacity_breakdown("cust-owner")
        usable = ce.usable_camera_capacity("cust-owner")
        mode = ce.product_mode_for_customer("cust-owner")

    assert result["status"] == "entitlement_updated"
    assert result["camera_capacity"] == 8
    assert {item["product"] for item in entitlements} == {"vms_license", "camera_slots_local"}
    assert all(item["stripe_subscription_id"] == "sub_owner_sandbox" for item in entitlements)
    assert mode == "local"
    assert usable == 8  # enough subscribed/licensed capacity for the owner's four-camera test
    assert capacity == {
        "camera_slot_quantity": 8,
        "plan_camera_slots": 8,
        "vms_license_capacity": 8,
        "vms_license_required": False,
    }


def test_webhook_replay_is_idempotent_after_recovery(db_path, owner_stripe):
    with override_target(sqlite_path=db_path):
        ce.reconcile_owner_sandbox_subscription(
            customer_id="cust-owner", subscription_id="sub_owner_sandbox", approved_by="admin-1",
        )
        event = {
            "id": "evt_owner_sandbox_update",
            "type": "customer.subscription.updated",
            "livemode": False,
            "data": {"object": {"id": "sub_owner_sandbox"}},
        }
        first = ce.sync_entitlement_from_stripe_event(event)
        replay = ce.sync_entitlement_from_stripe_event(event)
        second = ce.sync_entitlement_from_stripe_event({**event, "id": "evt_owner_sandbox_update_2"})
        entitlements = ce.get_entitlements_for_customer("cust-owner")
        total = ce.total_camera_slots("cust-owner")

    assert first["status"] == "entitlement_updated"
    assert replay["status"] == "already_processed"
    assert second["status"] == "entitlement_updated"
    assert len(entitlements) == 2
    assert total == 8


def test_checkout_email_and_unverified_session_id_do_not_claim_account(db_path, owner_stripe):
    event = {
        "id": "evt_unlinked_owner_sandbox",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {
            "id": "cs_test_unlinked",
            "mode": "subscription",
            "payment_status": "paid",
            "customer": "cus_owner_sandbox",
            "subscription": "sub_owner_sandbox",
            "client_reference_id": "cust-owner",
            "customer_details": {"email": "owner@example.test"},
            "metadata": {},
        }},
    }
    with override_target(sqlite_path=db_path):
        result = ce.sync_entitlement_from_stripe_event(event)
        total = ce.total_camera_slots("cust-owner")
    assert result["status"] == "needs_account_link"
    assert total == 0


def test_wrong_existing_stripe_customer_binding_is_rejected(db_path, owner_stripe):
    import checkout_guard

    with override_target(sqlite_path=db_path):
        assert checkout_guard.bind_stripe_customer("cust-other", "cus_owner_sandbox")
        with pytest.raises(ValueError, match="already associated"):
            ce.reconcile_owner_sandbox_subscription(
                customer_id="cust-owner", subscription_id="sub_owner_sandbox", approved_by="admin-1",
            )
        assert ce.total_camera_slots("cust-owner") == 0
        assert ce.total_camera_slots("cust-other") == 0


def test_live_key_cannot_use_the_sandbox_price_pair(db_path, owner_stripe, monkeypatch):
    main, subscription = owner_stripe
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_" + "live_unit_fixture")
    assert ce._owner_sandbox_bundle_tiers(subscription, event_livemode=False) is None
    with override_target(sqlite_path=db_path), pytest.raises(ValueError, match="restricted to the Stripe sandbox"):
        ce.reconcile_owner_sandbox_subscription(
            customer_id="cust-owner", subscription_id="sub_owner_sandbox", approved_by="admin-1",
        )


@pytest.mark.parametrize("field,value", [
    ("interval_count", 2),
    ("interval_count", True),
    ("interval_count", 1.5),
    ("quantity", True),
    ("quantity", 1.5),
    ("quantity", "1"),
    ("unit_amount", 100.5),
    ("unit_amount", "100"),
])
def test_sandbox_bundle_rejects_nonmonthly_or_coerced_values(owner_stripe, field, value):
    _, subscription = owner_stripe
    item = subscription["items"]["data"][0]
    if field == "interval_count":
        item["price"]["recurring"][field] = value
    elif field == "quantity":
        item[field] = value
    else:
        item["price"][field] = value
    assert ce._owner_sandbox_bundle_tiers(subscription, event_livemode=False) is None


def test_customer_subscription_cancellation_revokes_both_bundle_entitlements(db_path, owner_stripe, monkeypatch):
    main, subscription = owner_stripe
    with override_target(sqlite_path=db_path):
        ce.reconcile_owner_sandbox_subscription(
            customer_id="cust-owner", subscription_id="sub_owner_sandbox", approved_by="admin-1",
        )
        canceled = {**subscription, "status": "canceled"}
        monkeypatch.setattr(ce, "current_subscription", lambda _subscription_id: canceled)
        event = {
            "id": "evt_owner_sandbox_cancel",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "data": {"object": {"id": "sub_owner_sandbox"}},
        }
        result = ce.sync_entitlement_from_stripe_event(event)
        entitlements = ce.get_entitlements_for_customer("cust-owner")
        total = ce.total_camera_slots("cust-owner")

    assert result["status"] == "entitlement_updated"
    assert len(entitlements) == 2
    assert all(item["status"] == "cancelled" and item["camera_slot_quantity"] == 0 for item in entitlements)
    assert total == 0


def test_failed_invoice_starts_existing_grace_period_for_both_entitlements(db_path, owner_stripe, monkeypatch):
    import billing_status
    import main

    with override_target(sqlite_path=db_path):
        ce.reconcile_owner_sandbox_subscription(
            customer_id="cust-owner", subscription_id="sub_owner_sandbox", approved_by="admin-1",
        )
        monkeypatch.setattr(main, "stripe_api_get", lambda _path: {"id": "in_owner_sandbox", "status": "open"})
        result = billing_status.sync_from_stripe_event({
            "id": "evt_owner_sandbox_payment_failed",
            "type": "invoice.payment_failed",
            "created": int(time.time()),
            "livemode": False,
            "data": {"object": {
                "id": "in_owner_sandbox", "subscription": "sub_owner_sandbox",
                "billing_reason": "subscription_cycle", "status": "open",
            }},
        })
        entitlements = ce.get_entitlements_for_customer("cust-owner")
        total = ce.total_camera_slots("cust-owner")
        grace_expired = billing_status.sweep_grace(
            now=datetime.now() + timedelta(days=8), customer_id="cust-owner",
        )
        expired_entitlements = ce.get_entitlements_for_customer("cust-owner")
        expired_capacity = ce.usable_camera_capacity("cust-owner")

    assert result["status"] == "grace_started"
    assert len(entitlements) == 2
    assert all(item["status"] == "active" and item["payment_failed_at"] for item in entitlements)
    assert total == 8
    assert grace_expired == 2
    assert all(item["status"] == "suspended" for item in expired_entitlements)
    assert expired_capacity == 0
