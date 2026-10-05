"""Friends & Family scope on Build Your System orders (owner policy,
2026-10-05, after Codex review of 453bd1c).

50% of the recurring BASE plan, 25% of eligible separate analytics; hardware,
the one-time VMS license and Face Access are excluded. An own-PC Checkout
holds both the one-time license and the recurring plan, and a Checkout coupon
covers every line unless Stripe restricts it to Products, so the coupon is
verified on Stripe as limited to the plan's Product before checkout; anything
else refuses the checkout (fail closed). The browser sends nothing that sets
a discount, price or Price ID.
"""
import pytest

from database_backend import override_target
from test_build_order_billing import APPLIANCE, OWN_PC, _activate, _paid, stripe_subscriptions  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _checkout, _checkouts, _customer_id, _new_customer,  # noqa: F401
                                           _paid_checkout_event, _record_build_order,
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

PRODUCTS = {PRICES["ai_local"]: "prod_plan_ai_local", "price_vms_8": "prod_vms_license_8"}


def _approve(db_path, customer_id, email):
    import friends_family
    with override_target(sqlite_path=str(db_path)):
        request, _ = friends_family.create_request(customer_id=customer_id, email=email)
        friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")


@pytest.fixture()
def stripe_coupon(monkeypatch):
    """Stripe's view of the Friends & Family coupon and of each Price's Product."""
    import main
    state = {"applies_to": ["prod_plan_ai_local"], "reads": [], "fail": False}
    previous = main.stripe_api_get

    def get(path):
        state["reads"].append(path)
        if path.startswith("/v1/coupons/") or path.startswith("/v1/prices/"):
            if state["fail"]:
                raise OSError("Stripe unreachable")
            if path.startswith("/v1/coupons/"):
                from conftest import policy_coupon
                coupon = dict(policy_coupon(path.split("/")[3].split("?")[0]) or {"valid": True, "percent_off": 50})
                if state["applies_to"] is not None:
                    coupon["applies_to"] = {"products": list(state["applies_to"])}
                return coupon
            price = path.rsplit("/", 1)[-1]
            return {"id": price, "product": PRODUCTS.get(price, f"prod_for_{price}")}
        return previous(path)
    monkeypatch.setattr(main, "stripe_api_get", get)
    return state


def _own_pc_customer(client, mail, db_path, email, friends_family=False):
    _new_customer(client, mail, email, OWN_PC)
    customer_id = _customer_id(db_path, email)
    if friends_family:
        _approve(db_path, customer_id, email)
    return customer_id


def test_a_normal_own_pc_checkout_charges_the_full_license_and_full_plan(shop, db_path, stripe_coupon):
    client, captured, mail, _, _ = shop
    _own_pc_customer(client, mail, db_path, "full@example.test")
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert "discounts[0][coupon]" not in fields
    assert fields["allow_promotion_codes"] == "false"  # a promotion code could not be scoped away from the license
    assert (fields["line_items[0][price]"], fields["line_items[1][price]"]) == (PRICES["ai_local"], "price_vms_8")
    assert fields["metadata[anyaicam_friends_family]"] == "none"


def test_a_friends_and_family_own_pc_checkout_discounts_only_the_plan(shop, db_path, stripe_coupon):
    client, captured, mail, _, _ = shop
    _own_pc_customer(client, mail, db_path, "ff@example.test", friends_family=True)
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["discounts[0][coupon]"] == "coupon_ff_base"
    assert fields["line_items[1][price]"] == "price_vms_8" and fields["line_items[1][quantity]"] == "1"  # full license line
    assert "allow_promotion_codes" not in fields or fields["allow_promotion_codes"] != "true"
    # Verified on Stripe: the coupon is limited to the plan's Product, not the license's.
    assert any(path.startswith("/v1/coupons/coupon_ff_base") for path in stripe_coupon["reads"])
    assert f"/v1/prices/{PRICES['ai_local']}" in stripe_coupon["reads"] and "/v1/prices/price_vms_8" in stripe_coupon["reads"]


@pytest.mark.parametrize("applies_to", [None, [], ["prod_plan_ai_local", "prod_vms_license_8"], ["prod_vms_license_8"], ["prod_other"]])
def test_a_coupon_that_could_discount_the_license_refuses_the_checkout(shop, db_path, stripe_coupon, applies_to):
    client, captured, mail, _, _ = shop
    stripe_coupon["applies_to"] = applies_to
    _own_pc_customer(client, mail, db_path, "unsafe@example.test", friends_family=True)
    response = _checkout(client)
    assert response.status_code == 503 and "checkout is paused" in response.json()["detail"]
    assert _checkouts(captured) == []


def test_an_unreadable_coupon_refuses_the_checkout(shop, db_path, stripe_coupon):
    client, captured, mail, _, _ = shop
    stripe_coupon["fail"] = True
    _own_pc_customer(client, mail, db_path, "unread@example.test", friends_family=True)
    assert _checkout(client).status_code == 503 and _checkouts(captured) == []


def test_hardware_is_never_discounted(shop, db_path, stripe_coupon):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "hwff@example.test", APPLIANCE)
    _approve(db_path, _customer_id(db_path, "hwff@example.test"), "hwff@example.test")
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert "discounts[0][coupon]" not in fields and fields["allow_promotion_codes"] == "false"


def test_the_appliance_storage_plan_gets_the_base_discount_at_activation(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "actff@example.test", APPLIANCE, captured)
    _approve(db_path, customer_id, "actff@example.test")
    assert _activate(db_path, customer_id)["status"] == "started"
    created = stripe_subscriptions["created"][0]
    assert created["discounts[0][coupon]"] == "coupon_ff_base" and created["items[0][price]"] == PRICES["hybrid"]
    assert not any(key.startswith("add_invoice_items") for key in created)  # only the recurring plan is on it


def test_the_discount_classes_follow_the_policy(db_path, shop):
    import friends_family
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "classes@example.test", OWN_PC)
    customer_id = _customer_id(db_path, "classes@example.test")
    _approve(db_path, customer_id, "classes@example.test")
    with override_target(sqlite_path=str(db_path)):
        assert friends_family.checkout_discount(customer_id, "base")["coupon"] == "coupon_ff_base"  # 50% plan
        assert friends_family.checkout_discount(customer_id, "analytics")["coupon"] == "coupon_ff_analytics"  # 25% analytics
        for excluded in ("hardware", "vms_license", "face_access"):
            assert friends_family.checkout_discount(customer_id, excluded)["coupon"] is None
    import pricing_catalog
    assert pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF == {"base": 50, "analytics": 25, "hardware": 0, "vms_license": 0, "face_access": 0}


@pytest.mark.parametrize("body", [{"discounts": [{"coupon": "coupon_ff_base"}]}, {"coupon": "coupon_evil"}, {"allow_promotion_codes": True}])
def test_the_browser_cannot_set_a_discount(shop, db_path, body):
    client, captured, mail, _, _ = shop
    _own_pc_customer(client, mail, db_path, "nodisc@example.test")
    response = client.post("/api/v2/customer/build-order/checkout", json=body)
    assert response.status_code == 422 and _checkouts(captured) == []


def test_a_retried_own_pc_checkout_and_webhook_stay_single(shop, db_path, stripe_coupon):
    client, captured, mail, _, _ = shop
    customer_id = _own_pc_customer(client, mail, db_path, "retry@example.test", friends_family=True)
    first, second = _checkout(client), _checkout(client)
    assert first.json()["checkout_url"] == second.json()["checkout_url"] and len(_checkouts(captured)) == 1
    event = _paid_checkout_event(captured, customer_id)
    for _ in range(2):
        assert _record_build_order(db_path, event)["status"] == "vms_license_granted"
    import sqlite3
    count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM customer_entitlements WHERE customer_id=? AND product='vms_license'",
                                             (customer_id,)).fetchone()[0]
    assert count == 1
