"""Residential VMS -- Ryzen 5 catalog entry (owner-approved preparation, 2026-10-16).

The product exists in the hardware catalog at the website's $949.99 with an
included VMS software license, but it is NOT orderable until the owner
approves selling it: every ordering path refuses or skips it, the website
catalog omits it, and nothing is ever charged for it.
"""
import sqlite3
from datetime import datetime

import pytest

from test_hardware_checkout_billing import _buy, _session_posts, hw  # noqa: F401 -- fixture
from test_stripe_billing_launch import env  # noqa: F401 -- fixture

SKU = "AIC-APPLIANCE-RYZEN-RESIDENTIAL"


def test_catalog_entry_price_name_and_price_env_var():
    import hardware_orders
    entry = next(item for item in hardware_orders.HARDWARE_CATALOG if item[0] == SKU)
    assert entry == (SKU, "ryzen_residential", "AnyAiCam Residential VMS — Ryzen 5", 94999,
                     "ANYAICAM_STRIPE_PRICE_RYZEN_RESIDENTIAL")
    assert entry[1] not in {"starter", "professional", "enterprise", "residential"}  # never a bare plan key


def test_not_orderable_while_the_owner_has_not_approved_selling_it():
    import hardware_orders
    assert SKU in hardware_orders.HARDWARE_NOT_YET_SELLABLE
    assert hardware_orders.orderable(SKU) is False
    assert all(hardware_orders.orderable(sku) for sku, *_ in hardware_orders.HARDWARE_CATALOG if sku != SKU)
    assert hardware_orders.orderable("AIC-APPLIANCE-NOPE") is False


def test_website_catalog_omits_it():
    import hardware_orders
    skus = [item["sku"] for item in hardware_orders.hardware_catalog_for_website()]
    assert SKU not in skus and "AIC-APPLIANCE-RYZEN-STARTER" in skus


def test_older_hardware_checkout_refuses_it_without_calling_stripe(hw):
    response = _buy(hw, sku=SKU)
    assert response.status_code == 409 and "not available to order online yet" in response.json()["detail"]
    assert _session_posts(hw) == []


def test_build_your_system_and_hardware_first_selections_drop_it():
    import hardware_checkout
    import per_camera_billing
    assert hardware_checkout.selection_from({"appliance": SKU, "cameras": "4", "plan": "hybrid"}) is None
    assert hardware_checkout.selection_from({"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "cameras": "4", "plan": "hybrid"})
    selection = per_camera_billing.purchase_selection("hybrid", 4, appliance=SKU)
    assert "appliance" not in selection


def test_no_sales_commission_is_defined_for_it_yet():
    import pricing_catalog
    assert SKU not in pricing_catalog.HARDWARE_COMMISSION_CENTS  # owner decision pending: earns nothing


def test_a_paid_residential_order_includes_the_vms_software_license(env):  # noqa: F811
    import customer_entitlements
    now = datetime.now().isoformat()
    conn = sqlite3.connect(env["path"])
    conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,status,"
                 "created_at,updated_at) VALUES('hw-res','cust-A',?,?,'price_test_residential',1,94999,'paid',?,?)",
                 (SKU, "AnyAiCam Residential VMS — Ryzen 5", now, now))
    conn.commit()
    conn.close()
    assert customer_entitlements.appliance_includes_vms_license("cust-A") is True


def test_once_released_it_charges_exactly_the_published_price(hw, monkeypatch):
    import hardware_orders
    monkeypatch.setattr(hardware_orders, "HARDWARE_NOT_YET_SELLABLE", {})
    price = "price_hw_residential"
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_RYZEN_RESIDENTIAL", price)
    monkeypatch.setattr(hardware_orders, "HARDWARE_PRICE_MAP", hardware_orders._load_hardware_price_map())
    assert _buy(hw, sku=SKU).status_code == 200
    [(fields, _)] = _session_posts(hw)
    assert fields["mode"] == "payment" and fields["line_items[0][price]"] == price
    assert hardware_orders.resolve_hardware_sku(price)["amount_cents"] == 94999
