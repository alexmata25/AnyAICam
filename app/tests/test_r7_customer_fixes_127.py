"""Green 2026-10-21: customer-facing corrections after the R7 review (GREEN-R7-CUSTOMER-FACING-REVIEW-1.2.7.md), owner-
directed, on top of R7 7f512554:

N1  the licence panel names the real reason: a refund or an open dispute is not "until billing is resolved";
C3  an order that can no longer be checked out shows where to go (My subscription / support), never a dead-end button;
C2  no "Payment complete -- set up your system" banner after a refund or during a dispute;
C1  one order-confirmation e-mail per ORDER: the new order after a refunded-and-ended one gets its own; the first order's
    key is unchanged, so an account that was already confirmed is never confirmed twice.

Real routes and webhook handling through the R7 fixtures (test_r7_repurchase_after_refund_127.py).
"""
import pytest

from database_backend import override_target
from test_r7_repurchase_after_refund_127 import (NO_ORDER, SECOND_ORDER_QUERY, _all, _checkout, _end_order1, _new_order,  # noqa: F401
                                                 _ok, _order, _order1_paid, _order2_paid, _plan_row, _r7_ready,
                                                 _refund_order1, _replay_all)
from test_r7_repurchase_after_refund_127 import (PRICES, _login, _one, _signed_in, db_path, license_portal, package,  # noqa: F401
                                                 portal, shop, site, storage, stripe)

pytestmark = [pytest.mark.real_activation, pytest.mark.usefixtures("legacy_own_pc_checkout")]  # own-PC orders in their pre-2026-10-22 subscription shape (conftest legacy_own_pc_checkout)

KEY = "build-order-confirmed:"


def _dispute_order1(shop, ids):
    from test_refund_before_record_race_127 import _dispute
    _ok(shop[0], _dispute(ids, "created"))


def _banner(db_path, customer_id, path=""):
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        return billing.purchase_progress_banner(customer_id, path)


def _portal(shop):
    page = shop[0].get("/subscription-portal")
    assert page.status_code == 200, page.text[:300]
    return page.text


def _confirmations(db_path, customer_id):
    return _all(db_path, "SELECT stripe_event_id,status FROM provisioning_notifications WHERE stripe_event_id LIKE ? ORDER BY created_at",
                (KEY + customer_id + "%",))


# ------------------------------------------------------------------ N1: licence wording names the reason

def test_n1_refunded_plan_says_refunded_not_billing_problem(shop, db_path, stripe):
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "n1refund@example.test")
    _refund_order1(shop, db_path, stripe, customer_id, ids, events)
    text = _portal(shop)
    assert "suspended because the payment was refunded" in text and "Suspended (payment refunded)" in text
    assert "until billing is resolved" not in text and "Paused (billing problem)" not in text


def test_n1_disputed_plan_says_dispute_open(shop, db_path, stripe):
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "n1dispute@example.test")
    _dispute_order1(shop, ids)
    text = _portal(shop)
    assert "suspended while a payment dispute is open" in text and "Suspended (payment dispute open)" in text
    assert "until billing is resolved" not in text and "Paused (billing problem)" not in text


def test_n1_failed_payment_keeps_the_billing_wording(shop, db_path, stripe):
    import customer_entitlements as ce
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "n1failed@example.test")
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            # A plan-only account (no one-time licence row; e.g. a licence that came with the plan) whose renewal
            # failed: its licence follows the plan, so "until billing is resolved" is right. (An own-PC account keeps
            # its active one-time licence through a failed renewal and shows it as active.)
            db.execute("DELETE FROM customer_entitlements WHERE customer_id=? AND product='vms_license'", (customer_id,))
            db.execute("UPDATE camera_plan_entitlements_v2 SET status='suspended',suspended_reason='payment_failed' WHERE customer_id=?",
                       (customer_id,))
        summary = ce.vms_license_summary(customer_id)
    assert summary["status"] == "suspended" and summary["suspended_reason"] == "payment_failed"
    assert summary["label"].endswith("suspended until billing is resolved")
    assert "Paused (billing problem)" in _portal(shop)


def test_n1_active_licence_wording_is_unchanged(shop, db_path, stripe):
    _order1_paid(shop, db_path, stripe, "n1active@example.test")
    text = _portal(shop)
    assert "suspended because" not in text and "payment dispute" not in text and "Paused (billing problem)" not in text


# ------------------------------------------------------------------ C3: no dead-end checkout button

def test_c3_active_licence_whose_plan_ended_is_sent_to_my_subscription(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "c3ended@example.test")
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    page = _order(shop[0], SECOND_ORDER_QUERY)
    assert page.status_code == 200
    assert 'id="order-checkout"' not in page.text and 'id="order-license-owned-plan-ended"' in page.text
    assert 'href="/subscription-portal"' in page.text and "not charged again" in page.text
    response = _checkout(shop[0])  # the server refusal is unchanged (no double licence charge)
    assert response.status_code == 409 and response.json()["detail"] == NO_ORDER


def test_c3_disputed_order_whose_subscription_ended_points_to_support(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "c3dispute@example.test")
    _dispute_order1(shop, ids)
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    page = _order(shop[0], SECOND_ORDER_QUERY)
    assert 'id="order-checkout"' not in page.text and 'id="order-dispute-open"' in page.text
    assert "vms-support.html" in page.text
    response = _checkout(shop[0])  # C4: still blocked (owner policy)
    assert response.status_code == 409 and response.json()["detail"] == NO_ORDER


def test_c3_r7_refunded_and_ended_still_gets_a_new_order(shop, db_path, stripe):
    _r7_ready(shop, db_path, stripe, "c3r7@example.test")
    page, body = _new_order(shop)
    assert 'id="order-checkout"' in page.text and body.get("checkout_url")


# ------------------------------------------------------------------ C2: no stale "Payment complete" banner

def test_c2_refunded_and_ended_banner_offers_a_new_order(shop, db_path, stripe):
    customer_id, ids, events, _ = _r7_ready(shop, db_path, stripe, "c2ended@example.test")
    banner = _banner(db_path, customer_id, "/customer/dashboard")
    assert "Previous order refunded" in banner and 'href="/order-summary"' in banner and "Payment complete" not in banner
    assert _banner(db_path, customer_id, "/order-summary") == ""


def test_c2_refunded_plan_still_running_banner_names_the_refund(shop, db_path, stripe):
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "c2running@example.test")
    _refund_order1(shop, db_path, stripe, customer_id, ids, events)
    banner = _banner(db_path, customer_id, "/customer/dashboard")
    assert "Plan suspended" in banner and "refunded" in banner and "Payment complete" not in banner
    assert _banner(db_path, customer_id, "/subscription-portal") == ""


def test_c2_disputed_plan_banner_names_the_dispute(shop, db_path, stripe):
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "c2dispute@example.test")
    _dispute_order1(shop, ids)
    banner = _banner(db_path, customer_id, "/customer/dashboard")
    assert "Plan suspended" in banner and "dispute" in banner and "Payment complete" not in banner


def test_c2_paid_order_still_says_payment_complete(shop, db_path, stripe):
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "c2paid@example.test")
    assert "Payment complete" in _banner(db_path, customer_id, "/customer/dashboard")


# ------------------------------------------------------------------ C1: one confirmation per order

def test_c1_first_order_keeps_the_original_key(shop, db_path, stripe):
    customer_id, ids, events, _ = _order1_paid(shop, db_path, stripe, "c1first@example.test")
    rows = _confirmations(db_path, customer_id)
    assert [r["stripe_event_id"] for r in rows] == [KEY + customer_id], rows


def test_c1_the_new_order_after_a_refund_gets_its_own_confirmation_exactly_once(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe, "c1new@example.test")
    first = _confirmations(db_path, customer_id)
    assert [r["stripe_event_id"] for r in first] == [KEY + customer_id]
    _new_order(shop)
    ids2, events2 = _order2_paid(shop, db_path, stripe, customer_id, stripe_customer)
    rows = _confirmations(db_path, customer_id)
    keys = [r["stripe_event_id"] for r in rows]
    assert keys == [KEY + customer_id, KEY + customer_id + ":order-2"], rows
    sent_before = [r["status"] for r in rows]
    for name in ("checkout", "invoice", "created"):  # redelivered order-2 events: no second confirmation
        _ok(shop[0], events2[name])
    _replay_all(shop, stripe, ids, events, "c1")  # order-1 replays: nothing new either
    rows = _confirmations(db_path, customer_id)
    assert [r["stripe_event_id"] for r in rows] == keys and [r["status"] for r in rows] == sent_before, rows


def test_c1_retry_worker_reads_the_customer_from_both_key_forms():
    import order_funnel
    assert order_funnel._customer_from_confirmation_key(KEY + "abc123") == "abc123"
    assert order_funnel._customer_from_confirmation_key(KEY + "abc123:order-3") == "abc123"
