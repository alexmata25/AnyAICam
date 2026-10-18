"""My subscription (billing v2) plan emails (Green 2026-10-17).

Plans bought, changed or cancelled on My subscription sent no email: account-ready / plan-updated / plan-cancelled
were wired only to the legacy entitlement path. purchase_notifications.notify_plan_v2 now runs in the camera_plan_v2
webhook step, after the plan is stored, and reads the stored v2 row:
  * checkout.session.completed (v2)                          -> "Your AnyAiCam service is ready" (account_ready)
  * customer.subscription.updated with an items/quantity change -> "Your AnyAiCam plan has been updated" (plan_updated)
  * customer.subscription.deleted                            -> "... has been cancelled" (plan_cancelled)
A renewal (no plan or camera-count change) sends nothing, a replayed event never sends a second copy, and a Build Your
System order keeps its own order confirmation instead.
"""
import pytest

from test_billing_v2_resubscribe_grace_smart_motion import mail  # noqa: F401 -- fixture
from test_per_camera_stripe_lifecycle import PERIOD_END, SUB, _checkout_to_active, _event, _ok, v2  # noqa: F401
from test_stripe_billing_launch import _deliver, env  # noqa: F401 -- fixture


def _types(mail):
    return [m["type"] for m in mail["sent"] if m["type"] in ("account_ready", "plan_updated", "plan_cancelled")]


def _updated(sub, event_id, previous):
    event = _event(event_id, "customer.subscription.updated", sub)
    event["data"]["previous_attributes"] = previous
    return event


def test_a_my_subscription_purchase_change_and_cancellation_are_each_emailed_once(v2, mail):
    env = v2
    sub = _checkout_to_active(env, "ai_local", 4)
    assert _types(mail) == ["account_ready"]
    ready = [m for m in mail["sent"] if m["type"] == "account_ready"][0]
    assert ready["to"] == "a@example.test"
    assert "AI Local" in ready["text"] and "Camera capacity: 4" in ready["text"]

    # Replay of the checkout event: nothing more.
    _deliver(env, _event("evt_checkout", "checkout.session.completed", {}))
    assert _types(mail) == ["account_ready"]

    # More cameras on My subscription, confirmed by Stripe's subscription.updated.
    _ok(env["client"].post("/api/v2/customer/subscription/change", json={"plan_key": "ai_local", "camera_quantity": 6}))
    _deliver(env, _updated(sub, "evt_more", {"items": {"data": [{"quantity": 4}]}}))
    assert _types(mail) == ["account_ready", "plan_updated"]
    assert "6 cameras" in mail["sent"][-1]["text"]
    _deliver(env, _updated(sub, "evt_more", {"items": {"data": [{"quantity": 4}]}}))  # Stripe redelivery
    assert _types(mail) == ["account_ready", "plan_updated"]

    # A renewal (only the period moved): no email.
    _deliver(env, _updated(sub, "evt_renewal", {"current_period_end": PERIOD_END - 2_592_000}))
    assert _types(mail) == ["account_ready", "plan_updated"]

    # Cancellation, ended by Stripe.
    _ok(env["client"].post("/api/v2/customer/subscription/cancel"))
    sub["status"] = "canceled"
    _deliver(env, _event("evt_deleted", "customer.subscription.deleted", sub))
    assert _types(mail) == ["account_ready", "plan_updated", "plan_cancelled"]
    assert "AI Local" in mail["sent"][-1]["text"]


def test_a_plan_email_failure_never_fails_the_webhook_or_the_plan(v2, mail):
    env = v2
    mail["failing"].append(True)
    _checkout_to_active(env, "hybrid", 2)  # asserts the webhook answered 200
    import per_camera_billing
    assert per_camera_billing.entitlement_for_customer("cust-A")["status"] == "active"


def test_events_without_a_v2_plan_are_ignored():
    import purchase_notifications as pn
    assert pn.notify_plan_v2({"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}})["status"] == "ignored"
    assert pn.notify_plan_v2({"id": "evt_y", "type": "checkout.session.completed",
                              "data": {"object": {"metadata": {"anyaicam_billing_version": "1"}}}})["status"] == "ignored"
    assert pn.notify_plan_v2({"id": "", "type": "customer.subscription.deleted", "data": {}})["status"] == "ignored"
