"""Proves the provisioning Phase 1 "checkout-first" safety net end to
end: a Stripe purchase completed under an email with no authoritative
customer yet must not be dropped, and must not require a second manual
step once that customer's registration is later approved.

customer_registration.approve_registration() is the exact moment a
brand-new customer's authoritative identity (customers.id) first exists
with a verified email -- see that function's own comment for why this is
the safe point to call customer_entitlements.
resolve_pending_links_for_customer().
"""
import sqlite3

import pytest

from customer_registration import approve_registration, create_pending_registration
from database_backend import override_target
from partner_db import initialize_database
import customer_entitlements as ce


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_registration_pending_entitlement.db"


@pytest.fixture(autouse=True)
def _db(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','Partner A','approved','real','2026-01-01')"
        )
        conn.commit()
        yield


MASTER_ACTOR = {"master": True, "email": "master@example.test", "partner_id": None}


def test_a_purchase_made_before_registration_is_granted_automatically_on_approval(db_path):
    with override_target(sqlite_path=db_path):
        # The customer paid through Stripe under this email first...
        ce.create_pending_link(
            email="new-customer@example.test", product="professional", camera_slot_quantity=10,
            raw_event={"id": "evt_before_registration"},
        )
        # ...then registers and is approved, in the opposite order.
        create_pending_registration("New Customer", "new-customer@example.test", "correct-horse-battery-staple")
        conn = sqlite3.connect(db_path)
        request_id = conn.execute("SELECT id FROM customer_registration_requests WHERE email=?", ("new-customer@example.test",)).fetchone()[0]

        result = approve_registration(request_id, "partner-a", MASTER_ACTOR)
        total_slots = ce.total_camera_slots(result["customer_id"])
        pending_status = conn.execute("SELECT status FROM pending_customer_links WHERE normalized_email=?", ("new-customer@example.test",)).fetchone()[0]

    assert total_slots == 10
    assert pending_status == "resolved"


def test_approval_with_no_pending_purchase_is_unaffected(db_path):
    """The overwhelming common case: no Stripe purchase happened first --
    approval must succeed exactly as before, granting nothing."""
    with override_target(sqlite_path=db_path):
        create_pending_registration("Plain Customer", "plain@example.test", "correct-horse-battery-staple")
        conn = sqlite3.connect(db_path)
        request_id = conn.execute("SELECT id FROM customer_registration_requests WHERE email=?", ("plain@example.test",)).fetchone()[0]

        result = approve_registration(request_id, "partner-a", MASTER_ACTOR)
        total_slots = ce.total_camera_slots(result["customer_id"])

    assert result["status"] == "complete"
    assert total_slots == 0


def test_a_pending_link_for_a_different_email_is_never_attached(db_path):
    with override_target(sqlite_path=db_path):
        ce.create_pending_link(
            email="someone-else@example.test", product="starter", camera_slot_quantity=4, raw_event={"id": "evt_other"},
        )
        create_pending_registration("New Customer", "new-customer@example.test", "correct-horse-battery-staple")
        conn = sqlite3.connect(db_path)
        request_id = conn.execute("SELECT id FROM customer_registration_requests WHERE email=?", ("new-customer@example.test",)).fetchone()[0]

        result = approve_registration(request_id, "partner-a", MASTER_ACTOR)
        total_slots = ce.total_camera_slots(result["customer_id"])
        other_link_status = conn.execute("SELECT status FROM pending_customer_links WHERE normalized_email=?", ("someone-else@example.test",)).fetchone()[0]

    assert total_slots == 0
    assert other_link_status == "pending"  # untouched -- still waiting for its own matching registration
