"""C5 (owner-authorized correction, 2026-10-21): a refunded or disputed one-time own-PC VMS licence can no longer be
bypassed with a camera plan alone.

Before (7f512554): an own-PC customer whose Build Your System order was fully refunded (or disputed) and whose
subscription ended still counted as activated (per_camera_billing.account_activated counted the cancelled plan), so My
subscription sold a plan-only checkout (POST /api/v2/customer/subscription/checkout adds no licence line), and that plan
alone gave installer download, licence capacity and signed camera capacity through the billing-v2 rule
per_camera_billing.vms_license_capacity ("an active per-camera plan carries the software licence").

Now (per_camera_billing.own_pc_licence_reversed):
* the ended plan of such an account no longer activates it: My subscription shows "Activate your system" and refuses
  the plan checkout (409 activate_first, no Stripe call); the customer orders again in Build Your System, which charges
  the licence (R7);
* defense in depth: an active plan of such an account (a plan restored by a later paid month, or a plan checkout that
  was opened before the refund and paid after it) never carries the software licence: download refused, licence and
  signed capacity 0;
* unchanged: an active licence with its plan, resubscription with an active licence, a won dispute, the R7 repurchase,
  appliance customers (their purchase includes the licence) and accounts that never had a licence row.

Not implemented (Option B B3, deferred from 1.2.7): a combined licence + plan checkout in My subscription.

Real routes (/subscription-portal, /api/v2/customer/subscription/checkout, /order-summary,
/api/v2/customer/build-order/checkout, /api/payments/stripe/webhook) and the real billing code; Stripe is the fake of
test_refund_before_record_race_127.py.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _checkouts, _paid_checkout_event,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)
from test_refund_before_record_race_127 import (_access, _configure, _dispute, _ids, _invoice_paid, _refund,  # noqa: F401
                                                _sub_event, stripe)
from test_r7_repurchase_after_refund_127 import (_capacity, _end_order1, _licence_row, _new_order, _ok, _order1_paid,
                                                 _order2_paid, _plan_row, _r7_ready, _replay_all, _subscription)

pytestmark = [pytest.mark.real_activation]

PLAN_CHECKOUT = "/api/v2/customer/subscription/checkout"


# ------------------------------------------------------------------ helpers

def _signed(db_path, customer_id):
    """The camera capacity the cloud signs for this account's appliance (feature_entitlements.appliance_snapshot)."""
    import feature_entitlements
    with override_target(sqlite_path=str(db_path)):
        snapshot = feature_entitlements.appliance_snapshot({"id": "appl-c5", "cloud_id": "cloud-c5", "customer_id": customer_id})
    return snapshot["camera_capacity"]


def _summary(db_path, customer_id):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(db_path)):
        return ce.vms_license_summary(customer_id)


def _entitled(db_path, customer_id):
    """(activated, download allowed, licence capacity, usable cameras, signed usable cameras)"""
    activated, download = _access(db_path, customer_id)
    capacity = _capacity(db_path, customer_id)
    return activated, download, capacity["vms_license_capacity"], capacity["camera_slot_quantity"], \
        _signed(db_path, customer_id)["camera_slot_quantity"]


def _open_plan_checkout(shop, quantity=5):
    return shop[0].post(PLAN_CHECKOUT, json={"plan_key": "ai_local", "camera_quantity": quantity})


def _refused_plan_checkout(shop):
    """My subscription refuses the plan-only checkout server-side and shows the way forward; Stripe is never called."""
    client, captured = shop[0], shop[1]
    before = len(_checkouts(captured))
    response = _open_plan_checkout(shop)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "activate_first", response.text
    assert len(_checkouts(captured)) == before
    page = client.get("/subscription-portal")
    assert page.status_code == 200 and 'id="v2-activate-first"' in page.text and 'id="v2-activate-link"' in page.text
    assert 'id="v2-plan-key"' not in page.text  # no live plan picker
    return response


def _pay_plan_checkout(shop, db_path, stripe, customer_id, stripe_customer, n, quantity=5):
    """The latest My subscription plan checkout, paid: checkout.session.completed, invoice.paid and
    customer.subscription.created through the webhook route."""
    client, captured = shop[0], shop[1]
    fields = _checkouts(captured)[-1]
    assert fields["metadata[anyaicam_billing_version]"] == "2" and "line_items[1][price]" not in fields  # plan only
    ids = _ids(n)
    event = _paid_checkout_event(captured, customer_id, ids["session"])
    obj = event["data"]["object"]
    obj["invoice"], obj["payment_intent"], obj["subscription"], obj["customer"] = ids["invoice"], None, ids["sub"], stripe_customer
    _configure(stripe, ids, stripe_customer)
    _subscription(stripe, ids, customer_id, stripe_customer, quantity=quantity)
    events = {"checkout": event, "invoice": _invoice_paid(ids), "created": _sub_event(stripe, ids, "created")}
    for name in ("checkout", "invoice", "created"):
        _ok(client, events[name])
    plan = _plan_row(db_path, customer_id)
    assert (plan["status"], plan["stripe_subscription_id"], plan["camera_quantity"]) == ("active", ids["sub"], quantity)
    return ids, events


def _disputed_and_ended(shop, db_path, stripe, email, *, lost=False):
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, email)
    events["dispute"] = _dispute(ids, "created")
    _ok(shop[0], events["dispute"])
    if lost:  # charge.dispute.closed (lost) is recorded as 'ignored': locally the same as an open dispute (C4)
        _ok(shop[0], _dispute(ids, "closed", status="lost"))
    licence = _licence_row(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"]) == ("suspended", "disputed")
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    return customer_id, ids, events, stripe_customer


# ------------------------------------------------------------------ refunded / disputed: no plan-only bypass

def test_refunded_and_ended_order_cannot_buy_a_plan_only_in_my_subscription(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe, "c5refund@example.test")
    assert _entitled(db_path, customer_id) == (False, False, 0, 0, 0)  # before C5: activated True (cancelled plan)
    _refused_plan_checkout(shop)
    assert _plan_row(db_path, customer_id)["status"] == "cancelled"
    assert _summary(db_path, customer_id)["status"] == "suspended" and _summary(db_path, customer_id)["cameras"] == 0
    # The way forward is Build Your System: the new order charges the licence again (R7).
    page, opened = _new_order(shop)
    fields = _checkouts(shop[1])[-1]
    assert opened["status"] == "complete" and fields["line_items[1][price]"] == "price_vms_16"


@pytest.mark.parametrize("lost", [False, True], ids=["open-dispute", "lost-dispute"])
def test_disputed_and_ended_order_cannot_buy_a_plan_only_in_my_subscription(shop, db_path, stripe, lost):
    customer_id, ids, events, stripe_customer = _disputed_and_ended(shop, db_path, stripe, f"c5dp{int(lost)}@example.test",
                                                                    lost=lost)
    assert _entitled(db_path, customer_id) == (False, False, 0, 0, 0)
    _refused_plan_checkout(shop)
    assert _licence_row(db_path, customer_id)["suspended_reason"] == "disputed"


def test_won_dispute_restores_the_licence_and_resubscription_works(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _disputed_and_ended(shop, db_path, stripe, "c5won@example.test")
    _refused_plan_checkout(shop)
    _ok(shop[0], _dispute(ids, "closed", status="won", event_id="evt_c5_dp_won"))
    assert _licence_row(db_path, customer_id)["status"] == "active"
    assert _access(db_path, customer_id) == (True, True)  # the licence alone: activated and downloadable
    assert _open_plan_checkout(shop).status_code == 200
    _pay_plan_checkout(shop, db_path, stripe, customer_id, stripe_customer, 3)
    assert _entitled(db_path, customer_id) == (True, True, 5, 5, 5)


@pytest.mark.parametrize("reason", ["refund", "dispute"])
def test_a_plan_checkout_opened_before_the_licence_reversal_and_paid_after_it_carries_no_licence(shop, db_path, stripe,
                                                                                                 reason):
    """Defense in depth (the readers, not only the activation gate): the plan ended without a refund, the customer opened
    a plan checkout in My subscription (allowed: the licence was active), then the licence payment was refunded or
    disputed, then the checkout was paid. The plan is active and billed, but it never licenses the software."""
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, f"c5late{reason}@example.test")
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    assert _open_plan_checkout(shop).status_code == 200
    reversal = _refund(ids, "evt_c5_late_refund") if reason == "refund" else _dispute(ids, "created", event_id="evt_c5_late_dp")
    _ok(shop[0], reversal)
    assert _licence_row(db_path, customer_id)["status"] == "suspended"
    _pay_plan_checkout(shop, db_path, stripe, customer_id, stripe_customer, 3)
    activated, download, licence, usable, signed = _entitled(db_path, customer_id)
    assert (download, licence, usable, signed) == (False, 0, 0, 0)
    assert activated is True  # the running plan stays manageable in My subscription (change, cancel)
    capacity = _capacity(db_path, customer_id)
    assert capacity["plan_camera_slots"] == 5 and capacity["vms_license_required"] is True
    assert _signed(db_path, customer_id) == {"camera_slot_quantity": 0, "plan_camera_slots": 5, "vms_license_capacity": 0}
    summary = _summary(db_path, customer_id)
    assert (summary["kind"], summary["status"], summary["cameras"]) == ("one_time_vms_license", "suspended", 0)
    # A second plan beside the running one is refused as before.
    assert _open_plan_checkout(shop).status_code == 409
    # Camera provisioning gives the licence reason (existing wording).
    import customer_entitlements as ce
    with override_target(sqlite_path=str(db_path)):
        assert ce.capacity_refusal_code(customer_id) == "vms_license_required"


def test_a_refunded_licence_with_a_later_paid_month_of_its_plan_carries_no_licence(shop, db_path, stripe):
    """F1 follow-up item 1: the first invoice (licence + plan) is fully refunded, a later month is paid, so the plan is
    active again (as before). The refunded licence stays terminal and the plan no longer carries the software."""
    import billing_status
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "c5later@example.test")
    _ok(shop[0], _refund(ids, "evt_c5_refund_first"))
    with override_target(sqlite_path=str(db_path)):
        billing_status.payment_recovered(ids["sub"])  # a later invoice of the subscription was paid
    assert _plan_row(db_path, customer_id)["status"] == "active"
    assert _licence_row(db_path, customer_id)["suspended_reason"] == "refunded"
    assert _entitled(db_path, customer_id) == (True, False, 0, 0, 0)


def test_replays_of_the_old_order_change_nothing(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe, "c5replay@example.test")
    _replay_all(shop, stripe, ids, events, "c5")
    assert _licence_row(db_path, customer_id)["status"] == "suspended" and _plan_row(db_path, customer_id)["status"] == "cancelled"
    assert _entitled(db_path, customer_id) == (False, False, 0, 0, 0)
    _refused_plan_checkout(shop)


# ------------------------------------------------------------------ valid licences and legitimate resubscription

def test_active_licence_and_plan_are_unchanged(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "c5paid@example.test")
    assert _entitled(db_path, customer_id) == (True, True, 5, 5, 5)


def test_active_licence_whose_plan_ended_resubscribes_in_my_subscription(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "c5resub@example.test")
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    assert _entitled(db_path, customer_id) == (True, True, 8, 0, 0)  # licence alone: download, no plan cameras
    page = shop[0].get("/subscription-portal")
    assert 'id="v2-plan-key"' in page.text and 'id="v2-activate-first"' not in page.text
    assert _open_plan_checkout(shop, quantity=6).status_code == 200
    _pay_plan_checkout(shop, db_path, stripe, customer_id, stripe_customer, 3, quantity=6)
    assert _entitled(db_path, customer_id) == (True, True, 6, 6, 6)


def test_r7_repurchase_after_a_refund_restores_licence_and_capacity(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe, "c5r7@example.test")
    _refused_plan_checkout(shop)
    _new_order(shop)
    ids2, _ = _order2_paid(shop, db_path, stripe, customer_id, stripe_customer)
    assert _licence_row(db_path, customer_id)["status"] == "active"
    assert _entitled(db_path, customer_id) == (True, True, 10, 10, 10)
    # Old order replays never take it away again.
    _replay_all(shop, stripe, ids, events, "c5post")
    assert _entitled(db_path, customer_id) == (True, True, 10, 10, 10)


# ------------------------------------------------------------------ appliance and plan-only accounts (no licence row)

def _exec(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _account(db_path, customer_id, *, appliance=False, plan="active", licence=None):
    """A seeded account: optional paid AnyAiCam appliance order, a per-camera plan, an optional vms_license row."""
    from test_pricing_ff_commission import _paid_appliance_order, _seed
    _seed(db_path, customer_id=customer_id, email=f"{customer_id}@example.test", cameras=0)
    if appliance:
        _paid_appliance_order(db_path, customer_id)
    if plan:
        _exec(db_path, "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,"
                       "stripe_subscription_id,stripe_subscription_item_id,stripe_price_id,status,local_retention_days,"
                       "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (f"v2-{customer_id}", customer_id, "ai_local", 6 if plan == "active" else 0, f"cus_{customer_id}",
               f"sub_{customer_id}", f"si_{customer_id}", PRICES["ai_local"], plan, 7, "2026-10-01", "2026-10-01"))
    if licence:
        _exec(db_path, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,suspended_reason,"
                       "created_at,updated_at) VALUES(?,?,'vms_license',8,'suspended',?,?,?)",
              (f"lic-{customer_id}", customer_id, licence, "2026-10-01", "2026-10-01"))


@pytest.mark.parametrize("licence", [None, "refunded", "disputed"])
def test_appliance_customer_plan_capacity_is_unchanged(shop, db_path, licence):
    """An AnyAiCam appliance includes the licence: its plan capacity is unchanged, even beside a refunded or disputed
    stand-alone licence; an ended plan still activates the account (resubscription in My subscription)."""
    _account(db_path, "cust-app", appliance=True, licence=licence)
    assert _entitled(db_path, "cust-app") == (True, True, 6, 6, 6)
    _exec(db_path, "UPDATE camera_plan_entitlements_v2 SET status='cancelled',camera_quantity=0 WHERE customer_id='cust-app'")
    import per_camera_billing
    with override_target(sqlite_path=str(db_path)):
        assert per_camera_billing.account_activated("cust-app") is True


@pytest.mark.parametrize("plan", ["active", "cancelled"])
def test_account_without_a_licence_row_keeps_the_billing_v2_plan_rule(shop, db_path, plan):
    """No licence was ever bought or refunded (billing-v2 plan-only accounts, outside C5; Option B B3 is deferred)."""
    _account(db_path, "cust-plan", plan=plan)
    expected = (True, True, 6, 6, 6) if plan == "active" else (True, False, 0, 0, 0)
    assert _entitled(db_path, "cust-plan") == expected


def test_refunded_licence_seeded_without_appliance_gets_no_plan_capacity(shop, db_path):
    """The reader rule on its own (no webhooks): an own-PC account with a refunded licence and an active plan."""
    _account(db_path, "cust-own", licence="refunded")
    assert _entitled(db_path, "cust-own") == (True, False, 0, 0, 0)
    _exec(db_path, "UPDATE camera_plan_entitlements_v2 SET status='cancelled',camera_quantity=0 WHERE customer_id='cust-own'")
    assert _entitled(db_path, "cust-own") == (False, False, 0, 0, 0)
    # An active licence row of the same account (e.g. a won dispute or a new order) restores everything.
    _exec(db_path, "UPDATE customer_entitlements SET status='active',suspended_reason=NULL WHERE customer_id='cust-own'")
    assert _access(db_path, "cust-own") == (True, True)
