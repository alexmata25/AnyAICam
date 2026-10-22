"""Owner 2026-10-22 (relayed by Red): the authenticated order-confirmation page shows what was bought, at what price, and
its status -- only from server records written after Stripe verification -- and links to My orders / My subscription so
a customer returning later can see the purchase. Display only: no billing, checkout or entitlement change.
"""
import pytest

from test_hardware_delivery_flow import APPLIANCE, OWN_PC, _hardware_order, _paid  # noqa: F401
from test_hybrid_build_system_flow import db_path, license_portal, package, portal, shop, site, storage  # noqa: F401

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


@pytest.fixture(autouse=True)
def no_installer(monkeypatch):
    import customer_downloads
    monkeypatch.setattr(customer_downloads, "current_installer", lambda platform: None)


def _section(html):
    assert 'id="order-purchases"' in html, html[:400]
    return html[html.index('id="order-purchases"'):]


def test_own_pc_order_lists_plan_and_licence_with_prices_and_status(shop, db_path):
    client, captured, mail, _, _ = shop
    _paid(client, mail, db_path, "items-ownpc@example.test", OWN_PC, captured)
    panel = _section(client.get("/order-complete").text)
    # Owner billing rule (2026-10-22): the plan is saved at the purchase price and is Pending Activation.
    assert "AnyAiCam AI Local plan · 5 licensed cameras" in panel and "5 × $14.99 = $74.95/month" in panel
    assert "Pending Activation" in panel
    assert "AnyAiCam VMS software license (up to 8 cameras)" in panel and "$24.99 one-time" in panel
    assert 'href="/customer/orders"' in panel and 'href="/subscription-portal"' in panel
    assert "Your AnyAiCam installer is coming soon." in client.get("/order-complete").text  # installer pending shown too


def test_the_page_ignores_anything_the_browser_sends(shop, db_path):
    client, captured, mail, _, _ = shop
    _paid(client, mail, db_path, "items-query@example.test", OWN_PC, captured)
    panel = _section(client.get("/order-complete?plan=hybrid&cameras=64&vms_licence=64&price=1").text)
    assert "AI Local plan · 5 licensed cameras" in panel and "Hybrid" not in panel and "64 cameras" not in panel


def test_hardware_order_lists_the_appliance_amount_and_preparation_status(shop, db_path):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "items-hw@example.test", APPLIANCE, captured)
    _hardware_order(db_path, customer_id, fulfillment="preparing")
    html = client.get("/order-complete").text
    panel = _section(html)
    assert "AnyAiCam Starter" in panel and "$1,249.99 one-time" in panel and "Being prepared" in panel
    assert "Hybrid plan · 8 licensed cameras" in panel and "8 × $19.99 = $159.92/month" in panel
    assert "Pending Activation" in panel and "when your AnyAiCam system is activated" in panel
    assert "Your AnyAiCam system is being prepared." in html


def test_hardware_status_labels_follow_fulfilment(shop, db_path):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "items-hw2@example.test", APPLIANCE, captured)
    _hardware_order(db_path, customer_id, fulfillment="paid")
    assert "Awaiting preparation" in _section(client.get("/order-complete").text)
    _hardware_order(db_path, customer_id, fulfillment="shipped", carrier="UPS", tracking_number="1Z999AA10123456784")
    assert "Shipped" in _section(client.get("/order-complete").text)
