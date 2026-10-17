"""Where a hardware-first customer lands on sign-in (Green, 2026-10-16).

/customer-account is the default sign-in destination. A customer who paid for
an appliance that has not arrived belongs on My orders ("waiting for
delivery"), not in the setup wizard; and a saved Build Your System selection
whose appliance is already paid for must never send them back to "Pay for
hardware".
"""
import sqlite3

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _signed_in, site  # noqa: F401 -- fixtures
from test_hardware_first_checkout import _buy, _customer_id, _paid_event, _record, _signup_and_verify, hw  # noqa: F401
from test_pricing_ff_commission import db_path, license_portal, portal  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

SESSION = "cs_test_hw000001"
EMAIL = "buyer@example.test"


def _paid_and_signed_in(hw, db_path):
    _buy(hw)
    _record(db_path, _paid_event(hw, SESSION))
    _signup_and_verify(hw, EMAIL, next_path="/customer/orders")
    assert _signed_in(_login(hw["client"], EMAIL, customer_only=True))
    return _customer_id(db_path, EMAIL)


def _fulfillment(db_path, status):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET fulfillment_status=? WHERE sku LIKE 'AIC-APPLIANCE-%'", (status,))
    conn.commit()
    conn.close()


def _account(hw):
    return hw["client"].get("/customer-account", follow_redirects=False)


@pytest.mark.parametrize("fulfillment", ["paid", "preparing", "shipped"])
def test_equipment_on_its_way_signs_in_to_my_orders_not_setup(hw, db_path, fulfillment):
    _paid_and_signed_in(hw, db_path)
    _fulfillment(db_path, fulfillment)
    response = _account(hw)
    assert response.status_code == 303 and response.headers["location"] == "/customer/orders"


def test_delivered_equipment_goes_on_to_setup(hw, db_path):
    _paid_and_signed_in(hw, db_path)
    _fulfillment(db_path, "delivered")
    response = _account(hw)
    assert response.status_code == 303 and response.headers["location"] == "/customer/setup"


def test_a_saved_appliance_selection_never_returns_a_paid_customer_to_pay_for_hardware(hw, db_path):
    import per_camera_billing as billing
    customer_id = _paid_and_signed_in(hw, db_path)
    with override_target(sqlite_path=str(db_path)):
        billing.save_build_selection(customer_id, billing.purchase_selection("hybrid", "4", "AIC-APPLIANCE-RYZEN-STARTER", None))
        assert billing.build_progress(customer_id)["state"] == billing.BUILD_PENDING  # the old intent is still there
    response = _account(hw)
    assert response.status_code == 303 and response.headers["location"] == "/customer/orders"
    _fulfillment(db_path, "delivered")
    assert _account(hw).headers["location"] == "/customer/setup"


def test_a_refunded_appliance_order_no_longer_counts_as_paid(hw, db_path):
    import per_camera_billing as billing
    customer_id = _paid_and_signed_in(hw, db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET status='refunded' WHERE sku LIKE 'AIC-APPLIANCE-%'")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        assert not billing.hardware_order_paid(customer_id) and not billing.hardware_awaiting_delivery(customer_id)


@pytest.mark.parametrize("fulfillment", ["preparing", "delivered"])
def test_an_appliance_owner_is_never_given_an_own_pc_order_or_charged_the_licence(hw, db_path, fulfillment):
    import per_camera_billing as billing
    customer_id = _paid_and_signed_in(hw, db_path)
    _fulfillment(db_path, fulfillment)
    response = hw["client"].get("/order-summary?plan=basic_local&cameras=4", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/customer/orders"
    with override_target(sqlite_path=str(db_path)):
        assert billing.build_progress(customer_id) is None  # nothing saved, so no license line can be built
        billing.save_build_selection(customer_id, billing.purchase_selection("basic_local", "4"))
        assert not billing.build_progress(customer_id)["own_pc"]  # the appliance includes the license


def test_my_orders_offers_no_second_plan_once_one_is_active(hw, db_path, monkeypatch):
    import per_camera_billing as billing
    _paid_and_signed_in(hw, db_path)
    _fulfillment(db_path, "delivered")
    assert 'id="orders-choose-plan"' in hw["client"].get("/customer/orders").text
    monkeypatch.setattr(billing, "has_camera_plan", lambda customer_id, **_: True)
    html = hw["client"].get("/customer/orders").text
    assert 'id="orders-choose-plan"' not in html and 'id="orders-manage-plan"' in html and 'id="orders-setup"' in html


def test_order_complete_offers_the_download_the_catalog_serves(monkeypatch):
    """/order-complete decides from customer_downloads.current_installer (the approved catalog row, what the
    download route serves), not only the legacy latest.json record."""
    import customer_downloads
    import order_funnel
    import software_releases
    monkeypatch.setattr(customer_downloads, "latest_vms_installer", lambda: None)
    monkeypatch.setattr(software_releases, "latest_approved", lambda platform: {
        "version": "1.2.7", "build_commit": "c" * 40, "filename": "anyaicam-appliance-installer-1.2.7-vms-cccccccccccc.tar.gz",
        "size_bytes": 1, "sha256": "d" * 64, "storage_key": "k", "approved_at": "2026-10-16"} if platform == "linux" else None)
    assert 'id="order-download"' in order_funnel._software_body({"own_pc": 1}, "")
    monkeypatch.setattr(software_releases, "latest_approved", lambda platform: None)
    assert 'id="order-download-pending"' in order_funnel._software_body({"own_pc": 1}, "")
