"""Service paused / active again emails (Green 2026-10-17).

When the 7-day grace after a failed renewal ran out, or a refund or dispute suspended the plan, the customer got no
email beyond the first payment-failed one, and nothing said when service came back. billing_status now emails once
when it actually suspends a plan (once per failed-payment episode, whatever the sweep frequency) and once when a
payment restores it.
"""
from datetime import datetime, timedelta

from test_billing_v2_resubscribe_grace_smart_motion import mail  # noqa: F401 -- fixture
from test_per_camera_stripe_lifecycle import _checkout_to_active, _deliver, _event, _invoice, _state, v2  # noqa: F401
from test_stripe_billing_launch import env  # noqa: F401 -- fixture


def _service(mail):
    return [m for m in mail["sent"] if m["type"] in ("service_suspended", "service_restored")]


def _fail_renewal(env, sub):
    failed = _invoice("in_renew_1", sub, "hybrid", 2)
    failed["amount_paid"] = 0
    env["invoices"]["in_renew_1"] = dict(failed, status="open")
    sub["status"] = "past_due"
    _deliver(env, _event("evt_fail", "invoice.payment_failed", failed))
    _deliver(env, _event("evt_past_due", "customer.subscription.updated", sub))


def test_grace_expiry_pauses_service_with_one_email_and_payment_restores_it_with_another(v2, mail):
    import billing_status
    env = v2
    sub = _checkout_to_active(env, "hybrid", 2)
    _fail_renewal(env, sub)
    assert _service(mail) == []  # still in grace: only the payment-failed email
    billing_status.sweep_grace(datetime.now() + timedelta(days=8))
    billing_status.sweep_grace(datetime.now() + timedelta(days=9))  # the sweep runs often: still one email
    assert _state(env)["status"] == "suspended"
    paused = _service(mail)
    assert [m["type"] for m in paused] == ["service_suspended"]
    assert paused[0]["to"] == "a@example.test" and "payment needed" in paused[0]["subject"]
    assert "/subscription-portal" in paused[0]["text"]

    env["invoices"]["in_renew_1"] = dict(_invoice("in_renew_1", sub, "hybrid", 2), status="paid")
    sub["status"] = "active"
    _deliver(env, _event("evt_recovered", "invoice.paid", _invoice("in_renew_1", sub, "hybrid", 2)))
    assert _state(env)["status"] == "active"
    assert [m["type"] for m in _service(mail)] == ["service_suspended", "service_restored"]
    assert _service(mail)[-1]["subject"] == "Your AnyAiCam service is active again"


def test_a_refund_pauses_service_with_an_email(v2, mail):
    env = v2
    sub = _checkout_to_active(env, "hybrid", 2)
    refund = {"id": "ch_in_first_" + sub["id"], "object": "charge", "amount": 3998, "amount_refunded": 3998, "refunded": True,
              "payment_intent": "pi_in_first_" + sub["id"], "invoice": "in_first_" + sub["id"]}
    _deliver(env, _event("evt_refund", "charge.refunded", refund))
    assert _state(env)["status"] == "suspended"
    assert [m["type"] for m in _service(mail)] == ["service_suspended"]
    assert "refunded" in _service(mail)[0]["text"]
    _deliver(env, _event("evt_refund", "charge.refunded", refund))  # Stripe redelivery: no second email
    assert [m["type"] for m in _service(mail)] == ["service_suspended"]


def test_no_email_when_nothing_changed_state():
    import purchase_notifications as pn
    assert pn.notify_service_suspended("sub_unknown", "something_else")["status"] == "ignored"
    assert pn.notify_service_suspended("sub_unknown", "payment_failed")["status"] == "ignored"
    assert pn.notify_service_restored("sub_unknown")["status"] == "ignored"
