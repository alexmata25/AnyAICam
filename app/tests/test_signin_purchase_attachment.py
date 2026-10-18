"""Fenced, transactional purchase attachment after sign-in (Green 2026-10-17, post-RC5; Codex review blockers 1-3 and
the owner's required regression cases). See signin_purchase_attachment.py.
"""
import inspect
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import customer_entitlements as ce
import signin_purchase_attachment as spa
from test_direct_onboarding import PASSWORD, _login, _signup, _token, site  # noqa: F401 -- fixture
from test_pricing_ff_commission import db_path, license_portal, portal  # noqa: F401 -- fixtures

LINKS = "pending_customer_links"


@pytest.fixture()
def notices(monkeypatch):
    """The "added to your account" emails (purchase_notifications binds get_email_service at import)."""
    import purchase_notifications
    sent, failing = [], []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            if failing:
                raise OSError("smtp unavailable")
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return {"sent": sent, "failing": failing}


def _q(db_path, sql, args=()):
    with sqlite3.connect(db_path) as db:
        return db.execute(sql, args).fetchall()


def _customer_id(db_path, email):
    return _q(db_path, "SELECT id FROM customers WHERE lower(email)=?", (email,))[0][0]


def _verified_owner(client, mail, db_path, email):
    _signup(client, email)
    assert client.get(f"/customer/verify-email?token={_token(mail, email)}").status_code == 200
    return _customer_id(db_path, email)


def _buy_as_guest(email, event_id="evt_guest"):
    ce.create_pending_link(email=email, product="professional", camera_slot_quantity=10, raw_event={"id": event_id})
    return _q_link(email)


def _q_link(email):
    import partner_db
    found = partner_db.row(f"SELECT id FROM {LINKS} WHERE normalized_email=?", (email.lower(),))
    return found["id"] if found else None


def _counts(db_path, customer_id):
    return {"slots": ce.total_camera_slots(customer_id),
            "attachments": _q(db_path, "SELECT COUNT(*) FROM purchase_attachments")[0][0],
            "audits": _q(db_path, "SELECT COUNT(*) FROM audit_logs WHERE action='signin.purchase_attached'")[0][0]}


def _link_state(db_path):
    return _q(db_path, f"SELECT status,resolved_customer_id FROM {LINKS}")[0]


# ------------------------------------------------------------------ blocker 1: only after a successful sign-in

def test_a_purchase_pending_before_email_verification_stays_pending_until_sign_in(site, db_path, notices):
    client, _, mail = site
    _buy_as_guest("early@example.test", "evt_before_verify")           # bought before the account existed
    customer_id = _verified_owner(client, mail, db_path, "early@example.test")
    assert _link_state(db_path)[0] == "pending"                          # confirmation attached nothing
    assert _counts(db_path, customer_id) == {"slots": 0, "attachments": 0, "audits": 0}
    assert _login(client, "early@example.test", customer_only=True).status_code < 400
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}
    assert _link_state(db_path) == ("resolved", customer_id)
    assert [n["type"] for n in notices["sent"]] == ["purchase_attached"]


def test_registration_approval_attaches_nothing_and_the_owner_s_sign_in_does(site, db_path, notices):
    import customer_registration as cr
    client, _, mail = site
    _buy_as_guest("approved@example.test", "evt_before_approval")
    cr.create_pending_registration("Approved Owner", "approved@example.test", PASSWORD)
    assert cr.verify_registration_email(cr.issue_registration_verification("approved@example.test", force=True),
                                        PASSWORD, "Approved Owner")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) "
                   "VALUES('partner-a','Partner A','approved','real','now')")
    request_id = _q(db_path, "SELECT id FROM customer_registration_requests WHERE email='approved@example.test'")[0][0]
    result = cr.approve_registration(request_id, "partner-a", {"master": True, "email": "m@example.test", "partner_id": None})
    assert _link_state(db_path)[0] == "pending"
    assert _counts(db_path, result["customer_id"])["slots"] == 0
    assert _login(client, "approved@example.test", customer_only=True).status_code < 400
    assert _counts(db_path, result["customer_id"]) == {"slots": 10, "attachments": 1, "audits": 1}


def test_an_unconfirmed_partner_created_account_never_collects_a_purchase(site, db_path, notices):
    from partner_db import authenticate_detailed, password_hash
    client, _, mail = site
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) "
                   "VALUES('cust-partner','house','Partner Made','victim@example.test','active','real','now')")
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,"
                   "account_status) VALUES('u-partner','house','victim@example.test','Partner Made','customer_owner',?,1,"
                   "'cust-partner','now','active')", (password_hash(PASSWORD),))
    _buy_as_guest("victim@example.test", "evt_victim")
    _login(client, "victim@example.test", customer_only=True)
    user, reason = authenticate_detailed("victim@example.test", PASSWORD)
    assert reason == "ok"
    assert spa.attach_after_signin(user) == {"status": "skipped", "reason": "the account's address was never confirmed"}
    assert _link_state(db_path)[0] == "pending" and _counts(db_path, "cust-partner")["attachments"] == 0


def test_a_failed_sign_in_and_another_address_attach_nothing(site, db_path, notices):
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "me@example.test")
    _buy_as_guest("someone-else@example.test", "evt_other")
    assert _login(client, "me@example.test", password="wrong-password-123", customer_only=True).status_code >= 400
    assert _login(client, "me@example.test", customer_only=True).status_code < 400
    assert _counts(db_path, customer_id) == {"slots": 0, "attachments": 0, "audits": 0}
    assert _link_state(db_path)[0] == "pending"


# ------------------------------------------------------------------ blocker 2: fenced claims, competing workers, replay

def test_competing_workers_one_live_claim_wins_and_a_replay_cannot_attach_twice(site, db_path, notices):
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "race@example.test")
    link_id = _buy_as_guest("race@example.test", "evt_race")
    token_a = spa.claim(LINKS, link_id, "race@example.test")
    assert token_a and spa.claim(LINKS, link_id, "race@example.test") is None          # B: A's lease is live
    spa.finalize("plan", LINKS, link_id, token_a, customer_id=customer_id, email="race@example.test", label="camera plan")
    with pytest.raises(spa.FenceLost):                                                 # A again: a replay
        spa.finalize("plan", LINKS, link_id, token_a, customer_id=customer_id, email="race@example.test", label="camera plan")
    assert spa.claim(LINKS, link_id, "race@example.test") is None                      # resolved: nothing to claim
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}


def test_lease_takeover_fences_out_the_original_worker(site, db_path, notices):
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "slow@example.test")
    link_id = _buy_as_guest("slow@example.test", "evt_slow")
    long_ago = datetime.now() - timedelta(seconds=spa.CLAIM_LEASE_SECONDS + 30)
    token_a = spa.claim(LINKS, link_id, "slow@example.test", now=long_ago)           # A claimed, then stalled
    token_b = spa.claim(LINKS, link_id, "slow@example.test")                          # B takes over the expired lease
    assert token_a and token_b and token_a != token_b
    with pytest.raises(spa.FenceLost):                                                 # A wakes up: fenced, grants nothing
        spa.finalize("plan", LINKS, link_id, token_a, customer_id=customer_id, email="slow@example.test", label="camera plan")
    assert _counts(db_path, customer_id) == {"slots": 0, "attachments": 0, "audits": 0}
    assert _q(db_path, f"SELECT claim_token FROM {LINKS}")[0][0] == token_b          # B's claim was not removed
    spa.finalize("plan", LINKS, link_id, token_b, customer_id=customer_id, email="slow@example.test", label="camera plan")
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}


def test_repeated_sign_ins_attach_once(site, db_path, notices):
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "twice@example.test")
    _buy_as_guest("twice@example.test", "evt_twice")
    for _ in range(3):
        assert _login(client, "twice@example.test", customer_only=True).status_code < 400
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}
    assert len(notices["sent"]) == 1


# ------------------------------------------------------------------ blocker 3: transition + grant + audit are one unit

def test_an_audit_failure_rolls_back_the_grant_and_a_later_sign_in_completes_it(site, db_path, notices, monkeypatch):
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "audit@example.test")
    _buy_as_guest("audit@example.test", "evt_audit")

    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit store unavailable")
    working_audit = spa.audit
    monkeypatch.setattr(spa, "audit", broken_audit)
    assert _login(client, "audit@example.test", customer_only=True).status_code < 400   # sign-in itself still works
    assert _counts(db_path, customer_id) == {"slots": 0, "attachments": 0, "audits": 0}   # nothing half-applied
    assert _link_state(db_path)[0] == "attaching" and notices["sent"] == []
    monkeypatch.setattr(spa, "audit", working_audit)
    assert _login(client, "audit@example.test", customer_only=True).status_code < 400   # lease still live: skipped
    assert _counts(db_path, customer_id)["attachments"] == 0
    with sqlite3.connect(db_path) as db:                                                 # lease expires
        db.execute(f"UPDATE {LINKS} SET claimed_at=?", ((datetime.now() - timedelta(seconds=spa.CLAIM_LEASE_SECONDS + 5)).isoformat(),))
    assert _login(client, "audit@example.test", customer_only=True).status_code < 400
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}


def test_the_email_is_delivered_separately_and_retried_once(site, db_path, notices):
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "mailme@example.test")
    _buy_as_guest("mailme@example.test", "evt_mail")
    notices["failing"].append(True)
    assert _login(client, "mailme@example.test", customer_only=True).status_code < 400
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}   # attachment stands
    assert _q(db_path, "SELECT email_sent_at FROM purchase_attachments")[0][0] is None
    notices["failing"].clear()
    assert spa.retry_purchase_attached_emails() == 1
    assert spa.retry_purchase_attached_emails() == 0
    assert len(notices["sent"]) == 1 and "added to your AnyAiCam account" in notices["sent"][0]["subject"]


# ------------------------------------------------------------------ conditions f, g and eligibility

def test_the_attachment_code_never_touches_sessions_cookies_or_roles():
    source = inspect.getsource(spa)
    for forbidden in ("set_cookie", "user_sessions", "RedirectResponse", "SET role"):
        assert forbidden not in source, forbidden


def test_password_reset_and_email_confirmation_never_attach():
    app_dir = Path(spa.__file__).resolve().parent
    for name in ("cloud_features.py", "cloud_security.py"):
        assert "signin_purchase_attachment" not in (app_dir / name).read_text(encoding="utf-8")
    for name in ("direct_onboarding.py", "customer_registration.py"):
        source = (app_dir / name).read_text(encoding="utf-8")
        assert "resolve_pending_links_for_customer(" not in source and "link_paid_orders(" not in source


def test_ineligible_identities_are_skipped():
    for user, reason in ((None, "no account"), ({"role": "customer_viewer", "customer_id": "c"}, "not a customer owner"),
                         ({"role": "customer_owner"}, "no customer"),
                         ({"role": "customer_owner", "customer_id": "c", "must_change_password": 1}, "password change pending"),
                         ({"role": "customer_owner", "customer_id": "c", "account_status": "suspended"}, "account not active")):
        assert spa.attach_after_signin(user) == {"status": "skipped", "reason": reason}


def test_the_order_confirmation_page_attaches_only_that_guest_checkout(site, db_path, notices):
    """attach_after_signin(..., only_session_id=...) considers only that hardware checkout; other pending purchases of
    the same address wait for a normal sign-in."""
    from partner_db import authenticate_detailed
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "page@example.test")
    _buy_as_guest("page@example.test", "evt_plan_waits")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO hardware_checkouts(id,email,items,status,stripe_checkout_session_id,created_at,updated_at) "
                   "VALUES('hc-1','page@example.test','{}','paid','cs_test_page','now','now')")
    user, _ = authenticate_detailed("page@example.test", PASSWORD)
    result = spa.attach_after_signin(user, only_session_id="cs_test_page")
    assert [item["kind"] for item in result["attached"]] == ["hardware order"]
    assert _q(db_path, "SELECT customer_id FROM hardware_checkouts WHERE id='hc-1'")[0][0] == customer_id
    assert _link_state(db_path)[0] == "pending"                     # the camera plan waits for a normal sign-in


def test_eight_parallel_sign_in_attachments_on_sqlite_grant_exactly_once(site, db_path, notices):
    """Real threads released together (Codex 2026-10-18: SQLite race evidence). BEGIN IMMEDIATE serializes writers;
    every loser's conditional UPDATE changes no row."""
    import threading
    from database_backend import override_target
    from partner_db import authenticate_detailed
    client, _, mail = site
    customer_id = _verified_owner(client, mail, db_path, "parallel@example.test")
    _buy_as_guest("parallel@example.test", "evt_parallel")
    user, _ = authenticate_detailed("parallel@example.test", PASSWORD)
    barrier, results, errors = threading.Barrier(8), [], []

    def worker():
        try:
            with override_target(sqlite_path=str(db_path)):
                barrier.wait(timeout=10)
                results.append(spa.attach_after_signin(user))
        except Exception as exc:
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert sum(len(r.get("attached", [])) for r in results) == 1
    assert _counts(db_path, customer_id) == {"slots": 10, "attachments": 1, "audits": 1}
    assert len(notices["sent"]) == 1


def test_a_recorded_refund_rejects_a_pending_checkout_before_grant(db_path):
    """A refund received between checkout completion and account attachment must not create an active license."""
    from database_backend import override_target
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p-refund','P','now')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
                       "VALUES('c-refund','p-refund','Refund Owner','refund@example.test','active','now')")
        event = {"id": "evt-refund-before-attach", "data": {"object": {
            "payment_intent": "pi_refunded_before_attach", "charge": "ch_refunded_before_attach"}}}
        link = ce.create_pending_link(email="refund@example.test", product="camera_slots_local",
                                      camera_slot_quantity=8, raw_event=event)
        billing_status.record_reversal("ch_refunded_before_attach", "pi_refunded_before_attach", "", "refunded")
        token = spa.claim(LINKS, link["id"], "refund@example.test")
        assert token
        assert spa.finalize("plan", LINKS, link["id"], token, customer_id="c-refund",
                            email="refund@example.test", label="camera plan") is None
        state = _q(db_path, f"SELECT status FROM {LINKS} WHERE id=?", (link["id"],))[0][0]
        assert state == "reversed"
        assert _q(db_path, "SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='c-refund'")[0][0] == 0
        assert _q(db_path, "SELECT COUNT(*) FROM purchase_attachments WHERE source_id=?", (link["id"],))[0][0] == 0
        assert _q(db_path, "SELECT COUNT(*) FROM audit_logs WHERE action='signin.purchase_attached'")[0][0] == 0


def test_attached_plan_keeps_stripe_references_for_later_reversal(db_path):
    from database_backend import override_target
    with override_target(sqlite_path=str(db_path)):
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p-refs','P','now')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
                       "VALUES('c-refs','p-refs','Reference Owner','refs@example.test','active','now')")
        event = {"id": "evt-refs", "data": {"object": {
            "payment_intent": "pi_keep_for_reversal", "subscription": "sub_keep_for_reversal"}}}
        link = ce.create_pending_link(email="refs@example.test", product="camera_slots_local",
                                      camera_slot_quantity=8, raw_event=event)
        token = spa.claim(LINKS, link["id"], "refs@example.test")
        assert token
        assert spa.finalize("plan", LINKS, link["id"], token, customer_id="c-refs",
                            email="refs@example.test", label="camera plan")
        license_row = _q(db_path, "SELECT stripe_payment_intent_id,stripe_subscription_id FROM customer_entitlements "
                         "WHERE customer_id='c-refs' AND product='camera_slots_local'")[0]
        assert license_row == ("pi_keep_for_reversal", "sub_keep_for_reversal")
