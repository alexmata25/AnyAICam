"""A purchase email the provider reports as failed is never recorded as sent
(Green, 2026-10-16).

SMTPEmail.send reports an SMTP/connection failure by RETURNING
{'status': 'failed', 'error': ...} rather than raising (email_service.py).
purchase_notifications._send_once used to look only for an exception, so such
a send was marked 'sent' and the outbox retry never tried again: the customer
silently got no order confirmation. Hardware confirmations also stop being
retried once they are CONFIRMATION_RETRY_DAYS old.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_direct_onboarding import _one, site  # noqa: F401 -- fixtures
from test_hardware_first_checkout import _buy, _paid_event, _record, hw  # noqa: F401 -- fixtures
from test_pricing_ff_commission import db_path, license_portal, portal  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

SESSION = "cs_test_hw000001"
SMTP_535 = "(535, b'5.7.8 Username and Password not accepted')"


@pytest.fixture()
def provider(monkeypatch):
    """An email backend that, like SMTPEmail, reports failure in its result."""
    import purchase_notifications
    state = {"failing": True, "delivered": []}

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            if state["failing"]:
                return {"type": message_type, "to": to, "status": "failed", "error": SMTP_535}
            state["delivered"].append({"type": message_type, "to": to, "subject": subject})
            return {"type": message_type, "to": to, "status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return state


def _confirmation_row(db_path, checkout_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    found = conn.execute("SELECT status,error_detail,sent_at FROM provisioning_notifications WHERE stripe_event_id=?",
                         (f"hardware-checkout-confirmed:{checkout_id}",)).fetchone()
    conn.close()
    return dict(found) if found else None


def _retry(db_path):
    import hardware_checkout
    with override_target(sqlite_path=str(db_path)):
        return hardware_checkout.retry_confirmations()


def _paid_checkout(hw, db_path):
    _buy(hw)
    _record(db_path, _paid_event(hw, SESSION))
    return _one(db_path, "SELECT id FROM hardware_checkouts WHERE stripe_checkout_session_id=?", (SESSION,))["id"]


def test_a_send_the_provider_reports_failed_is_recorded_failed_with_its_reason(hw, db_path, provider):
    import purchase_notifications as pn
    with override_target(sqlite_path=str(db_path)):
        result = pn._send_once(event_id="evt-result-check", notification_type="hardware_order_confirmation",
                               customer_id=None, recipient_email="buyer@example.test", subject="s", text="t",
                               html=None, metadata={}, test_mode=False)
    assert result["status"] == "failed" and result["error"] == SMTP_535
    stored = _one(db_path, "SELECT status,error_detail,sent_at FROM provisioning_notifications WHERE stripe_event_id=?",
                  ("evt-result-check",))
    assert stored["status"] == "failed" and stored["error_detail"] == SMTP_535 and stored["sent_at"] is None


def test_a_failed_hardware_confirmation_is_retried_until_delivered_once(hw, db_path, provider):
    checkout_id = _paid_checkout(hw, db_path)
    assert provider["delivered"] == []
    assert _confirmation_row(db_path, checkout_id)["status"] == "failed"
    assert _retry(db_path) == 0  # provider still failing: stays failed, not "sent"
    assert _confirmation_row(db_path, checkout_id)["status"] == "failed"
    provider["failing"] = False
    assert _retry(db_path) == 1
    assert [m["type"] for m in provider["delivered"]] == ["hardware_order_confirmation"]
    assert _confirmation_row(db_path, checkout_id)["status"] == "sent"
    assert _retry(db_path) == 0 and len(provider["delivered"]) == 1  # never a second email


def test_an_old_undelivered_confirmation_is_no_longer_retried(hw, db_path, provider):
    import hardware_checkout
    checkout_id = _paid_checkout(hw, db_path)
    old = (datetime.now() - timedelta(days=hardware_checkout.CONFIRMATION_RETRY_DAYS, hours=1)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_checkouts SET paid_at=?,updated_at=? WHERE id=?", (old, old, checkout_id))
    conn.commit()
    conn.close()
    provider["failing"] = False
    assert _retry(db_path) == 0 and provider["delivered"] == []
    assert _confirmation_row(db_path, checkout_id)["status"] == "failed"  # kept, visible to support


def test_a_second_retry_worker_running_during_a_send_never_sends_a_second_confirmation(hw, db_path, monkeypatch, provider):
    """Green 2026-10-16: a reused 'failed' outbox row used to stay 'failed' while it was being sent, so another
    worker (another process, or the old container during a deploy) sent the same confirmation again."""
    import purchase_notifications
    checkout_id = _paid_checkout(hw, db_path)  # the provider is failing: the row is 'failed'
    assert _confirmation_row(db_path, checkout_id)["status"] == "failed"
    delivered, inner = [], {}

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            delivered.append(to)
            if len(delivered) == 1:
                inner["sent"] = _retry(db_path)  # the other worker, mid-send
                inner["row"] = _confirmation_row(db_path, checkout_id)
            return {"type": message_type, "to": to, "status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    assert _retry(db_path) == 1
    assert delivered == ["buyer@example.test"] and inner["sent"] == 0
    assert inner["row"]["status"] == "pending"  # claimed while the send was in progress
    assert _confirmation_row(db_path, checkout_id)["status"] == "sent"


def test_a_claimed_send_left_pending_by_a_crash_is_retried_once_stale(hw, db_path, provider):
    import purchase_notifications
    checkout_id = _paid_checkout(hw, db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE provisioning_notifications SET status='pending',updated_at=? WHERE stripe_event_id=?",
                 (datetime.now().isoformat(), f"hardware-checkout-confirmed:{checkout_id}"))
    conn.commit()
    provider["failing"] = False
    assert _retry(db_path) == 0 and provider["delivered"] == []  # possibly still being sent elsewhere
    stale = (datetime.now() - timedelta(seconds=purchase_notifications.OUTBOX_CLAIM_STALE_SECONDS + 60)).isoformat()
    conn.execute("UPDATE provisioning_notifications SET updated_at=? WHERE stripe_event_id=?",
                 (stale, f"hardware-checkout-confirmed:{checkout_id}"))
    conn.commit()
    conn.close()
    assert _retry(db_path) == 1 and len(provider["delivered"]) == 1
