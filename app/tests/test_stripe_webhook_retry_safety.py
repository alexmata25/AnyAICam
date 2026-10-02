"""Stripe webhook retry safety (2026-09-24).

The webhook used to record an event as processed BEFORE running its
provisioning steps, swallow every step failure, and always answer 200 --
so a transient failure was never retried by Stripe and a paid purchase
could silently never be provisioned. Each provisioning step is now
tracked per event (stripe_webhook_steps): a failure answers 503 so Stripe
redelivers, and a redelivery runs only the steps not yet completed, so
nothing is ever provisioned twice. No real Stripe call is made; the
signature check is bypassed exactly as the sibling webhook tests do.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_stripe_webhook_entitlement_sync import _checkout_completed_event, _post_webhook, client, db_path  # noqa: F401

STEPS = ("legacy_billing", "camera_slot_entitlements", "hardware_orders", "analytics_entitlements")


@pytest.fixture()
def counted_steps(client, monkeypatch):
    """Replace every step with a counter (optionally failing) -- the real
    steps are covered by their own suites; this file is about the retry
    bookkeeping around them."""
    _test_client, main = client
    calls = {name: 0 for name in STEPS}
    failing = set()

    def make(name):
        def run(event):
            calls[name] += 1
            if name in failing:
                raise RuntimeError(f"simulated {name} failure")
        return run

    monkeypatch.setattr(main, "_stripe_webhook_steps", lambda: [(name, make(name)) for name in STEPS])
    return calls, failing


def _step_rows(db_path, event_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {row["step"]: dict(row) for row in conn.execute("SELECT * FROM stripe_webhook_steps WHERE event_id=?", (event_id,))}
    finally:
        conn.close()


def test_every_step_runs_once_and_a_redelivery_runs_nothing(client, db_path, counted_steps):
    test_client, _main = client
    calls, _failing = counted_steps
    assert _post_webhook(test_client, _checkout_completed_event(event_id="evt_ok")).status_code == 200
    again = _post_webhook(test_client, _checkout_completed_event(event_id="evt_ok"))
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert calls == {name: 1 for name in STEPS}
    assert {row["status"] for row in _step_rows(db_path, "evt_ok").values()} == {"completed"}


def test_a_failed_step_is_retried_alone(client, db_path, counted_steps):
    test_client, _main = client
    calls, failing = counted_steps
    failing.add("hardware_orders")
    first = _post_webhook(test_client, _checkout_completed_event(event_id="evt_hw"))
    assert first.status_code == 503 and "hardware_orders" in first.json()["detail"]
    rows = _step_rows(db_path, "evt_hw")
    assert rows["hardware_orders"]["status"] == "failed" and "simulated" in rows["hardware_orders"]["last_error"]

    failing.clear()
    assert _post_webhook(test_client, _checkout_completed_event(event_id="evt_hw")).status_code == 200
    assert calls == {"legacy_billing": 1, "camera_slot_entitlements": 1, "hardware_orders": 2, "analytics_entitlements": 1}
    assert _step_rows(db_path, "evt_hw")["hardware_orders"]["attempts"] == 2


def test_a_step_running_in_another_worker_is_not_run_twice(client, db_path, counted_steps):
    test_client, _main = client
    calls, _failing = counted_steps
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO stripe_webhook_steps(event_id,step,status,attempts,updated_at) VALUES('evt_busy','camera_slot_entitlements','running',1,?)", (datetime.now().isoformat(),))
    conn.commit(); conn.close()
    response = _post_webhook(test_client, _checkout_completed_event(event_id="evt_busy"))
    assert response.status_code == 503 and "camera_slot_entitlements" in response.json()["detail"]
    assert calls["camera_slot_entitlements"] == 0


def test_a_step_left_running_by_a_crashed_worker_is_reclaimed(client, db_path, counted_steps, monkeypatch):
    test_client, main = client
    calls, _failing = counted_steps
    stale = (datetime.now() - timedelta(seconds=main.STRIPE_WEBHOOK_STEP_STALE_SECONDS + 60)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO stripe_webhook_steps(event_id,step,status,attempts,updated_at) VALUES('evt_stale','camera_slot_entitlements','running',1,?)", (stale,))
    conn.commit(); conn.close()
    assert _post_webhook(test_client, _checkout_completed_event(event_id="evt_stale")).status_code == 200
    assert calls["camera_slot_entitlements"] == 1


def test_claims_are_atomic(client, db_path):
    _test_client, main = client
    with override_target(sqlite_path=str(db_path)):
        assert main._claim_stripe_webhook_step("evt_claim", "hardware_orders") == "claimed"
        assert main._claim_stripe_webhook_step("evt_claim", "hardware_orders") == "busy"
        main._finish_stripe_webhook_step("evt_claim", "hardware_orders", "completed")
        assert main._claim_stripe_webhook_step("evt_claim", "hardware_orders") == "already_completed"


def test_an_event_processed_before_per_step_tracking_is_not_reprocessed(client, db_path, counted_steps):
    test_client, main = client
    calls, _failing = counted_steps
    event = _checkout_completed_event(event_id="evt_legacy_done")
    main.record_stripe_webhook_event(event)  # the old handler's own record, no step rows
    response = _post_webhook(test_client, event)
    assert response.status_code == 200 and response.json()["duplicate"] is True
    assert calls == {name: 0 for name in STEPS}


def test_an_event_without_an_id_is_rejected(client, counted_steps):
    test_client, _main = client
    event = _checkout_completed_event()
    event["id"] = ""
    assert _post_webhook(test_client, event).status_code == 400
