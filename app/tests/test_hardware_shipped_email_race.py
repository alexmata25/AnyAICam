"""Shipping email vs. refund/dispute race (2026-10-05, Codex review of
51d1346): notify_hardware_shipped() decided "paid and shipped" and then sent
in separate steps, so a refund or dispute could commit in between and the
customer would be told an order that was no longer paid had shipped.

The decision, the send-once claim and the dispatch now run while holding the
order (SQLite: the write lock; PostgreSQL: the order row), so a refund or
dispute is applied either wholly before (no email) or after the email went
out -- never in between.
"""
import sqlite3
import threading

import pytest

from database_backend import override_target
from test_build_order_billing import APPLIANCE, _paid  # noqa: F401
from test_hardware_fulfillment_admin import _admin_cookie, _order_id, _step
from test_hybrid_build_system_flow import (_activate_appliance, _customer_id, _new_customer,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

INTENT = "pi_race_test"


@pytest.fixture()
def shipped_order(shop, db_path, monkeypatch):
    """A paid order marked shipped whose shipping email has not gone out yet
    (the first attempt failed, so the outbox row is retryable)."""
    import purchase_notifications
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "race@example.test", APPLIANCE, captured)
    order_id = _order_id(db_path, customer_id)

    class _Down:
        def send(self, *a, **k):
            raise OSError("email provider unavailable")
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Down())
    admin = _admin_cookie(db_path)
    _step(client, order_id, admin, status="preparing")
    assert _step(client, order_id, admin, status="shipped", carrier="UPS", tracking_number="1Z999").json()["status"] == "applied"
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET stripe_payment_intent_id=? WHERE id=?", (INTENT, order_id))
    conn.commit()
    conn.close()
    return order_id


def _order_status(db_path, order_id):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        return conn.execute("SELECT status FROM hardware_orders WHERE id=?", (order_id,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("reversal", ["refunded", "disputed"])
def test_a_refund_or_dispute_cannot_commit_between_the_decision_and_the_shipping_email(shipped_order, db_path, monkeypatch, reversal):
    import billing_status
    import purchase_notifications
    order_id = shipped_order
    observed = {}

    def reverse():
        with override_target(sqlite_path=str(db_path)):
            observed["reversed"] = billing_status.reverse_hardware_orders(INTENT, reversal)

    class _RacingMail:
        """While the shipping email is being handed to the provider, Stripe's
        refund/dispute webhook arrives and tries to reverse the order."""
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            webhook = threading.Thread(target=reverse)
            webhook.start()
            webhook.join(timeout=1.5)  # long enough for an unblocked refund to commit
            observed["status_while_sending"] = _order_status(db_path, order_id)
            observed["webhook_thread"] = webhook
            return {"status": "sent"}

    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _RacingMail())
    with override_target(sqlite_path=str(db_path)):
        result = purchase_notifications.notify_hardware_shipped(order_id)
    observed["webhook_thread"].join(timeout=10)

    assert result["status"] == "sent"
    # The order was still paid for the whole dispatch: the reversal waited.
    assert observed["status_while_sending"] == "paid"
    # ...and was then applied, not lost.
    assert observed["reversed"] == 1 and _order_status(db_path, order_id) == reversal
    # Once reversed, no further shipping email is ever sent.
    with override_target(sqlite_path=str(db_path)):
        assert purchase_notifications.notify_hardware_shipped(order_id)["status"] in ("ignored", "skipped")


def test_a_refund_that_commits_first_means_no_shipping_email(shipped_order, db_path, monkeypatch):
    import billing_status
    import purchase_notifications
    sent = []

    class _Mail:
        def send(self, *a, **k):
            sent.append(a)
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    with override_target(sqlite_path=str(db_path)):
        assert billing_status.reverse_hardware_orders(INTENT, "refunded") == 1
        assert purchase_notifications.notify_hardware_shipped(shipped_order)["status"] == "ignored"
    assert sent == []


def test_send_once_and_retry_survive_the_lock(shipped_order, db_path, monkeypatch):
    import purchase_notifications
    state = {"down": True, "sent": 0}

    class _Mail:
        def send(self, *a, **k):
            if state["down"]:
                raise OSError("email provider unavailable")
            state["sent"] += 1
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    with override_target(sqlite_path=str(db_path)):
        assert purchase_notifications.notify_hardware_shipped(shipped_order)["status"] == "failed"  # recorded, retryable
        state["down"] = False
        assert purchase_notifications.notify_hardware_shipped(shipped_order)["status"] == "sent"
        assert purchase_notifications.notify_hardware_shipped(shipped_order)["status"] == "skipped"
    assert state["sent"] == 1
    rows = sqlite3.connect(db_path).execute("SELECT status FROM provisioning_notifications WHERE stripe_event_id=?",
                                            (f"hardware-shipped:{shipped_order}",)).fetchall()
    assert rows == [("sent",)]


def test_concurrent_shipping_notifications_send_one_email(shipped_order, db_path, monkeypatch):
    import purchase_notifications
    sent = []
    gate = threading.Barrier(2)

    class _Mail:
        def send(self, *a, **k):
            sent.append(a)
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())

    def notify():
        gate.wait()
        with override_target(sqlite_path=str(db_path)):
            purchase_notifications.notify_hardware_shipped(shipped_order)
    threads = [threading.Thread(target=notify) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert len(sent) == 1
