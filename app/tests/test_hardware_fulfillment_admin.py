"""Operator control of hardware fulfillment (2026-10-05, after Codex review of
453bd1c): platform administrators move a paid order through preparing ->
shipped -> delivered; activation follows the customer's appliance. Durable,
ordered, idempotent, audited; one shipping email per order; nothing about the
order taken from the browser.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_build_order_billing import APPLIANCE, _paid  # noqa: F401
from test_hybrid_build_system_flow import (_activate_appliance, _customer_id, _new_customer,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


def _admin_cookie(db_path, *, global_grant=True, email="ops@example.test"):
    import partner_portal
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partner_users(id,email,name,role,password_hash,approved,created_at) "
                 "VALUES(?,?,'Ops','administrator','x',1,'2026-01-01')", (f"admin-{email}", email))
    if global_grant:
        conn.execute("INSERT OR IGNORE INTO identity_grants(id,user_id,role,scope_type,granted_at) "
                     "VALUES(?,?,'administrator','global','2026-01-01')", (f"grant-{email}", f"admin-{email}"))
    conn.commit()
    conn.close()
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, "administrator", None, None, None)}


@pytest.fixture()
def outbox(monkeypatch):
    import purchase_notifications
    state = {"sent": [], "down": False}

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            if state["down"]:
                raise OSError("email provider unavailable")
            state["sent"].append({"type": message_type, "to": to, "subject": subject, "text": text})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return state


def _order_id(db_path, customer_id):
    return sqlite3.connect(db_path).execute("SELECT id FROM hardware_orders WHERE customer_id=? AND sku LIKE 'AIC-APPLIANCE-%'",
                                            (customer_id,)).fetchone()[0]


def _step(client, order_id, cookies, **body):
    return client.post(f"/api/admin/hardware-orders/{order_id}/fulfillment", json=body, cookies=cookies)


@pytest.fixture()
def order(shop, db_path):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "buyer@example.test", APPLIANCE, captured)
    return client, customer_id, _order_id(db_path, customer_id)


# ------------------------------------------------------------ authorization

def test_customers_partner_admins_and_signed_out_visitors_are_refused(order, db_path):
    client, customer_id, order_id = order
    owner_cookies = dict(client.cookies)
    for cookies in (owner_cookies, _admin_cookie(db_path, global_grant=False, email="partner-admin@example.test")):
        assert _step(client, order_id, cookies, status="preparing").status_code == 403
        assert client.get("/admin/hardware-orders", cookies=cookies).status_code == 403
    client.cookies.clear()
    anonymous = client.post(f"/api/admin/hardware-orders/{order_id}/fulfillment", json={"status": "preparing"})
    assert anonymous.status_code in (401, 403, 303)
    assert sqlite3.connect(db_path).execute("SELECT fulfillment_status FROM hardware_orders WHERE id=?", (order_id,)).fetchone()[0] == "paid"


# ------------------------------------------------------------ the full path, and what the customer sees

def test_preparing_shipped_delivered_then_activation(order, db_path, outbox):
    client, customer_id, order_id = order
    customer_cookies = dict(client.cookies)
    admin = _admin_cookie(db_path)
    assert _step(client, order_id, admin, status="preparing").json()["status"] == "applied"
    assert "being prepared" in client.get("/order-complete", cookies=customer_cookies).text
    shipped = _step(client, order_id, admin, status="shipped", carrier="UPS", tracking_number="1Z999AA10123456784")
    assert shipped.status_code == 200 and shipped.json()["fulfillment_status"] == "shipped"
    stored = sqlite3.connect(db_path).execute("SELECT tracking_link FROM hardware_orders WHERE id=?", (order_id,)).fetchone()[0]
    assert stored == "https://www.ups.com/track?tracknum=1Z999AA10123456784"
    page = client.get("/order-complete", cookies=customer_cookies).text
    assert "on its way" in page and "Tracking number: 1Z999AA10123456784" in page
    assert [m["type"] for m in outbox["sent"]].count("hardware_shipped") == 1
    assert _step(client, order_id, admin, status="delivered").json()["fulfillment_status"] == "delivered"
    page = client.get("/order-complete", cookies=customer_cookies).text
    assert '<a class="submit" id="order-setup" href="/customer/setup">Start Setup</a>' in page
    _activate_appliance(db_path, customer_id)
    listing = client.get("/admin/hardware-orders", cookies=admin).text
    assert '<span class="pill">activated</span>' in listing
    actions = sqlite3.connect(db_path).execute("SELECT action FROM audit_logs WHERE entity_id=? ORDER BY id", (order_id,)).fetchall()
    assert [a[0] for a in actions] == ["hardware_order.preparing", "hardware_order.shipped", "hardware_order.delivered"]


# ------------------------------------------------------------ transitions

@pytest.mark.parametrize("first,then", [(None, "shipped"), (None, "delivered"), ("preparing", "delivered")])
def test_steps_cannot_be_skipped(order, db_path, first, then):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    if first:
        _step(client, order_id, admin, status=first)
    body = {"status": then, **({"carrier": "UPS", "tracking_number": "1Z999"} if then == "shipped" else {})}
    assert _step(client, order_id, admin, **body).status_code == 409


def test_steps_cannot_go_backwards(order, db_path, outbox):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    _step(client, order_id, admin, status="preparing")
    _step(client, order_id, admin, status="shipped", carrier="UPS", tracking_number="1Z999")
    assert _step(client, order_id, admin, status="preparing").status_code == 409


@pytest.mark.parametrize("status", ["refunded", "disputed"])
def test_a_refunded_or_disputed_order_cannot_be_fulfilled(order, db_path, status):
    client, _, order_id = order
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()
    assert _step(client, order_id, _admin_cookie(db_path), status="preparing").status_code == 409


def test_unknown_orders_and_statuses_are_refused(order, db_path):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    assert _step(client, "no-such-order", admin, status="preparing").status_code == 404
    assert _step(client, order_id, admin, status="activated").status_code == 400
    assert _step(client, order_id, admin, status="cancelled").status_code == 400


# ------------------------------------------------------------ idempotency and email

def test_repeated_steps_are_idempotent_and_send_one_shipping_email(order, db_path, outbox):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    assert _step(client, order_id, admin, status="preparing").json()["status"] == "applied"
    assert _step(client, order_id, admin, status="preparing").json()["status"] == "unchanged"
    for _ in range(3):
        _step(client, order_id, admin, status="shipped", carrier="UPS", tracking_number="1Z999")
    assert [m["type"] for m in outbox["sent"]].count("hardware_shipped") == 1
    conflicting = _step(client, order_id, admin, status="shipped", carrier="UPS", tracking_number="1Z000")
    assert conflicting.status_code == 409
    audit = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM audit_logs WHERE entity_id=? AND action='hardware_order.shipped'",
                                             (order_id,)).fetchone()[0]
    assert audit == 1


def test_a_failed_shipping_email_is_sent_once_when_the_step_is_repeated(order, db_path, outbox):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    _step(client, order_id, admin, status="preparing")
    outbox["down"] = True
    assert _step(client, order_id, admin, status="shipped", carrier="FedEx", tracking_number="7489").json()["status"] == "applied"
    assert outbox["sent"] == []
    outbox["down"] = False
    assert _step(client, order_id, admin, status="shipped", carrier="FedEx", tracking_number="7489").json()["status"] == "unchanged"
    _step(client, order_id, admin, status="shipped", carrier="FedEx", tracking_number="7489")
    assert [m["type"] for m in outbox["sent"]].count("hardware_shipped") == 1


# ------------------------------------------------------------ input safety

@pytest.mark.parametrize("carrier,tracking", [("UPS", "<script>alert(1)</script>"), ("<b>UPS</b>", "1Z999"), ("UPS", ""), ("", "1Z999"),
                                              ("UPS", "1Z9" + "9" * 80)])
def test_carrier_and_tracking_are_validated(order, db_path, carrier, tracking):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    _step(client, order_id, admin, status="preparing")
    assert _step(client, order_id, admin, status="shipped", carrier=carrier, tracking_number=tracking).status_code == 400


def test_the_tracking_link_is_never_taken_from_input(order, db_path, outbox):
    client, _, order_id = order
    admin = _admin_cookie(db_path)
    _step(client, order_id, admin, status="preparing")
    injected = _step(client, order_id, admin, status="shipped", carrier="UPS", tracking_number="1Z999",
                     tracking_link="https://evil.example/")
    assert injected.status_code == 422
    other = _step(client, order_id, admin, status="shipped", carrier="Local Courier", tracking_number="LC-12345")
    assert other.status_code == 200
    assert sqlite3.connect(db_path).execute("SELECT tracking_link FROM hardware_orders WHERE id=?", (order_id,)).fetchone()[0] is None


def test_the_operator_page_escapes_customer_data(order, db_path):
    client, customer_id, order_id = order
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE customers SET name=? WHERE id=?", ("<script>alert('x')</script>", customer_id))
    conn.commit()
    conn.close()
    html = client.get("/admin/hardware-orders", cookies=_admin_cookie(db_path)).text
    assert "<script>alert('x')</script>" not in html and "&lt;script&gt;" in html
    assert f'data-order="{order_id}" data-status="preparing"' in html
