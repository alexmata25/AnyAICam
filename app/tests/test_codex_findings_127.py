"""VMS 1.2.7 regressions for the two issues in Codex's independent review (2026-10-16).

1. A suspended or closed account must never receive a Stripe purchase by email matching (camera plans, hardware
   orders and analytics add-ons all reconcile by email when the event carries no authoritative customer id).
2. A rejected registration request must not block that email from applying again.
"""
import json
import sqlite3

import pytest
from fastapi import HTTPException

import analytics_entitlements
import customer_entitlements
import hardware_orders
from customer_registration import create_pending_registration, issue_registration_verification, verify_registration_email
from database_backend import override_target
from partner_db import initialize_database

NOW = "2026-10-16T00:00:00"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "codex127.db"
    with override_target(sqlite_path=path):
        initialize_database()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p','P',?)", (NOW,))
        conn.commit()  # never hold a write transaction while the code under test writes
        yield conn
        conn.close()


def _customer(conn, cid, email, status):
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                 (cid, "p", cid, email, status, NOW))
    conn.commit()


# ------------------------------------------------------------------------------------- 1. email matching
FINDERS = [customer_entitlements.find_customer_by_email, hardware_orders.find_customer_by_email,
           analytics_entitlements._find_customer_by_email]


@pytest.mark.parametrize("finder", FINDERS, ids=["camera_plan", "hardware", "analytics"])
@pytest.mark.parametrize("status", ["suspended", "closed_test", "closed", "rejected", "deleted", "SUSPENDED"])
def test_an_inactive_account_never_matches_a_purchase_email(db, finder, status):
    _customer(db, "cust-old", "Buyer@Example.test", status)
    assert finder(" buyer@example.test ") is None


@pytest.mark.parametrize("finder", FINDERS, ids=["camera_plan", "hardware", "analytics"])
@pytest.mark.parametrize("status", ["active", "pending_installation", "trial", "Active"])
def test_a_usable_account_still_matches(db, finder, status):
    _customer(db, "cust-live", "buyer@example.test", status)
    found = finder("BUYER@example.test")
    assert found is not None and found["id"] == "cust-live"


def test_a_suspended_account_is_not_found_for_the_camera_plan_webhook(db):
    """The camera-plan webhook path with no authoritative id finds no account here (it then creates a pending link,
    covered by the existing pending-link tests); nothing is granted to the suspended account."""
    _customer(db, "cust-old", "buyer@example.test", "suspended")
    assert customer_entitlements.find_customer_by_email("buyer@example.test") is None
    assert db.execute("SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='cust-old'").fetchone()[0] == 0


# ------------------------------------------------------------------------------------- 2. re-registration
def _request(conn, email):
    return conn.execute("SELECT * FROM customer_registration_requests WHERE email=?", (email,)).fetchone()


def _reject(conn, email, *, customer_id=None, user_id=None):
    conn.execute("UPDATE customer_registration_requests SET status='rejected',decided_at=?,decided_by='admin@example.test',"
                 "rejection_reason='duplicate test',customer_id=?,user_id=? WHERE email=?", (NOW, customer_id, user_id, email))
    conn.commit()


def test_a_rejected_request_can_apply_again_and_the_old_decision_stays_audited(db):
    assert create_pending_registration("First Try", "again@example.test", "correct-horse-battery-staple") == ("pending", True)
    assert verify_registration_email(issue_registration_verification("again@example.test"), "correct-horse-battery-staple", "Confirmed Owner")
    first = _request(db, "again@example.test")
    _reject(db, "again@example.test")

    assert create_pending_registration("Second Try", "Again@Example.test", "another-long-passphrase-1") == ("pending", True)
    reopened = _request(db, "again@example.test")
    assert reopened["id"] == first["id"]
    assert reopened["status"] == "pending" and reopened["display_name"] == "Second Try"
    assert reopened["decided_at"] is None and reopened["decided_by"] is None and reopened["rejection_reason"] is None
    assert reopened["password_hash"] != first["password_hash"]
    # 2026-10-17: the earlier confirmation is void; the reopened request must confirm its email again.
    assert first["email_verified_at"] is not None
    assert reopened["email_verified_at"] is None and reopened["email_verified_for"] is None
    assert reopened["verification_token_hash"] is None and reopened["verification_sent_at"] is None
    audit = db.execute("SELECT details_json FROM audit_logs WHERE action='customer_registration.reopened' AND entity_id=?",
                       (first["id"],)).fetchone()
    details = json.loads(audit["details_json"])
    assert details["previous_status"] == "rejected" and details["previous_reason"] == "duplicate test"
    assert details["previous_decided_by"] == "admin@example.test"


def test_a_pending_request_is_still_not_duplicated(db):
    create_pending_registration("A", "pending@example.test", "correct-horse-battery-staple")
    assert create_pending_registration("A", "pending@example.test", "correct-horse-battery-staple") == ("pending", False)
    assert db.execute("SELECT COUNT(*) FROM customer_registration_requests").fetchone()[0] == 1


def test_a_rejected_request_whose_account_still_exists_is_sent_to_support(db):
    """Rejected after approval: the suspended customer and revoked user still hold the email."""
    create_pending_registration("A", "held@example.test", "correct-horse-battery-staple")
    _customer(db, "cust-held", "held@example.test", "suspended")
    _reject(db, "held@example.test", customer_id="cust-held")
    with pytest.raises(HTTPException) as refused:
        create_pending_registration("A", "held@example.test", "correct-horse-battery-staple")
    assert refused.value.status_code == 409 and "contact AnyAiCam support" in refused.value.detail
    assert _request(db, "held@example.test")["status"] == "rejected"


def test_an_existing_active_account_is_still_refused(db):
    _customer(db, "cust-live", "live@example.test", "active")
    with pytest.raises(HTTPException) as refused:
        create_pending_registration("A", "live@example.test", "correct-horse-battery-staple")
    assert refused.value.status_code == 409
