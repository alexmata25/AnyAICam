"""A shipping email that could not be sent is retried (Green 2026-10-17).

notify_hardware_shipped records a failed send in the outbox and never raises, but nothing retried hardware_shipped
rows (the billing worker retried payment-failed, order confirmations, hardware confirmations and storage starts only),
so when SMTP was down at "Mark shipped" the customer never received carrier and tracking by email. The billing worker
now also runs retry_hardware_shipped_notifications, which re-enters notify_hardware_shipped (so an order refunded or
disputed meanwhile is still never emailed) and sends each email at most once.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_hardware_shipped_email_race import INTENT, shipped_order  # noqa: F401 -- fixture
from test_hybrid_build_system_flow import db_path, license_portal, package, portal, shop, site, storage  # noqa: F401

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


class _Mail:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
        self.sent.append({"type": message_type, "to": to, "subject": subject, "text": text})
        return {"status": "sent"}


def _outbox_status(db_path, order_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT status FROM provisioning_notifications WHERE stripe_event_id=?",
                            (f"hardware-shipped:{order_id}",)).fetchone()[0]
    finally:
        conn.close()


def test_a_failed_shipping_email_is_resent_once_with_the_tracking_number(shipped_order, db_path, monkeypatch):
    import purchase_notifications
    order_id = shipped_order
    assert _outbox_status(db_path, order_id) == "failed"
    mail = _Mail()
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: mail)
    with override_target(sqlite_path=str(db_path)):
        assert purchase_notifications.retry_hardware_shipped_notifications() == 1
        assert purchase_notifications.retry_hardware_shipped_notifications() == 0  # never a second copy
    assert [m["type"] for m in mail.sent] == ["hardware_shipped"]
    assert mail.sent[0]["to"] == "race@example.test"
    assert "1Z999" in mail.sent[0]["text"] and "UPS" in mail.sent[0]["text"]
    assert _outbox_status(db_path, order_id) == "sent"


@pytest.mark.parametrize("reversal", ["refunded", "disputed"])
def test_a_refunded_or_disputed_order_is_not_emailed_by_the_retry(shipped_order, db_path, monkeypatch, reversal):
    import billing_status
    import purchase_notifications
    mail = _Mail()
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: mail)
    with override_target(sqlite_path=str(db_path)):
        billing_status.reverse_hardware_orders(INTENT, reversal)
        assert purchase_notifications.retry_hardware_shipped_notifications() == 0
    assert mail.sent == []


def test_the_billing_worker_runs_the_shipping_email_retry():
    import inspect

    import main
    assert "retry_hardware_shipped_notifications" in inspect.getsource(main._billing_grace_worker)
