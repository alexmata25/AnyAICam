"""Three per-camera (billing v2) defects found in isolated acceptance with a
fake Stripe (2026-10-10), offline -- no live Stripe, no remote hosts.

1. A customer whose plan Stripe cancelled could not buy again: My
   subscription offered checkout, but POST /api/v2/customer/subscription/
   checkout answered 409 "already has a camera plan" for the kept,
   cancelled history row. Now only an active/suspended plan routes to plan
   change, and the webhook rebinds the account's one row to the new
   subscription without carrying the ended one's billing state over and
   without a late event for the ended one touching the new one.
3. A failed renewal (Stripe past_due, plan active in its 7-day grace) showed
   no warning and sent no email. My subscription now says so, with the grace
   deadline and an Update payment method action (Stripe billing portal); the
   once-per-invoice payment-failed email covers v2 plans; the "Payment
   complete -- Set up" prompts are not shown while payment has failed.
5. After a Hybrid plan ended (or moved to Basic Local) the appliance
   configuration still reported smart_motion_enabled=1. It now reports the
   effective value; the customer's stored choice is kept, so re-subscribing
   restores it.
"""
from datetime import datetime, timedelta, timezone
import secrets
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target
from test_stripe_billing_launch import OWNER_A, _cookie, _deliver, env  # noqa: F401  (env is a fixture)
from test_per_camera_stripe_lifecycle import (PRICES, SUB, _checkout_to_active, _event, _invoice, _ok, _state,  # noqa: F401
                                              v2)

NEW_SUB = "sub_v2_again"


@pytest.fixture()
def mail(monkeypatch):
    import purchase_notifications
    sent, failing = [], []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            if failing:
                raise OSError("smtp unavailable")
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text, "html": html or ""})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return {"sent": sent, "failing": failing}


def _db(env):
    conn = sqlite3.connect(env["path"])
    conn.row_factory = sqlite3.Row
    return conn


def _row(env, customer_id="cust-A"):
    with _db(env) as conn:
        found = conn.execute("SELECT * FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,)).fetchone()
    return dict(found) if found else None


def _cancel_in_stripe(env, sub):
    sub["status"] = "canceled"
    assert _deliver(env, _event("evt_deleted", "customer.subscription.deleted", sub)).status_code == 200
    assert _state(env)["status"] == "cancelled"


def _buy_again(env, plan_key, quantity, *, sub_id=NEW_SUB, event_id="evt_checkout_again"):
    """A second checkout through the real route, then Stripe's verified
    checkout.session.completed for the new subscription."""
    started = _ok(env["client"].post("/api/v2/customer/subscription/checkout",
                                     json={"plan_key": plan_key, "camera_quantity": quantity}))
    fields = env["sessions"][started["session_id"]]["fields"]
    price_id = PRICES[plan_key][0]
    sub = {"id": sub_id, "object": "subscription", "customer": fields["customer"], "status": "active",
           "current_period_end": 1_896_134_400, "cancel_at_period_end": False, "latest_invoice": f"in_first_{sub_id}",
           "schedule": None, "items": {"data": [{"id": f"si_{sub_id}", "price": {"id": price_id}, "quantity": quantity}]},
           "metadata": {"anyaicam_customer_id": "cust-A", "anyaicam_billing_version": "2", "anyaicam_plan_key": plan_key,
                        "anyaicam_stripe_price_id": price_id}}
    env["stripe"][sub_id] = sub
    session = {"id": started["session_id"], "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
               "customer": sub["customer"], "subscription": sub_id, "client_reference_id": "cust-A",
               "metadata": {k[len("metadata["):-1]: v for k, v in fields.items() if k.startswith("metadata[")}}
    env["paid_sessions"].add(started["session_id"])
    assert _deliver(env, _event(event_id, "checkout.session.completed", session)).status_code == 200
    return sub


# ======================================================== 1. buy again after cancellation

def test_a_cancelled_customer_can_check_out_again_and_the_new_subscription_is_bound(v2):
    env = v2
    old = _checkout_to_active(env, "hybrid", 3)
    with _db(env) as conn:  # the ended subscription had a pending renewal change and a failed payment
        conn.execute("UPDATE camera_plan_entitlements_v2 SET scheduled_plan_key='basic_local',scheduled_camera_quantity=1,"
                     "scheduled_at=1893456000,payment_failed_at='2026-10-01T00:00:00' WHERE customer_id='cust-A'")
    _cancel_in_stripe(env, old)
    assert _state(env)["capacity"] == 0

    page = env["client"].get("/subscription-portal").text
    assert 'id="v2-checkout-button"' in page  # what the page offers ...
    _buy_again(env, "ai_local", 2)  # ... the route now accepts
    row = _row(env)
    assert row["stripe_subscription_id"] == NEW_SUB and row["status"] == "active"
    assert _state(env) == {"plan": "ai_local", "status": "active", "quantity": 2, "capacity": 2, "mode": "local"}
    # The ended subscription's own billing state never carries over.
    assert row["scheduled_plan_key"] is None and row["scheduled_camera_quantity"] is None and row["scheduled_at"] is None
    assert row["payment_failed_at"] is None and row["suspended_reason"] is None and row["cancel_at_period_end"] == 0
    with _db(env) as conn:  # still one row per account
        assert conn.execute("SELECT COUNT(*) FROM camera_plan_entitlements_v2 WHERE customer_id='cust-A'").fetchone()[0] == 1


def test_late_events_for_the_ended_subscription_never_touch_the_new_one(v2):
    env = v2
    old = _checkout_to_active(env, "hybrid", 3)
    _cancel_in_stripe(env, old)
    _buy_again(env, "basic_local", 4)
    before = _row(env)
    # Stripe redelivers the old subscription's events (its current state: canceled).
    _deliver(env, _event("evt_old_deleted_again", "customer.subscription.deleted", old))
    _deliver(env, _event("evt_old_updated", "customer.subscription.updated", dict(old, status="active")))
    failed = _invoice("in_old_late", old, "hybrid", 3)
    failed["amount_paid"] = 0
    env["invoices"]["in_old_late"] = dict(failed, status="open")
    _deliver(env, _event("evt_old_failed", "invoice.payment_failed", failed))
    after = _row(env)
    assert after["stripe_subscription_id"] == NEW_SUB and after["status"] == "active" and after["plan_key"] == "basic_local"
    assert after["camera_quantity"] == 4 and after["payment_failed_at"] is None
    assert (before["plan_key"], before["camera_quantity"]) == (after["plan_key"], after["camera_quantity"])


@pytest.mark.parametrize("status", ["active", "suspended"])
def test_a_current_plan_still_routes_to_plan_change(v2, status):
    env = v2
    _checkout_to_active(env, "ai_local", 2)
    with _db(env) as conn:
        conn.execute("UPDATE camera_plan_entitlements_v2 SET status=?,suspended_reason=? WHERE customer_id='cust-A'",
                     (status, "payment_failed" if status == "suspended" else None))
    sessions = len(env["sessions"])
    response = env["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 2})
    assert response.status_code == 409 and "plan-change" in response.json()["detail"]
    assert len(env["sessions"]) == sessions


def test_a_live_legacy_camera_plan_still_blocks_a_second_checkout(v2):
    env = v2
    with _db(env) as conn:
        conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,"
                     "created_at,updated_at) VALUES('ce-legacy','cust-A','camera_slots_local',8,'active',"
                     "'2026-10-01','2026-10-01')")
    response = env["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 2})
    assert response.status_code == 409


def test_every_reader_treats_a_cancelled_plan_as_not_entitled(v2):
    env = v2
    old = _checkout_to_active(env, "hybrid", 3)
    _cancel_in_stripe(env, old)
    import customer_entitlements as ce
    import event_media_policy
    import local_storage_policy
    import per_camera_billing as billing
    from analytics_entitlements import account_wide_feature_active
    from partner_db import connection
    assert not billing.has_camera_plan("cust-A") and not billing.order_paid("cust-A")
    assert billing.active_quantity("cust-A") == 0 and billing.product_mode("cust-A") == ""
    assert billing.vms_license_capacity("cust-A") == 0
    assert ce.usable_camera_capacity("cust-A") == 0 and ce.product_mode_for_customer("cust-A") == ""
    with override_target(sqlite_path=str(env["path"])):
        with connection() as db:
            assert event_media_policy.motion_event_policy(db, "cust-A") is None
            assert event_media_policy.effective_cloud_recording_mode(db, "cust-A", None) == "disabled"
            assert event_media_policy.effective_cloud_recording_mode(db, "cust-A", "motion") == "disabled"
            assert not account_wide_feature_active(db, "cust-A", "smart_motion")
            assert local_storage_policy.local_storage_policy_for_customer(db, "cust-A")["local_retention_days"] is None


# ======================================================== 3. failed renewal: warning and email

def _fail_renewal(env, sub, plan_key, quantity, invoice_id="in_renew_fail"):
    failed = _invoice(invoice_id, sub, plan_key, quantity)
    failed["amount_paid"] = 0
    env["invoices"][invoice_id] = dict(failed, status="open")
    sub["status"] = "past_due"
    assert _deliver(env, _event(f"evt_fail_{invoice_id}", "invoice.payment_failed", failed)).status_code == 200
    assert _deliver(env, _event(f"evt_past_due_{invoice_id}", "customer.subscription.updated", sub)).status_code == 200
    return failed


def test_grace_shows_a_payment_failed_banner_with_deadline_and_update_action(v2, mail):
    env = v2
    sub = _checkout_to_active(env, "hybrid", 3)
    healthy = env["client"].get("/subscription-portal").text
    assert 'id="v2-setup-next"' in healthy and 'id="v2-payment-failed"' not in healthy
    _fail_renewal(env, sub, "hybrid", 3)
    row = _row(env)
    assert row["status"] == "active" and row["stripe_status"] == "past_due" and row["payment_failed_at"]
    html = env["client"].get("/subscription-portal").text
    assert 'id="v2-payment-failed"' in html and "Payment failed" in html
    assert 'id="v2-update-payment"' in html and "Update payment method" in html
    grace_end = datetime.fromisoformat(row["payment_failed_at"]) + timedelta(days=7)
    shown = grace_end.astimezone(timezone.utc)
    assert shown.strftime("%b %d, %Y") in html  # the deadline is shown
    # No stale "Payment complete -- Set up your AnyAiCam system" while payment has failed.
    assert 'id="v2-setup-next"' not in html and "Payment complete" not in html
    # The action opens the Stripe billing portal (fake).
    opened = env["client"].post("/api/customer/billing-portal")
    assert opened.status_code == 200 and opened.json()["url"].startswith("https://billing.stripe.test/")


def test_a_viewer_sees_the_warning_without_the_owner_action(v2):
    env = v2
    sub = _checkout_to_active(env, "ai_local", 2)
    _fail_renewal(env, sub, "ai_local", 2)
    env["client"].cookies.clear()
    env["client"].cookies.update(_cookie("viewer@example.test", "customer_viewer", "cust-A"))
    html = env["client"].get("/subscription-portal").text
    assert 'id="v2-payment-failed"' in html and 'id="v2-update-payment"' not in html
    assert "Ask the account owner" in html


def test_payment_recovery_removes_the_warning(v2):
    env = v2
    sub = _checkout_to_active(env, "hybrid", 2)
    _fail_renewal(env, sub, "hybrid", 2, "in_recover")
    env["invoices"]["in_recover"] = dict(_invoice("in_recover", sub, "hybrid", 2), status="paid")
    sub["status"] = "active"
    _deliver(env, _event("evt_recovered", "invoice.paid", _invoice("in_recover", sub, "hybrid", 2)))
    _deliver(env, _event("evt_active_again", "customer.subscription.updated", sub))
    assert _row(env)["payment_failed_at"] is None
    assert 'id="v2-payment-failed"' not in env["client"].get("/subscription-portal").text


def test_after_grace_the_paused_plan_keeps_the_update_payment_action(v2):
    import billing_status
    env = v2
    sub = _checkout_to_active(env, "hybrid", 2)
    _fail_renewal(env, sub, "hybrid", 2)
    with override_target(sqlite_path=str(env["path"])):
        billing_status.sweep_grace(datetime.now() + timedelta(days=8))
    assert _row(env)["status"] == "suspended"
    html = env["client"].get("/subscription-portal").text
    assert 'id="v2-payment-failed"' in html and "Plan paused" in html and 'id="v2-update-payment"' in html


def test_a_v2_failed_renewal_is_emailed_once_per_invoice_and_retried(v2, mail):
    import purchase_notifications
    env = v2
    sub = _checkout_to_active(env, "hybrid", 3)
    mail["failing"].append(True)
    failed = _fail_renewal(env, sub, "hybrid", 3, "in_v2_fail")
    assert not [m for m in mail["sent"] if m["type"] == "payment_failed"]
    mail["failing"].clear()
    with override_target(sqlite_path=str(env["path"])):
        assert purchase_notifications.retry_payment_failed_notifications() == 1
        assert purchase_notifications.retry_payment_failed_notifications() == 0
    for n in range(2):  # Stripe retries the charge and resends the event
        _deliver(env, _event(f"evt_fail_again_{n}", "invoice.payment_failed", failed))
    emails = [m for m in mail["sent"] if m["type"] == "payment_failed"]
    assert len(emails) == 1 and emails[0]["to"] == "a@example.test"
    assert "Hybrid plan" in emails[0]["text"] and "/subscription-portal" in emails[0]["text"]
    grace_end = datetime.fromisoformat(_row(env)["payment_failed_at"]) + timedelta(days=7)
    assert f"continues until {grace_end.strftime('%B %d, %Y')}" in emails[0]["text"]
    with _db(env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provisioning_notifications WHERE stripe_event_id='payment-failed:in_v2_fail' "
                            "AND status='sent'").fetchone()[0] == 1


def test_a_cancelled_v2_plan_is_never_sent_a_payment_failed_email(v2, mail):
    import purchase_notifications
    env = v2
    sub = _checkout_to_active(env, "hybrid", 3)
    _cancel_in_stripe(env, sub)
    with override_target(sqlite_path=str(env["path"])):
        from partner_db import connection
        with connection() as db:
            db.execute("INSERT INTO billing_invoice_states(invoice_id,subscription_id,state,updated_at) "
                       "VALUES('in_after_cancel',?,'failed','2026-10-10')", (SUB,))
        assert purchase_notifications.notify_payment_failed("in_after_cancel")["status"] == "ignored"
    assert not mail["sent"]


def test_the_shell_banner_warns_instead_of_payment_complete(v2):
    import per_camera_billing as billing
    env = v2
    sub = _checkout_to_active(env, "hybrid", 2)
    with override_target(sqlite_path=str(env["path"])):
        billing.save_build_selection("cust-A", {"plan": "hybrid", "cameras": 2})  # left alone: already paid
        from partner_db import connection
        with connection() as db:
            db.execute("INSERT INTO build_system_intents(customer_id,plan_key,camera_quantity,own_pc,state,created_at,updated_at) "
                       "VALUES('cust-A','hybrid',2,1,'paid_setup_pending','2026-10-10','2026-10-10')")
        assert "Payment complete" in billing.purchase_progress_banner("cust-A", "/customer-portal")
    _fail_renewal(env, sub, "hybrid", 2)
    with override_target(sqlite_path=str(env["path"])):
        banner = billing.purchase_progress_banner("cust-A", "/customer-portal")
        assert "Payment failed" in banner and "Payment complete" not in banner
        assert 'href="/subscription-portal"' in banner and "Update payment method" in banner
        assert billing.purchase_progress_banner("cust-A", "/subscription-portal") == ""  # the page has its own notice


def test_payment_problem_unit():
    import per_camera_billing as billing
    assert billing.payment_problem(None) is None
    assert billing.payment_problem({"status": "active", "stripe_status": "active"}) is None
    assert billing.payment_problem({"status": "cancelled", "payment_failed_at": "2026-10-01T00:00:00"}) is None
    assert billing.payment_problem({"status": "suspended", "suspended_reason": "refunded"}) is None
    grace = billing.payment_problem({"status": "active", "stripe_status": "past_due", "payment_failed_at": "2026-10-01T08:00:00"})
    assert grace == {"state": "grace", "grace_ends_at": datetime(2026, 10, 8, 8, 0)}
    assert billing.payment_problem({"status": "suspended", "suspended_reason": "payment_failed"})["state"] == "suspended"


# ======================================================== 5. Smart Motion follows the plan at the edge

with override_target(sqlite_path="/tmp/test_billing_v2_resubscribe_grace_smart_motion.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance(db):
    now = "2026-10-10T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (now,))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) "
               "VALUES('cust-1','partner-1','C','c@example.test','active','real',?)", (now,))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S',?)", (now,))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST0001',?)", (now,))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-1','appl-1',?,?)",
               (password_hash("test-credential"), now))
    for n, flags in ((1, (1, 1, 1, 1)), (2, (None, None, None, None))):
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,smart_motion_enabled,"
                   "people_counting_enabled,lpr_enabled,ppe_enabled,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'online',?)",
                   (f"cam-{n}", "cust-1", "site-1", "appl-1", f"Camera {n}", n, *flags, now))


def _v2_plan(db, plan_key="hybrid", status="active"):
    cloud = 1 if plan_key == "hybrid" else 0
    db.execute("DELETE FROM camera_plan_entitlements_v2 WHERE customer_id='cust-1'")
    db.execute("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
               "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,cloud_event_storage,"
               "cloud_event_retention_days,created_at,updated_at) VALUES('v2-1','cust-1',?,2,'cus_t','sub_t','si_t','price_t',?,"
               "?,7,?,?,'2026-10-10','2026-10-10')",
               (plan_key, status, "canceled" if status == "cancelled" else "active", cloud, 14 if cloud else None))


@pytest.fixture()
def appliance(tmp_path):
    path = tmp_path / "smart_motion_config.db"
    with override_target(sqlite_path=str(path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed_appliance(db)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as client:
            def config():
                headers = {"X-Appliance-Id": "appl-1", "X-Request-Timestamp": str(int(time.time())),
                           "X-Request-Nonce": secrets.token_hex(16), "Authorization": "Bearer test-credential"}
                response = client.get("/api/appliance/configuration", headers=headers)
                assert response.status_code == 200, response.text
                return {c["id"]: c for c in response.json()["cameras"]}

            def plan(plan_key="hybrid", status="active"):
                with connection() as db:
                    _v2_plan(db, plan_key, status)
            yield {"config": config, "plan": plan, "path": path}


def _stored(appliance, camera_id="cam-1"):
    with connection() as db:
        return dict(db.execute("SELECT smart_motion_enabled,people_counting_enabled,lpr_enabled,ppe_enabled FROM cameras WHERE id=?",
                               (camera_id,)).fetchone())


def test_smart_motion_is_reported_only_while_the_plan_includes_it(appliance):
    appliance["plan"]("hybrid", "active")
    assert appliance["config"]()["cam-1"]["smart_motion_enabled"] == 1
    appliance["plan"]("hybrid", "cancelled")
    cameras = appliance["config"]()
    assert cameras["cam-1"]["smart_motion_enabled"] == 0
    assert cameras["cam-2"]["smart_motion_enabled"] is None  # never enabled: unchanged
    # The customer's preference is kept ...
    assert _stored(appliance)["smart_motion_enabled"] == 1
    # ... so buying again restores it.
    appliance["plan"]("ai_local", "active")
    assert appliance["config"]()["cam-1"]["smart_motion_enabled"] == 1


def test_a_downgrade_to_basic_local_turns_smart_motion_off_at_the_edge(appliance):
    appliance["plan"]("basic_local", "active")
    assert appliance["config"]()["cam-1"]["smart_motion_enabled"] == 0
    appliance["plan"]("hybrid", "suspended")
    assert appliance["config"]()["cam-1"]["smart_motion_enabled"] == 0
    assert _stored(appliance)["smart_motion_enabled"] == 1


def test_add_on_and_admin_analytics_are_not_plan_gated(appliance):
    appliance["plan"]("hybrid", "cancelled")
    camera = appliance["config"]()["cam-1"]
    assert (camera["people_counting_enabled"], camera["lpr_enabled"], camera["ppe_enabled"]) == (1, 1, 1)


def test_an_existing_smart_motion_licence_still_counts(appliance):
    appliance["plan"]("basic_local", "active")
    with connection() as db:
        db.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at) "
                   "VALUES('as-1','cust-1',NULL,'smart_motion','active',1,'2026-09-01')")
    assert appliance["config"]()["cam-1"]["smart_motion_enabled"] == 1


def test_a_customer_outside_billing_v2_keeps_the_stored_value(appliance):
    assert appliance["config"]()["cam-1"]["smart_motion_enabled"] == 1  # no v2 row: legacy / partner-managed


def test_effective_camera_analytic_unit(appliance):
    from analytics_entitlements import effective_camera_analytic
    with connection() as db:
        assert effective_camera_analytic(db, "cust-1", "site-1", "smart_motion", None) is None
        assert effective_camera_analytic(db, "cust-1", "site-1", "smart_motion", 0) == 0
        assert effective_camera_analytic(db, "cust-1", "site-1", "smart_motion", 1) == 1  # no v2 row
        _v2_plan(db, "hybrid", "cancelled")
    with connection() as db:
        cache: dict = {}
        assert effective_camera_analytic(db, "cust-1", "site-1", "smart_motion", 1, cache) == 0
        assert effective_camera_analytic(db, "cust-1", "site-1", "lpr", 1, cache) == 1
        assert effective_camera_analytic(db, "cust-1", "site-1", "people_counting", 1, cache) == 1
        assert list(cache.values()) == [False]
