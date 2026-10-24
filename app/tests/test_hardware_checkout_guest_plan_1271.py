"""D-HW-PLAN-1 (Linux 1.2.7.1, owner-authorized corrective work; Codex final baseline review of b8c7a7ed, Green
GREEN-1271-TEST-DEFINITIONS.md section 6).

A guest appliance order placed with a camera plan (e.g. Hybrid x4) must keep that plan: when the buyer later proves the
order email (verified account + sign-in, or a sign-in from the order's own confirmation page), the guest order AND its
saved plan (plan, camera count, Price locked at checkout) are attached in ONE transaction under the payment's lock
(signin_purchase_attachment.finalize). The plan then starts only at appliance activation -- with the storage trial when
an administrator granted one, otherwise at the locked Price with the card saved at purchase.

  U-H1  guest Hybrid x4 + appliance paid -> verified sign-in -> deferred plan saved with the locked Price, in the SAME
        transaction as the order link, under the payment lock (rollback of either rolls back both);
  U-H2  duplicate webhook / second sign-in -> one deferred row, one attachment, one subscription;
  U-H3  refund/dispute before attachment -> no plan (a won dispute makes it attachable, a lost one never); refund
        after attachment -> the plan does not start at activation;
  U-H4  replay of an older checkout session never replaces a live plan.

Real routes and webhook steps with the Stripe doubles of test_build_order_billing / test_hybrid_build_system_flow; no
network.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_activation_billing_127 import _billing_event, _call, _dispute, _plan_row, _refund, one_time_payments  # noqa: F401
from test_build_order_billing import stripe_subscriptions  # noqa: F401
from test_direct_onboarding import _login, _one, _signed_in, _signup, _token  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _activate_appliance, db_path, license_portal, package, portal,  # noqa: F401
                                           shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

HYBRID_X4 = {"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "plan": "hybrid", "cameras": 4}
ADMIN = {"email": "ops@example.test", "role": "global_admin"}


@pytest.fixture()
def guest_shop(shop, stripe_subscriptions, monkeypatch):
    """The shop plus: Stripe answers which card paid a PaymentIntent (pm_<intent>), and line items are not read."""
    import build_orders
    import partner_portal
    # These journeys exercise cloud-account sign-in and deferred purchase
    # attachment. The Dell test host may have a persisted activation identity;
    # it must not make the website's login route take the appliance-only
    # delegated/offline-auth branch.
    monkeypatch.setattr(partner_portal, "_claimed_appliance", lambda: False)
    import main
    monkeypatch.setattr(build_orders, "_line_amounts", lambda session: {})
    inner_get = main.stripe_api_get
    reads = []

    def get(path):
        if path.startswith("/v1/payment_intents/"):
            intent = path.split("/")[3].split("?")[0]
            reads.append(intent)
            return {"id": intent, "object": "payment_intent", "payment_method": f"pm_{intent}"}
        return inner_get(path)
    monkeypatch.setattr(main, "stripe_api_get", get)
    inner_post, made = main.stripe_api_post, []

    def post(path, fields, idempotency_key=None):
        answer = inner_post(path, fields, idempotency_key=idempotency_key)
        if path == "/v1/checkout/sessions":  # Stripe-shaped ids: the order confirmation page only accepts cs_test_...
            made.append(1)
            answer = {**answer, "id": f"cs_test_guest{len(made):06d}"}
        return answer
    monkeypatch.setattr(main, "stripe_api_post", post)
    client, captured, mail, _, _ = shop
    return {"client": client, "captured": captured, "mail": mail, "subs": stripe_subscriptions, "intent_reads": reads}


def _guest_checkout(db_path, g, email, selection=HYBRID_X4):
    """A signed-out Build Your System appliance checkout (hardware_checkout.create_checkout, identity=None)."""
    import hardware_checkout
    with override_target(sqlite_path=str(db_path)):
        opened = hardware_checkout.create_checkout(selection, email=email, identity=None)
    fields = dict(g["captured"][-1])
    assert fields["mode"] == "payment" and "metadata[anyaicam_customer_id]" not in fields
    metadata = {key[len("metadata["):-1]: value for key, value in fields.items() if key.startswith("metadata[")}
    return opened["session_id"], metadata


def _paid(session_id, metadata, email, intent, *, event_id=None):
    return {"id": event_id or f"evt_{session_id}", "type": "checkout.session.completed", "livemode": False,
            "data": {"object": {"id": session_id, "mode": "payment", "payment_status": "paid", "status": "complete",
                                "customer": f"cus_{intent}", "payment_intent": intent, "customer_email": email,
                                "customer_details": {"email": email}, "metadata": metadata}}}


def _webhook(db_path, event):
    import hardware_checkout
    with override_target(sqlite_path=str(db_path)):
        return hardware_checkout.sync_from_stripe_event(event)


def _guest_order(db_path, g, email, intent, selection=HYBRID_X4):
    session_id, metadata = _guest_checkout(db_path, g, email, selection)
    event = _paid(session_id, metadata, email, intent)
    result = _webhook(db_path, event)
    assert result["status"] == "hardware_order_recorded" and result["customer_id"] is None
    return session_id, event


def _create_account(db_path, g, email):
    """Direct signup + email confirmation (attaches nothing by itself)."""
    client, mail = g["client"], g["mail"]
    assert _signup(client, email).status_code == 200
    assert client.get(f"/customer/verify-email?token={_token(mail, email)}").status_code == 200
    return _one(db_path, "SELECT id FROM customers WHERE lower(email)=?", (email,))["id"]


def _sign_in(g, email):
    response = _login(g["client"], email, customer_only=True)
    assert _signed_in(response)
    return response


def _checkout(db_path, session_id):
    return _one(db_path, "SELECT * FROM hardware_checkouts WHERE stripe_checkout_session_id=?", (session_id,))


def _order_owners(db_path, session_id):
    with sqlite3.connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT customer_id FROM hardware_orders WHERE stripe_checkout_session_id=?",
                                           (session_id,))}


def _count(db_path, sql, args=()):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, args).fetchone()[0]


def _activate(db_path, customer_id):
    import build_orders
    _activate_appliance(db_path, customer_id)
    return _call(db_path, build_orders.on_appliance_activated, customer_id)


def _expire_lease(db_path, session_id):
    import signin_purchase_attachment as spa
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE hardware_checkouts SET claimed_at=? WHERE stripe_checkout_session_id=? AND claim_token IS NOT NULL",
                     ((datetime.now() - timedelta(seconds=spa.CLAIM_LEASE_SECONDS + 5)).isoformat(), session_id))


# ------------------------------------------------------------------ U-H1: atomic attach of the order and its plan

def test_u_h1_guest_hybrid_x4_plan_attaches_with_the_order_and_starts_at_activation(shop, guest_shop, db_path):
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-h1@example.test", "pi_guest_h1")
    # The webhook keeps the card that paid (needed at activation) on the guest checkout; nothing is saved as a plan yet.
    assert _checkout(db_path, session_id)["stripe_payment_method_id"] == "pm_pi_guest_h1"
    assert _count(db_path, "SELECT COUNT(*) FROM deferred_storage_plans") == 0
    customer_id = _create_account(db_path, g, "guest-h1@example.test")
    assert _checkout(db_path, session_id)["customer_id"] is None                  # confirming the email attaches nothing
    assert _plan_row(db_path, customer_id) is None
    _sign_in(g, "guest-h1@example.test")
    assert _checkout(db_path, session_id)["customer_id"] == customer_id
    assert _order_owners(db_path, session_id) == {customer_id}
    plan = _plan_row(db_path, customer_id)
    assert plan is not None, "D-HW-PLAN-1: the guest's chosen plan was not saved when the order was attached"
    assert (plan["plan_key"], plan["camera_quantity"], plan["stripe_price_id"], plan["monthly_cents_per_camera"],
            plan["order_kind"], plan["state"], plan["checkout_session_id"], plan["stripe_customer_id"],
            plan["stripe_payment_method_id"]) == \
        ("hybrid", 4, PRICES["hybrid"], 1999, "appliance", "awaiting_activation", session_id, "cus_pi_guest_h1", "pm_pi_guest_h1")
    assert g["subs"]["created"] == []                                              # nothing monthly before activation
    assert _activate(db_path, customer_id)["status"] == "started"
    created = g["subs"]["created"]
    assert len(created) == 1
    assert (created[0]["items[0][price]"], created[0]["items[0][quantity]"], created[0]["customer"],
            created[0]["default_payment_method"]) == (PRICES["hybrid"], "4", "cus_pi_guest_h1", "pm_pi_guest_h1")
    assert _plan_row(db_path, customer_id)["state"] == "started"


def test_u_h1_the_plan_is_saved_in_the_attach_transaction_under_the_payment_lock(shop, guest_shop, db_path, monkeypatch):
    import billing_status
    import build_orders
    import partner_db
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-lock@example.test", "pi_guest_lock")
    customer_id = _create_account(db_path, g, "guest-lock@example.test")
    trace = []
    real_lock, real_record = billing_status.lock_payment, build_orders.record_deferred_plan

    def lock(db, keys):
        trace.append(("lock", id(db), tuple(keys)))
        return real_lock(db, keys)

    def record(*args, **kwargs):
        shared = partner_db._shared_connection.get()
        trace.append(("plan", id(shared) if shared is not None else None, id(kwargs["db"]) if kwargs.get("db") is not None else None))
        return real_record(*args, **kwargs)
    monkeypatch.setattr(billing_status, "lock_payment", lock)
    monkeypatch.setattr(build_orders, "record_deferred_plan", record)
    _sign_in(g, "guest-lock@example.test")
    plans = [t for t in trace if t[0] == "plan"]
    assert len(plans) == 1, f"D-HW-PLAN-1: the attach never saved the plan ({trace})"
    locks_before = trace[:trace.index(plans[0])]
    held = [t for t in locks_before if any("pi_guest_lock" in str(key) for key in t[2])]
    assert held, "the payment lock was not held when the plan was saved"
    assert plans[0][1] == held[-1][1], "the plan was not saved inside the attach transaction"
    assert plans[0][2] in (None, held[-1][1])
    assert _plan_row(db_path, customer_id)["camera_quantity"] == 4


def test_u_h1_a_failure_after_the_plan_rolls_back_the_order_link_and_a_later_sign_in_completes_both(shop, guest_shop, db_path,
                                                                                                   monkeypatch):
    import signin_purchase_attachment as spa
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-atomic@example.test", "pi_guest_atomic")
    customer_id = _create_account(db_path, g, "guest-atomic@example.test")
    working = spa.audit

    def broken(*args, **kwargs):
        raise RuntimeError("audit store unavailable")
    monkeypatch.setattr(spa, "audit", broken)
    _sign_in(g, "guest-atomic@example.test")                                       # the sign-in itself still works
    assert _checkout(db_path, session_id)["customer_id"] is None                   # nothing half-applied
    assert _order_owners(db_path, session_id) == {None}
    assert _plan_row(db_path, customer_id) is None
    monkeypatch.setattr(spa, "audit", working)
    _expire_lease(db_path, session_id)
    _sign_in(g, "guest-atomic@example.test")
    assert _checkout(db_path, session_id)["customer_id"] == customer_id
    assert _order_owners(db_path, session_id) == {customer_id}
    assert (_plan_row(db_path, customer_id)["plan_key"], _plan_row(db_path, customer_id)["camera_quantity"]) == ("hybrid", 4)


def test_u_h1_a_granted_storage_trial_starts_at_activation_on_the_saved_plan(shop, guest_shop, db_path):
    import storage_trial
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-trial@example.test", "pi_guest_trial")
    customer_id = _create_account(db_path, g, "guest-trial@example.test")
    _sign_in(g, "guest-trial@example.test")
    assert _plan_row(db_path, customer_id)["state"] == "awaiting_activation"
    _call(db_path, storage_trial.grant, customer_id, admin=ADMIN, reason="evaluating cloud storage")
    assert _activate(db_path, customer_id)["status"] == "started"
    created = g["subs"]["created"]
    assert len(created) == 1
    assert (created[0]["items[0][price]"], created[0]["items[0][quantity]"]) == (PRICES["hybrid"], "4")
    assert "trial_end" in created[0] and created[0]["cancel_at_period_end"] == "true"
    assert "default_payment_method" not in created[0]                             # a free trial charges nothing


def test_u_h1_the_attached_plan_is_shown_as_saved_and_never_sold_twice(shop, guest_shop, db_path):
    """After attachment the guest is in the same state as a signed-in appliance buyer: My subscription shows the saved
    plan as Pending Activation, a second plan checkout is refused, and My orders offers setup, not a plan picker."""
    g = guest_shop
    _guest_order(db_path, g, "guest-ui@example.test", "pi_guest_ui")
    customer_id = _create_account(db_path, g, "guest-ui@example.test")
    _sign_in(g, "guest-ui@example.test")
    portal = g["client"].get("/subscription-portal").text
    assert "Pending Activation" in portal and "$19.99 per camera/month ($79.96/month)" in portal
    refused = g["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 4})
    assert refused.status_code == 409
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE hardware_orders SET fulfillment_status='delivered' WHERE customer_id=?", (customer_id,))
    orders = g["client"].get("/customer/orders").text
    assert 'id="orders-saved-plan"' in orders and "Hybrid · 4 cameras" in orders and 'id="orders-choose-plan"' not in orders
    assert g["subs"]["created"] == []


def test_u_h1_the_order_page_sign_in_attaches_the_plan_too(shop, guest_shop, db_path):
    """The other proof of the order email: an existing, confirmed account signs in from the order's own page."""
    g = guest_shop
    customer_id = _create_account(db_path, g, "guest-page@example.test")           # account first, order after
    g["client"].cookies.clear()
    session_id, _ = _guest_order(db_path, g, "guest-page@example.test", "pi_guest_page")
    _sign_in(g, "guest-page@example.test")
    assert _checkout(db_path, session_id)["customer_id"] is None                   # paid after confirmation: not by email
    page = g["client"].get(f"/hardware-order/complete?session_id={session_id}")
    assert page.status_code == 200 and 'id="hw-view-order"' in page.text
    assert _order_owners(db_path, session_id) == {customer_id}
    plan = _plan_row(db_path, customer_id)
    assert plan and (plan["plan_key"], plan["camera_quantity"], plan["stripe_price_id"]) == ("hybrid", 4, PRICES["hybrid"])


# ------------------------------------------------------------------ U-H2: duplicates are idempotent

def test_u_h2_duplicate_webhooks_and_repeated_sign_ins_keep_one_plan_and_one_subscription(shop, guest_shop, db_path):
    import build_orders
    import signin_purchase_attachment as spa
    g = guest_shop
    session_id, event = _guest_order(db_path, g, "guest-dup@example.test", "pi_guest_dup")
    _webhook(db_path, event)                                                       # redelivered
    _webhook(db_path, {**event, "id": "evt_guest_dup_again"})                      # duplicated under another id
    customer_id = _create_account(db_path, g, "guest-dup@example.test")
    for _ in range(3):
        _sign_in(g, "guest-dup@example.test")
    _webhook(db_path, event)                                                       # replayed after the attachment
    assert _call(db_path, spa.attach_for_customer, customer_id, "guest-dup@example.test",
                 confirmed_at=datetime.now().isoformat()) == []
    assert _count(db_path, "SELECT COUNT(*) FROM deferred_storage_plans WHERE customer_id=?", (customer_id,)) == 1
    assert _count(db_path, "SELECT COUNT(*) FROM purchase_attachments WHERE source_table='hardware_checkouts'") == 1
    assert _checkout(db_path, session_id)["customer_id"] == customer_id and _order_owners(db_path, session_id) == {customer_id}
    plan = _plan_row(db_path, customer_id)
    assert (plan["plan_key"], plan["camera_quantity"], plan["stripe_price_id"], plan["state"]) == \
        ("hybrid", 4, PRICES["hybrid"], "awaiting_activation")
    assert _activate(db_path, customer_id)["status"] == "started"
    for _ in range(2):
        _call(db_path, build_orders.on_appliance_activated, customer_id)
        _call(db_path, build_orders.retry_storage_starts)
    assert len(g["subs"]["created"]) == 1


# ------------------------------------------------------------------ U-H3: refunds and disputes

def test_u_h3_refunded_before_attachment_attaches_nothing_and_never_starts(shop, guest_shop, db_path, one_time_payments):
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-refund@example.test", "pi_guest_refund")
    _billing_event(db_path, _refund("pi_guest_refund"))
    customer_id = _create_account(db_path, g, "guest-refund@example.test")
    _sign_in(g, "guest-refund@example.test")
    assert _checkout(db_path, session_id)["customer_id"] is None
    assert _plan_row(db_path, customer_id) is None
    assert _activate(db_path, customer_id)["status"] == "ignored"
    assert g["subs"]["created"] == []


def test_u_h3_disputed_before_attachment_waits_and_a_won_dispute_attaches_the_plan(shop, guest_shop, db_path, one_time_payments):
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-won@example.test", "pi_guest_won")
    _billing_event(db_path, _dispute("pi_guest_won", "created"))
    customer_id = _create_account(db_path, g, "guest-won@example.test")
    _sign_in(g, "guest-won@example.test")
    assert _checkout(db_path, session_id)["customer_id"] is None and _plan_row(db_path, customer_id) is None
    _billing_event(db_path, _dispute("pi_guest_won", "closed", status="won", event_id="evt_guest_won_closed"))
    _sign_in(g, "guest-won@example.test")                                          # attachable again at once
    assert _checkout(db_path, session_id)["customer_id"] == customer_id
    plan = _plan_row(db_path, customer_id)
    assert plan and (plan["plan_key"], plan["camera_quantity"]) == ("hybrid", 4)
    assert _activate(db_path, customer_id)["status"] == "started" and len(g["subs"]["created"]) == 1


def test_u_h3_a_lost_dispute_never_attaches(shop, guest_shop, db_path, one_time_payments):
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-lost@example.test", "pi_guest_lost")
    _billing_event(db_path, _dispute("pi_guest_lost", "created"))
    _billing_event(db_path, _dispute("pi_guest_lost", "closed", status="lost", event_id="evt_guest_lost_closed"))
    customer_id = _create_account(db_path, g, "guest-lost@example.test")
    _sign_in(g, "guest-lost@example.test")
    _expire_lease(db_path, session_id)
    _sign_in(g, "guest-lost@example.test")
    assert _checkout(db_path, session_id)["customer_id"] is None and _plan_row(db_path, customer_id) is None
    assert _activate(db_path, customer_id)["status"] == "ignored" and g["subs"]["created"] == []


def test_u_h3_refunded_after_attachment_the_plan_never_starts(shop, guest_shop, db_path, one_time_payments):
    g = guest_shop
    session_id, _ = _guest_order(db_path, g, "guest-late@example.test", "pi_guest_late")
    customer_id = _create_account(db_path, g, "guest-late@example.test")
    _sign_in(g, "guest-late@example.test")
    assert _plan_row(db_path, customer_id)["state"] == "awaiting_activation"
    _billing_event(db_path, _refund("pi_guest_late"))
    assert _activate(db_path, customer_id)["status"] == "ignored"
    assert g["subs"]["created"] == []


# ------------------------------------------------------------------ U-H4: replay of an older session

def test_u_h4_an_older_checkout_paid_or_replayed_later_never_replaces_the_live_plan(shop, guest_shop, db_path):
    g = guest_shop
    email = "guest-replay@example.test"
    older_id, older_meta = _guest_checkout(db_path, g, email, {**HYBRID_X4, "plan": "ai_local", "cameras": 2})
    newer_id, newer_event = _guest_order(db_path, g, email, "pi_guest_newer")
    customer_id = _create_account(db_path, g, email)
    _sign_in(g, email)
    live = _plan_row(db_path, customer_id)
    assert (live["plan_key"], live["camera_quantity"], live["checkout_session_id"]) == ("hybrid", 4, newer_id)
    # The older session completes (or its event is replayed) afterwards and is attached from its own order page.
    _webhook(db_path, _paid(older_id, older_meta, email, "pi_guest_older"))
    _webhook(db_path, newer_event)
    page = g["client"].get(f"/hardware-order/complete?session_id={older_id}")
    assert page.status_code == 200
    assert _checkout(db_path, older_id)["customer_id"] == customer_id              # the order itself is the account's
    assert _plan_row(db_path, customer_id) == live                                 # the live plan is untouched
    assert _activate(db_path, customer_id)["status"] == "started"
    created = g["subs"]["created"]
    assert len(created) == 1 and (created[0]["items[0][price]"], created[0]["items[0][quantity]"]) == (PRICES["hybrid"], "4")
