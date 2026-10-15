"""Free storage trial countdown (owner requirement change 2026-10-15).

The 30 days must not run while hardware is awaiting delivery. An
administrator grant only reserves the trial; the 30 days start when the
cloud-storage subscription is created -- in the hardware-first flow, the
separate subscription Checkout the customer opens after delivery. Stripe is
a test double: no Stripe API is ever called.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database_backend import override_target
from test_direct_onboarding import _one, site  # noqa: F401 -- fixtures
from test_hardware_first_checkout import ORDER, _paid_event, _record, _sessions, hw  # noqa: F401 -- fixtures
from test_pricing_ff_commission import OWNER, _cookie, _make_global_admin, _seed, db_path, license_portal, portal  # noqa: F401

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

CUSTOMER = OWNER[2]
TRIAL_KEYS = ("subscription_data[trial_end]", "subscription_data[trial_settings][end_behavior][missing_payment_method]",
              "payment_method_collection", "metadata[anyaicam_storage_trial]", "subscription_data[metadata][anyaicam_storage_trial]",
              "trial_end", "trial_period_days", "subscription_data[trial_period_days]", "trial_settings[end_behavior][missing_payment_method]")


def _trial(db_path):
    return _one(db_path, "SELECT * FROM storage_trials WHERE customer_id=? ORDER BY granted_at DESC", (CUSTOMER,))


def _grant(hw, db_path):
    response = hw["client"].post("/api/admin/storage-trials", json={"customer_id": CUSTOMER, "reason": "Evaluating cloud storage"},
                                 cookies=_make_global_admin(db_path))
    assert response.status_code == 200, response.text
    return _trial(db_path)


def _no_trial(fields):
    return not any(key in fields for key in TRIAL_KEYS) and not any(key.startswith("discounts[") for key in fields)


def _fulfillment(db_path, status):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET fulfillment_status=? WHERE customer_id=?", (status, CUSTOMER))
    conn.commit()
    conn.close()


def _webhook(hw, event):
    response = hw["client"].post("/api/payments/stripe/webhook", content=json.dumps(event),
                                 headers={"Stripe-Signature": "t=1,v1=test", "Content-Type": "application/json"})
    assert response.status_code == 200, response.text


def test_the_countdown_starts_at_the_storage_subscription_not_at_the_grant_or_during_delivery(hw, db_path, monkeypatch):
    import storage_trial
    _seed(db_path)
    owner = _cookie(*OWNER)
    granted_at = datetime.now(timezone.utc)
    trial = _grant(hw, db_path)
    assert trial["status"] == "granted" and trial["starts_at"] == "" and trial["expires_at"] == ""

    # Hardware-first: the appliance is bought and paid for on its own Checkout,
    # which never carries the trial.
    response = hw["client"].post("/api/hardware-checkout", json={**ORDER, "email": "owner@example.test"}, cookies=owner)
    assert response.status_code == 200, response.text
    hardware = _sessions(hw)[-1]
    assert hardware["mode"] == "payment" and _no_trial(hardware)
    session_id = response.json()["session_id"]
    _record(db_path, _paid_event(hw, session_id, email="owner@example.test"))
    assert _one(db_path, "SELECT COUNT(*) AS n FROM hardware_orders WHERE customer_id=?", (CUSTOMER,))["n"] >= 1
    _fulfillment(db_path, "shipped")  # paid, awaiting delivery

    # 40 days pass while the hardware is on its way: the trial is untouched.
    later = granted_at + timedelta(days=40)
    monkeypatch.setattr(storage_trial, "_now", lambda: later)
    with override_target(sqlite_path=str(db_path)):
        view = storage_trial.customer_view(CUSTOMER)
        assert view["status"] == "granted" and not view["countdown_started"] and view["expires_at"] is None
        assert "reserved" in storage_trial.customer_note_html(CUSTOMER, True)
    assert _trial(db_path)["status"] == "granted"

    # Delivered: the customer starts the separate storage subscription.
    _fulfillment(db_path, "delivered")
    response = hw["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 4},
                                 cookies=owner)
    assert response.status_code == 200, response.text
    fields = [f for f in hw["captured"] if f.get("mode") == "subscription"][-1]
    trial_end = int(fields["subscription_data[trial_end]"])
    assert trial_end == int((later + timedelta(days=30)).timestamp())  # start + 30 days, not grant + 30 days
    assert fields["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "cancel"
    assert fields["payment_method_collection"] == "if_required"
    assert fields["subscription_data[metadata][anyaicam_storage_trial]"] == trial["id"]
    assert fields["line_items[0][price]"] == "price_srv_hybrid" and "line_items[1][price]" not in fields
    assert _trial(db_path)["status"] == "granted"  # an unfinished Checkout starts nothing

    # Stripe creates the trial subscription: the verified webhook starts the countdown.
    start = int(later.timestamp())
    subscription = {"id": "sub_hw_first", "object": "subscription", "status": "trialing", "customer": "cus_test_cust-1",
                    "cancel_at_period_end": False, "trial_start": start, "trial_end": trial_end, "current_period_end": trial_end,
                    "metadata": {"anyaicam_customer_id": CUSTOMER, "anyaicam_billing_version": "2", "anyaicam_plan_key": "hybrid",
                                 "anyaicam_camera_quantity": "4", "anyaicam_stripe_price_id": "price_srv_hybrid",
                                 "anyaicam_storage_trial": trial["id"]},
                    "items": {"data": [{"id": "si_hw_first", "quantity": 4, "price": {"id": "price_srv_hybrid"}}]}}
    _webhook(hw, {"id": "evt_trial_sub_created", "type": "customer.subscription.created", "livemode": False,
                  "data": {"object": subscription}})
    assert _one(db_path, "SELECT status FROM stripe_webhook_steps WHERE event_id='evt_trial_sub_created' AND step='storage_trials'")[
        "status"] == "completed"
    started = _trial(db_path)
    assert started["status"] == "active" and started["stripe_subscription_id"] == "sub_hw_first"
    starts, expires = datetime.fromisoformat(started["starts_at"]), datetime.fromisoformat(started["expires_at"])
    assert int(starts.timestamp()) == start and expires - starts == timedelta(days=30)
    assert expires - granted_at >= timedelta(days=70) - timedelta(seconds=1)  # the delivery wait never counted
    logged = _one(db_path, "SELECT details_json FROM audit_logs WHERE action='storage_trial.start'")
    assert json.loads(logged["details_json"])["source"] == "webhook"
    # The no-charge guard was sent to Stripe for the new trial subscription.
    assert {"cancel_at_period_end": "true"} in hw["captured"]

    # The full 30 days are available from the start, and then it expires.
    monkeypatch.setattr(storage_trial, "_now", lambda: starts + timedelta(days=29, hours=23))
    with override_target(sqlite_path=str(db_path)):
        assert storage_trial.customer_view(CUSTOMER)["status"] == "active"
    monkeypatch.setattr(storage_trial, "_now", lambda: expires + timedelta(seconds=1))
    with override_target(sqlite_path=str(db_path)):
        assert storage_trial.customer_view(CUSTOMER)["status"] == "expired"


def test_with_a_granted_trial_only_the_hybrid_storage_checkout_carries_it(hw, db_path, monkeypatch):
    """Hardware, the VMS license, analytics, Talk Down and local plans keep
    their normal prices and never read or start the trial."""
    import main
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", lambda *args: None)
    _seed(db_path)
    owner = _cookie(*OWNER)
    client = hw["client"]
    _grant(hw, db_path)
    assert client.post("/api/hardware-checkout", json={**ORDER, "email": "owner@example.test"}, cookies=owner).status_code == 200
    assert _no_trial(_sessions(hw)[-1])
    response = client.post("/api/customer/vms-license/checkout", json={"capacity": 16}, cookies=owner)
    assert response.status_code == 200, response.text
    assert hw["captured"][-1]["mode"] == "payment" and _no_trial(hw["captured"][-1])
    for addon in ({"addon_key": "talk_down", "quantity": 2}, {"addon_key": "advanced_analytics"}):
        response = client.post("/api/customer/analytics/checkout", json=addon, cookies=owner)
        assert response.status_code == 200, response.text
        assert _no_trial(hw["captured"][-1]), addon
    for plan_key in ("basic_local", "ai_local"):
        response = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": plan_key, "camera_quantity": 4},
                               cookies=owner)
        assert response.status_code == 200, response.text
        assert hw["captured"][-1]["mode"] == "subscription" and _no_trial(hw["captured"][-1]), plan_key
    untouched = _trial(db_path)
    assert untouched["status"] == "granted" and untouched["expires_at"] == ""  # nothing offered, nothing started
    response = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 4}, cookies=owner)
    assert response.status_code == 200, response.text
    assert hw["captured"][-1]["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "cancel"


def test_a_repeated_storage_checkout_reuses_the_same_trial_end(hw, db_path, monkeypatch):
    """A second click within the hour sends identical fields, so the open
    Checkout Session is reused instead of a second one being refused."""
    import storage_trial
    _seed(db_path)
    _grant(hw, db_path)
    now = datetime.now(timezone.utc)
    with override_target(sqlite_path=str(db_path)):
        first = dict(storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid"))
        monkeypatch.setattr(storage_trial, "_now", lambda: now + timedelta(minutes=20))
        again = dict(storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid"))
        monkeypatch.setattr(storage_trial, "_now", lambda: now + timedelta(days=3))
        later = dict(storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid"))
    assert first == again
    assert int(later["subscription_data[trial_end]"]) == int((now + timedelta(days=33)).timestamp())


def test_rows_from_the_first_version_become_granted_until_storage_starts(db_path):
    """Migration 20261015_storage_trials_pending_start (the feature was never
    deployed): an active row without a subscription had its countdown start
    at the grant; it is moved back to granted. A row with a subscription
    keeps its dates."""
    import db_migrations
    script = dict(db_migrations.MIGRATIONS)["20261015_storage_trials_pending_start"]
    _seed(db_path)
    _seed(db_path, customer_id="cust-2", email="two@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX IF EXISTS idx_storage_trials_one_open")
    insert = ("INSERT INTO storage_trials(id,customer_id,status,starts_at,expires_at,granted_by,granted_at,reason,stripe_subscription_id,"
              "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)")
    conn.execute(insert, ("old-1", "cust-1", "active", "2026-10-15T10:00:00+00:00", "2026-11-14T10:00:00+00:00", "a", "2026-10-15T10:00:00+00:00",
                          "r", None, "x"))
    conn.execute(insert, ("old-2", "cust-2", "active", "2026-10-15T10:00:00+00:00", "2026-11-14T10:00:00+00:00", "a", "2026-10-15T10:00:00+00:00",
                          "r", "sub_x", "x"))
    for statement in [item.strip() for item in script.split(";") if item.strip()]:
        conn.execute(statement)
    conn.commit()
    found = {r[0]: r[1:] for r in conn.execute("SELECT id,status,starts_at,expires_at FROM storage_trials")}
    conn.close()
    assert found["old-1"] == ("granted", "", "")
    assert found["old-2"] == ("active", "2026-10-15T10:00:00+00:00", "2026-11-14T10:00:00+00:00")
