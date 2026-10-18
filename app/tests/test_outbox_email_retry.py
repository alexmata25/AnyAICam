"""Purchase emails that failed to send are re-sent from the outbox copy (Green 2026-10-17).

_send_once records a failed send and never raises, and nothing retried account-ready / setup-required / plan /
registration-outcome emails, so a mail-server hiccup lost them for good. The outbox now keeps the exact final copy
(body_text / body_html) and retry_outbox_emails re-sends it once, unchanged, within 48 hours, for an allow-list of types
whose message stays true later.
"""
import sqlite3
from datetime import datetime, timedelta

import customer_registration as cr
import purchase_notifications as pn
from test_registration_decision_emails import _verified_request, mail  # noqa: F401 -- fixture
from test_registration_email_verification_127 import MASTER, _request, db  # noqa: F401 -- fixture


def _outbox(db, kind):
    return db.execute("SELECT * FROM provisioning_notifications WHERE notification_type=?", (kind,)).fetchone()


def test_a_failed_registration_email_is_resent_once_from_the_stored_copy(db, mail):
    _verified_request()
    mail["failing"].append(True)
    cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    row = _outbox(db, "registration_approved")
    assert row["status"] == "failed" and "approved" in row["body_text"] and row["body_html"]
    mail["failing"].clear()
    assert pn.retry_outbox_emails() == 1
    assert pn.retry_outbox_emails() == 0  # never a second copy
    resent = [m for m in mail["sent"] if m["type"] == "registration_approved"]
    assert len(resent) == 1 and resent[0]["text"] == row["body_text"] and resent[0]["subject"] == row["subject"]
    assert _outbox(db, "registration_approved")["status"] == "sent"


def test_an_old_failure_is_not_resent_days_later(db, mail):
    _verified_request()
    mail["failing"].append(True)
    cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    old = (datetime.now() - timedelta(hours=pn.RETRY_OUTBOX_MAX_AGE_HOURS + 1)).isoformat()
    db.execute("UPDATE provisioning_notifications SET created_at=?,updated_at=?", (old, old))
    db.commit()
    mail["failing"].clear()
    assert pn.retry_outbox_emails() == 0
    assert [m for m in mail["sent"] if m["type"] == "registration_approved"] == []


def test_state_dependent_emails_are_never_resent_from_the_copy():
    assert "service_suspended" not in pn.RETRY_OUTBOX_TYPES and "service_restored" not in pn.RETRY_OUTBOX_TYPES
    for own_retry in ("payment_failed", "hardware_shipped", "hardware_order_confirmation"):
        assert own_retry not in pn.RETRY_OUTBOX_TYPES


def test_the_billing_worker_runs_the_outbox_retry():
    import inspect

    import main
    assert "retry_outbox_emails" in inspect.getsource(main._billing_grace_worker)
