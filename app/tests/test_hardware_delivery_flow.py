"""Post-payment paths for Build Your System orders (owner requirement 2026-10-05).

Path A -- software / own PC (or no hardware): straight to Download AnyAiCam
and Set Up My System.
Path B -- AnyAiCam appliance: order confirmed, "Your AnyAiCam system is being
prepared", no setup or camera discovery as the next step until the appliance
is delivered. The state comes from durable records (Build Your System
selection, paid plan, hardware_orders.fulfillment_status), so it survives
closing the browser, signing out and returning days later on another device.
One order-confirmation email per account, after the verified webhook granted
the plan. Billing start is unchanged (Stripe starts the subscription at
checkout payment).
"""
import sqlite3

import pytest

from database_backend import override_target
from test_direct_onboarding import _direct_owner, _login, _one, _signed_in  # noqa: F401
from test_hybrid_build_system_flow import (_activate_appliance, _checkout, _checkouts, _customer_id, _new_customer, _pay,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

OWN_PC = "plan=ai_local&cameras=5&vms_licence=5"
APPLIANCE = "plan=hybrid&cameras=8&appliance=AIC-APPLIANCE-RYZEN-STARTER"


def _paid(client, mail, db_path, email, query):
    _new_customer(client, mail, email, query)
    customer_id = _customer_id(db_path, email)
    plan, cameras = ("ai_local", 5) if "ai_local" in query else ("hybrid", 8)
    _pay(db_path, customer_id, plan, cameras, f"sub_{customer_id}")
    return customer_id


def _hardware_order(db_path, customer_id, *, fulfillment="preparing", status="paid", **extra):
    conn = sqlite3.connect(db_path)
    values = {"id": f"hw-{fulfillment}-{status}", "customer_id": customer_id, "sku": "AIC-APPLIANCE-RYZEN-STARTER",
              "product_name": "AnyAiCam Starter", "stripe_price_id": "price_hw", "quantity": 1, "amount_cents": 124999,
              "currency": "usd", "status": status, "fulfillment_status": fulfillment,
              "created_at": "2026-10-05T10:00:00", "updated_at": "2026-10-05T10:00:00", **extra}
    conn.execute(f"INSERT INTO hardware_orders({','.join(values)}) VALUES({','.join('?' * len(values))})", tuple(values.values()))
    conn.commit()
    conn.close()


@pytest.fixture()
def outbox(monkeypatch):
    import purchase_notifications
    sent = []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return sent


def _event(customer_id, event_id="evt_build_1"):
    return {"id": event_id, "type": "checkout.session.completed", "livemode": False,
            "data": {"object": {"metadata": {"anyaicam_customer_id": customer_id, "anyaicam_billing_version": "2"}}}}


def _notify(db_path, event):
    import order_funnel
    with override_target(sqlite_path=str(db_path)):
        return order_funnel.notify_order_confirmed(event)


# ------------------------------------------------------------ Path A: own PC / software

def test_own_pc_customer_goes_straight_to_download_and_setup(shop, db_path):
    client, _, mail, _, _ = shop
    _paid(client, mail, db_path, "ownpc@example.test", OWN_PC)
    html = client.get("/order-complete").text
    assert "Your AnyAiCam system is ready to set up." in html
    assert "Download AnyAiCam and install it on your PC." in html
    assert 'id="order-download-pending"' in html  # no installer published in this test
    assert '<a class="submit" id="order-setup" href="/customer/setup" style="margin-top:10px">Set Up My System</a>' in html
    assert "being prepared" not in html
    assert client.get("/customer-account").headers["location"] == "/customer/setup"
    dashboard = client.get("/dashboard")
    if dashboard.status_code == 200:
        assert '<a href="/customer/setup">Continue to setup</a>' in dashboard.text


def test_own_pc_customer_gets_the_real_installer_download_when_published(shop, db_path, monkeypatch):
    import customer_downloads
    client, _, mail, _, _ = shop
    _paid(client, mail, db_path, "download@example.test", OWN_PC)
    monkeypatch.setattr(customer_downloads, "latest_vms_installer", lambda: {"version": "1.2.3", "commit": "a" * 40,
                        "filename": "x.tar.gz", "size_bytes": 1, "sha256": "b" * 64, "key": "vms-installer/x.tar.gz"})
    html = client.get("/order-complete").text
    assert '<a class="submit" id="order-download" href="/api/customer/downloads/vms-installer" download>Download AnyAiCam</a>' in html
    with override_target(sqlite_path=str(db_path)):
        assert customer_downloads.download_eligibility({"role": "customer_owner",
                                                        "customer_id": _customer_id(db_path, "download@example.test")})[0] is True


# ------------------------------------------------------------ Path B: appliance -- setup deferred

def test_appliance_customer_is_told_the_system_is_being_prepared_not_sent_to_setup(shop, db_path):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "appliance@example.test", APPLIANCE)
    html = client.get("/order-complete").text
    assert "Your AnyAiCam system is being prepared." in html
    assert "We'll email you when your system ships. When your package arrives, sign in to your AnyAiCam account" in html
    assert "You don't need to set up any cameras yet." in html
    assert 'id="order-setup"' not in html and "Discover" not in html  # no setup/discovery as the next step
    assert "ordered with AnyAiCam by phone" in html  # no hardware order recorded yet
    assert '<a class="ghost" id="order-setup-early" href="/customer/setup">Already received your appliance? Start setup</a>' in html
    # Signing in lands on the order status, not on appliance/camera setup.
    assert client.get("/customer-account").headers["location"] == "/order-complete"
    dashboard = client.get("/dashboard")
    if dashboard.status_code == 200:
        assert "Order confirmed</strong> — Your AnyAiCam system is being prepared." in dashboard.text
        assert '<a href="/order-complete">View order status</a>' in dashboard.text
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        assert ce.usable_camera_capacity(customer_id) == 8  # paid capacity is already on the account


def test_a_returning_appliance_customer_days_later_on_another_device_sees_the_same_order(shop, db_path):
    client, captured, mail, _, _ = shop
    _paid(client, mail, db_path, "later@example.test", APPLIANCE)
    client.cookies.clear()  # browser closed, new device: nothing client-side
    assert _signed_in(_login(client, "later@example.test", customer_only=True))
    landing = client.get("/customer-account")
    assert landing.status_code == 303 and landing.headers["location"] == "/order-complete"
    assert "Your AnyAiCam system is being prepared." in client.get("/order-complete").text
    # Never back through Build Your System or Stripe once paid.
    again = client.get("/order-summary")
    assert _checkouts(captured) == [] and again.status_code == 303 and again.headers["location"] == "/order-complete"


def test_a_recorded_hardware_order_shows_its_number_and_preparation_time(shop, db_path):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "prep@example.test", APPLIANCE)
    _hardware_order(db_path, customer_id, fulfillment="preparing")
    html = client.get("/order-complete").text
    assert 'id="order-status">Order ' in html and "AnyAiCam Starter · being prepared." in html
    assert "Please allow up to" in html and "by phone" not in html and 'id="order-setup"' not in html


def test_shipped_shows_only_recorded_tracking_and_offers_start_setup_for_arrival(shop, db_path):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "ship@example.test", APPLIANCE)
    _hardware_order(db_path, customer_id, fulfillment="shipped", carrier="UPS", tracking_number="1Z999",
                    tracking_link="https://www.ups.com/track?tracknum=1Z999", shipped_at="2026-10-06T09:00:00")
    html = client.get("/order-complete").text
    assert "Your AnyAiCam system is on its way." in html
    assert "Carrier: UPS<br>Tracking number: 1Z999" in html and 'href="https://www.ups.com/track?tracknum=1Z999"' in html
    assert ">My appliance has arrived — Start Setup</a>" in html
    dashboard = client.get("/dashboard")
    if dashboard.status_code == 200:
        assert "Your AnyAiCam system has shipped." in dashboard.text


def test_no_tracking_is_invented_and_unsafe_links_are_dropped(shop, db_path):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "notrack@example.test", APPLIANCE)
    _hardware_order(db_path, customer_id, fulfillment="shipped", tracking_link="javascript:alert(1)")
    html = client.get("/order-complete").text
    assert 'id="order-tracking"' not in html and "javascript:" not in html


def test_delivered_hardware_is_ready_for_setup(shop, db_path):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "delivered@example.test", APPLIANCE)
    _hardware_order(db_path, customer_id, fulfillment="delivered")
    html = client.get("/order-complete").text
    assert "Your AnyAiCam system is ready to set up." in html
    assert '<a class="submit" id="order-setup" href="/customer/setup">Start Setup</a>' in html
    assert client.get("/customer-account").headers["location"] == "/customer/setup"


@pytest.mark.parametrize("status,fulfillment", [("refunded", "preparing"), ("disputed", "shipped"), ("paid", "cancelled")])
def test_refunded_disputed_or_cancelled_hardware_orders_do_not_count(shop, db_path, status, fulfillment):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "void@example.test", APPLIANCE)
    _hardware_order(db_path, customer_id, fulfillment=fulfillment, status=status)
    html = client.get("/order-complete").text
    assert "ordered with AnyAiCam by phone" in html and "on its way" not in html


def test_setup_complete_ends_the_order_notices(shop, db_path):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "active@example.test", APPLIANCE)
    _hardware_order(db_path, customer_id, fulfillment="delivered")
    _activate_appliance(db_path, customer_id)
    assert "Your AnyAiCam plan is active" in client.get("/order-complete").text
    for path in ("/dashboard", "/subscription-portal"):
        assert 'id="purchase-progress-banner"' not in client.get(path).text
    assert _one(db_path, "SELECT state FROM build_system_intents")["state"] == "setup_complete"


def test_an_unpaid_appliance_order_still_resumes_checkout_not_the_order_status(shop, db_path):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "unpaid@example.test", APPLIANCE)
    assert client.get("/customer-account").headers["location"] == "/order-summary"
    assert client.get("/order-complete").headers["location"] == "/order-summary"


# ------------------------------------------------------------ order-confirmation email

def test_appliance_order_confirmation_email_is_sent_once_after_payment(shop, db_path, outbox):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "mail-hw@example.test", APPLIANCE)
    customer_id = _customer_id(db_path, "mail-hw@example.test")
    assert _notify(db_path, _event(customer_id))["status"] == "ignored" and outbox == []  # nothing before the plan exists
    _pay(db_path, customer_id, "hybrid", 8, "sub_mail_hw")
    assert _notify(db_path, _event(customer_id))["status"] == "sent"
    assert _notify(db_path, _event(customer_id, "evt_build_2"))["status"] == "skipped"  # once per account
    assert len(outbox) == 1
    email = outbox[0]
    assert email["type"] == "hardware_order_confirmation" and email["to"] == "mail-hw@example.test"
    assert "Your AnyAiCam order is confirmed" in email["subject"]
    for line in ("Your payment is confirmed and your AnyAiCam Hybrid subscription for 8 licensed cameras is active.",
                 "Your AnyAiCam system is being prepared.", "You don't need to set up any cameras yet.",
                 "We'll email you when your system ships. When your package arrives, sign in to your AnyAiCam account"):
        assert line in email["text"]
    assert "racking" not in email["text"]  # no tracking invented


def test_own_pc_order_confirmation_email_points_to_download_and_setup(shop, db_path, outbox):
    client, _, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "mail-sw@example.test", OWN_PC)
    assert _notify(db_path, _event(customer_id))["status"] == "sent"
    email = outbox[0]
    assert email["type"] == "account_ready" and "Your AnyAiCam subscription is active" in email["subject"]
    assert "download AnyAiCam" in email["text"] and "being prepared" not in email["text"]


def test_purchases_without_a_build_system_order_get_no_new_email(shop, db_path, outbox):
    client, _, mail, _, _ = shop
    _direct_owner(client, mail, "portal-buyer@example.test")
    customer_id = _customer_id(db_path, "portal-buyer@example.test")
    _pay(db_path, customer_id, "ai_local", 2, "sub_portal_buyer")
    assert _notify(db_path, _event(customer_id))["status"] == "ignored" and outbox == []


@pytest.mark.parametrize("event", [{}, {"id": "e"}, {"data": {"object": {"metadata": {"anyaicam_billing_version": "2"}}}},
                                   {"data": {"object": {"metadata": {"anyaicam_customer_id": "x"}}}}])
def test_the_email_hook_never_raises(event, db_path):
    assert _notify(db_path, event)["status"] in ("ignored", "error")


def test_the_v2_webhook_step_sends_the_confirmation_after_granting_the_plan(monkeypatch):
    import main
    import order_funnel
    import per_camera_billing
    calls = []
    monkeypatch.setattr(per_camera_billing, "sync_from_stripe_event", lambda event: calls.append("grant"))
    monkeypatch.setattr(order_funnel, "notify_order_confirmed", lambda event: calls.append("notify"))
    step = dict(main._stripe_webhook_steps())["camera_plan_v2_entitlements"]
    step({"id": "evt"})
    assert calls == ["grant", "notify"]


# ------------------------------------------------------------ billing start (reported, unchanged)

def test_the_subscription_still_starts_at_checkout_payment(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "billing-start@example.test", APPLIANCE)
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["mode"] == "subscription"
    assert not any("trial" in key or "billing_cycle_anchor" in key for key in fields)
