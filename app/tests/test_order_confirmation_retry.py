"""Build Your System order-confirmation email: durable retry (Codex review of
f7ebb99, 2026-10-05).

notify_order_confirmed() never raises, so the Stripe webhook step completes
even when the email provider fails, and a redelivered webhook skips completed
steps. The send therefore stays in purchase_notifications' outbox
(provisioning_notifications, key build-order-confirmed:<customer>) as
'failed' and the billing worker retries it -- exactly one successful email per
account, never a lost one, and never any effect on the payment or the plan.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_hardware_delivery_flow import (APPLIANCE, OWN_PC, _event, _hardware_order, _notify, _paid,  # noqa: F401
                                         db_path, license_portal, package, portal, shop, site, storage)
from test_hybrid_build_system_flow import _activate_appliance

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


@pytest.fixture()
def provider(monkeypatch):
    """An email provider that can be switched down and back up."""
    import purchase_notifications
    state = {"down": False, "delivered": [], "attempts": 0}

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            state["attempts"] += 1
            if state["down"]:
                raise OSError("email provider unavailable")
            state["delivered"].append({"type": message_type, "to": to, "subject": subject, "text": text})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return state


def _retry(db_path):
    import order_funnel
    with override_target(sqlite_path=str(db_path)):
        return order_funnel.retry_order_confirmation_notifications()


def _outbox(db_path, customer_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    found = [dict(r) for r in conn.execute("SELECT notification_type,status,error_detail FROM provisioning_notifications "
                                           "WHERE stripe_event_id=?", (f"build-order-confirmed:{customer_id}",))]
    conn.close()
    return found


def _plan_is_active(db_path, customer_id, quantity):
    import customer_entitlements as ce
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        entitlement = billing.entitlement_for_customer(customer_id)
        return entitlement["status"] == "active" and ce.usable_camera_capacity(customer_id) == quantity


def test_initial_success_sends_one_email(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "ok@example.test", APPLIANCE)
    assert _notify(db_path, _event(customer_id))["status"] == "sent"
    assert len(provider["delivered"]) == 1 and _outbox(db_path, customer_id)[0]["status"] == "sent"
    assert _retry(db_path) == 0


def test_initial_failure_is_kept_for_retry_and_the_plan_stays_valid(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "fail@example.test", APPLIANCE)
    provider["down"] = True
    result = _notify(db_path, _event(customer_id))
    assert result["status"] == "failed" and provider["delivered"] == []
    assert _outbox(db_path, customer_id) == [{"notification_type": "hardware_order_confirmation", "status": "failed",
                                              "error_detail": "email provider unavailable"}]
    assert _plan_is_active(db_path, customer_id, 8)  # the payment's entitlement is untouched


def test_failure_then_retry_delivers_exactly_one_email(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "retry@example.test", APPLIANCE)
    provider["down"] = True
    _notify(db_path, _event(customer_id))
    assert _retry(db_path) == 0 and provider["delivered"] == []  # still down: stays failed, no exception
    assert _outbox(db_path, customer_id)[0]["status"] == "failed"
    provider["down"] = False
    assert _retry(db_path) == 1
    assert _retry(db_path) == 0
    assert len(provider["delivered"]) == 1 and provider["delivered"][0]["subject"].endswith("Your AnyAiCam order is confirmed")
    assert [row["status"] for row in _outbox(db_path, customer_id)] == ["sent"]


def test_duplicate_webhook_after_failure_sends_once_when_the_provider_recovers(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "dup-fail@example.test", OWN_PC)
    provider["down"] = True
    assert _notify(db_path, _event(customer_id, "evt_1"))["status"] == "failed"
    assert _notify(db_path, _event(customer_id, "evt_1"))["status"] == "failed"  # redelivery while still down
    provider["down"] = False
    assert _notify(db_path, _event(customer_id, "evt_2"))["status"] == "sent"  # a later delivery
    assert _notify(db_path, _event(customer_id, "evt_1"))["status"] == "skipped"
    assert _retry(db_path) == 0
    assert len(provider["delivered"]) == 1 and provider["delivered"][0]["type"] == "account_ready"
    assert len(_outbox(db_path, customer_id)) == 1  # one outbox row, never a second one


def test_duplicate_webhook_after_success_never_sends_again(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "dup-ok@example.test", APPLIANCE)
    for event_id in ("evt_1", "evt_1", "evt_2", "evt_3"):
        _notify(db_path, _event(customer_id, event_id))
    _retry(db_path)
    assert len(provider["delivered"]) == 1


def test_exactly_one_successful_delivery_across_failures_retries_and_redeliveries(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "once@example.test", APPLIANCE)
    provider["down"] = True
    _notify(db_path, _event(customer_id, "evt_1"))
    _retry(db_path)
    _notify(db_path, _event(customer_id, "evt_2"))
    provider["down"] = False
    _retry(db_path)
    _notify(db_path, _event(customer_id, "evt_1"))
    _retry(db_path)
    # A later hardware order would change the wording; the outbox row is reused, never doubled.
    _hardware_order(db_path, customer_id, fulfillment="shipped")
    _notify(db_path, _event(customer_id, "evt_3"))
    assert len(provider["delivered"]) == 1 and len(_outbox(db_path, customer_id)) == 1
    assert provider["attempts"] == 4  # three failed attempts, one success


def test_an_email_failure_in_the_webhook_step_never_fails_the_step_or_the_payment(shop, db_path, provider, monkeypatch):
    import main
    import per_camera_billing
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "step@example.test", APPLIANCE)
    provider["down"] = True
    monkeypatch.setattr(per_camera_billing, "sync_from_stripe_event", lambda event: {"status": "entitlement_updated"})
    step = dict(main._stripe_webhook_steps())["camera_plan_v2_entitlements"]
    with override_target(sqlite_path=str(db_path)):
        step(_event(customer_id))  # does not raise: the step completes, Stripe gets its 200
    assert _outbox(db_path, customer_id)[0]["status"] == "failed"
    assert _plan_is_active(db_path, customer_id, 8)
    provider["down"] = False
    assert _retry(db_path) == 1 and len(provider["delivered"]) == 1


def test_an_interrupted_send_is_retried_only_once_it_is_stale(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "pending@example.test", APPLIANCE)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO provisioning_notifications(id,stripe_event_id,notification_type,customer_id,recipient_email,"
                 "subject,status,created_at,updated_at) VALUES('n1',?,?,?,?,?,'pending',?,?)",
                 (f"build-order-confirmed:{customer_id}", "hardware_order_confirmation", customer_id, "pending@example.test",
                  "Your AnyAiCam order is confirmed", datetime.now().isoformat(), datetime.now().isoformat()))
    conn.commit()
    assert _retry(db_path) == 0  # possibly still sending right now
    old = (datetime.now() - timedelta(minutes=10)).isoformat()
    conn.execute("UPDATE provisioning_notifications SET updated_at=? WHERE id='n1'", (old,))
    conn.commit()
    conn.close()
    assert _retry(db_path) == 1 and len(provider["delivered"]) == 1


def test_a_confirmation_still_unsent_when_setup_finishes_is_closed_not_retried_forever(shop, db_path, provider):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "late@example.test", APPLIANCE)
    provider["down"] = True
    _notify(db_path, _event(customer_id))
    _hardware_order(db_path, customer_id, fulfillment="delivered")
    _activate_appliance(db_path, customer_id)
    provider["down"] = False
    assert _retry(db_path) == 0
    assert _outbox(db_path, customer_id)[0]["status"] == "superseded"
    assert _retry(db_path) == 0 and provider["delivered"] == []


def test_the_billing_worker_runs_the_order_confirmation_retry():
    import inspect
    import main
    source = inspect.getsource(main._billing_grace_worker)
    assert "retry_order_confirmation_notifications" in source and "retry_payment_failed_notifications" in source
