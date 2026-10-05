"""Billing v2 (owner decision 2026-10-05): new camera plans are per-camera only.

POST /api/customer/camera-slots/checkout (the legacy fixed-capacity checkout)
must never start a NEW legacy purchase: a customer with no plan is sent to
the billing-v2 plans on My subscription, a per-camera account can never add a
parallel legacy subscription, and grandfathered legacy plan holders keep the
existing "already has an active plan" answer and their own paths. No Stripe
call is made for a refused request.
"""
import pytest

from database_backend import override_target
from test_camera_slot_checkout import _owner_cookie, _seed_tenant, client, db_path  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


def _post(test_client, plan_type="local", tier_label="1-8"):
    return test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": plan_type, "tier_label": tier_label},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )


def test_production_default_refuses_new_legacy_purchases():
    import main
    assert main.LEGACY_CAMERA_SLOT_NEW_PURCHASES is False


@pytest.mark.parametrize("plan_type", ["local", "hybrid"])
def test_a_customer_with_no_plan_is_sent_to_billing_v2_and_stripe_is_never_called(client, db_path, plan_type):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = _post(test_client, plan_type=plan_type)
    assert response.status_code == 409
    assert "per-camera" in response.json()["detail"] and "My subscription" in response.json()["detail"]
    assert captured == {}  # no Checkout Session was created


def test_a_lapsed_legacy_customer_is_also_sent_to_billing_v2(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8, status="cancelled")
    response = _post(test_client)
    assert response.status_code == 409 and "per-camera" in response.json()["detail"]
    assert captured == {}


def test_a_per_camera_account_can_never_add_a_parallel_legacy_subscription(client, db_path, monkeypatch):
    import main
    import per_camera_billing as billing
    import stripe_state
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    monkeypatch.setenv(billing.PRICE_ENV["ai_local"], "price_v2_ai_local")
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    with override_target(sqlite_path=str(db_path)):
        billing._upsert_current({"id": "sub_v2", "customer": "cus_v2", "status": "active",
                                 "metadata": {"anyaicam_customer_id": "cust-1"},
                                 "items": {"data": [{"id": "si", "quantity": 6, "price": {"id": "price_v2_ai_local"}}]}})
    for enabled in (False, True):  # refused even if legacy purchases were ever switched back on
        monkeypatch.setattr(main, "LEGACY_CAMERA_SLOT_NEW_PURCHASES", enabled)
        response = _post(test_client)
        assert response.status_code == 409
        assert "already has a per-camera plan" in response.json()["detail"]
    assert captured == {}


def test_a_grandfathered_legacy_plan_holder_keeps_the_existing_answer(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = _post(test_client, plan_type="hybrid")
    assert response.status_code == 409
    assert response.json()["detail"] == "Your account already has an active Local plan. Use Upgrade to Hybrid on My subscription."
    assert captured == {}
