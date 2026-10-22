"""Owner 2026-10-22: AnyAiCam accepts orders before the 1.2.7 installer is released. While no Linux installer is
published (customer_downloads.current_installer("linux") is None), an own-PC customer is told so BEFORE payment (order
summary) and after it (order-complete page and confirmation email), and is never sent to a missing or old download.
Once an installer is published, every page and email is exactly as before. Appliance (hardware) orders are unchanged.
Wording only: no billing, entitlement or checkout behaviour changes.
"""
import pytest

from test_hardware_delivery_flow import OWN_PC, _paid  # noqa: F401
from test_hardware_delivery_flow import _new_customer  # noqa: F401
from test_hybrid_build_system_flow import db_path, license_portal, package, portal, shop, site, storage  # noqa: F401

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


@pytest.fixture()
def no_installer(monkeypatch):
    import customer_downloads
    monkeypatch.setattr(customer_downloads, "current_installer", lambda platform: None)


@pytest.fixture()
def installer(monkeypatch):
    import customer_downloads
    monkeypatch.setattr(customer_downloads, "current_installer", lambda platform: {"version": "1.2.7"} if platform == "linux" else None)


OWNER_NOTICE = ("Your AnyAiCam software installer is not yet available for download. You can complete your purchase now, "
                "and the installer will become available in your AnyAiCam account when it is ready.")


def test_order_summary_shows_the_owner_notice_before_checkout_session_creation(shop, db_path, no_installer):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "pending-summary@example.test", OWN_PC)
    sessions_before = [c for c in captured if c.get("mode")]
    page = client.get("/order-summary").text
    assert OWNER_NOTICE in page and 'id="order-installer-pending"' in page
    assert page.index('id="order-installer-pending"') < page.index('id="order-checkout"')  # above the pay button
    assert [c for c in captured if c.get("mode")] == sessions_before == []  # no Stripe Checkout Session exists yet
    response = client.post("/api/v2/customer/build-order/checkout", json={})
    assert response.status_code == 200 and response.json().get("checkout_url")  # the purchase can still be completed
    session = [c for c in captured if c.get("mode")][-1]
    assert session["mode"] == "payment"  # owner billing rule (2026-10-22): the licence now, the plan from activation


def test_order_summary_unchanged_when_an_installer_is_published(shop, db_path, installer):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "published-summary@example.test", OWN_PC)
    page = client.get("/order-summary").text
    assert 'id="order-installer-pending"' not in page and 'id="order-checkout"' in page


def test_order_complete_says_coming_soon_and_offers_no_download(shop, db_path, no_installer):
    import order_funnel
    client, captured, mail, _, _ = shop
    _paid(client, mail, db_path, "pending-complete@example.test", OWN_PC, captured)
    html = client.get("/order-complete").text
    assert "Your AnyAiCam installer is coming soon." in html and order_funnel.INSTALLER_PENDING_TEXT in html
    assert 'id="order-download-pending"' in html and 'id="order-ready"' in html
    assert "/api/customer/downloads/" not in html and 'id="order-download"' not in html
    assert 'id="order-setup"' not in html and "Download AnyAiCam and install it" not in html
    assert 'id="order-account"' in html


def test_confirmation_email_promises_no_download_while_pending(no_installer):
    import order_funnel
    kind, subject, text, html = order_funnel._order_email("Bea", {"own_pc": 1}, None, "AI Local plan for 5 cameras")
    assert kind == "account_ready"
    assert order_funnel.INSTALLER_PENDING_TEXT in text and "to download AnyAiCam" not in text
    assert "Your one-time AnyAiCam VMS software license is on your account." in text


def test_confirmation_email_unchanged_when_an_installer_is_published(installer):
    import order_funnel
    kind, subject, text, html = order_funnel._order_email("Bea", {"own_pc": 1}, None, "AI Local plan for 5 cameras")
    assert "to download AnyAiCam (for your own PC)" in text and order_funnel.INSTALLER_PENDING_TEXT not in text


def test_installed_pc_email_is_unchanged(no_installer):
    import order_funnel
    kind, subject, text, html = order_funnel._order_email("Bea", {"own_pc": 1}, None, "AI Local plan for 5 cameras", installed=True)
    assert "already installed on your computer" in text and order_funnel.INSTALLER_PENDING_TEXT not in text


def test_hardware_order_email_is_unchanged(no_installer):
    import order_funnel
    delivery = {"state": "preparing", "order": {"product_name": "AnyAiCam Starter"}}
    kind, subject, text, html = order_funnel._order_email("Bea", {"appliance": "AIC-APPLIANCE-RYZEN-STARTER"}, delivery,
                                                          "Hybrid plan for 8 cameras")
    assert kind == "hardware_order_confirmation" and "is being prepared" in text
    assert order_funnel.INSTALLER_PENDING_TEXT not in text


def test_installer_available_follows_the_download_route(monkeypatch):
    import customer_downloads
    import order_funnel
    monkeypatch.setattr(customer_downloads, "current_installer", lambda platform: None)
    assert order_funnel.installer_available() is False
    monkeypatch.setattr(customer_downloads, "current_installer", lambda platform: {"version": "1.2.7"})
    assert order_funnel.installer_available() is True

    def boom(platform):
        raise RuntimeError("storage down")
    monkeypatch.setattr(customer_downloads, "current_installer", boom)
    assert order_funnel.installer_available() is False  # never claims a download it cannot confirm
