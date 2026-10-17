"""One-time VMS software license repricing (owner approval 2026-10-17).

8 / 16 / 32 / 64 cameras: $24.99 / $37.49 / $62.49 / $99.99, one time (was $49.99 / $79.99 / $129.99 / $199.99).
Each license still grants exactly its camera capacity. Stripe Prices are immutable: new checkouts need the new
Prices (the guard refuses an old one); a tier's earlier Price IDs (<env var>_PREVIOUS) keep granting and resolving the
SAME capacity, so a checkout opened before the switch still grants and earlier purchases' refunds still resolve.
The monthly plans ($9.99 / $14.99 / $19.99 per camera) are separate and unchanged here.
"""
import pytest
from fastapi import HTTPException

import pricing_catalog as pc

NEW = {8: ("price_lic8_2499", 2499), 16: ("price_lic16_3749", 3749), 32: ("price_lic32_6249", 6249), 64: ("price_lic64_9999", 9999)}
OLD = {8: "price_lic8_4999_previous", 16: "price_lic16_7999_previous", 32: "price_lic32_12999_previous", 64: "price_lic64_19999_previous"}


@pytest.fixture()
def prices(monkeypatch):
    for capacity, cents, env in pc.VMS_LICENSES:
        monkeypatch.setenv(env, NEW[capacity][0])
        monkeypatch.setenv(env + pc.PREVIOUS_PRICE_SUFFIX, OLD[capacity])


def test_the_four_license_prices_and_capacities():
    assert [(capacity, cents) for capacity, cents, _env in pc.VMS_LICENSES] == [(8, 2499), (16, 3749), (32, 6249), (64, 9999)]
    assert [(lic["capacity"], lic["billing_type"]) for lic in pc.vms_licenses()] == [
        (8, "one_time"), (16, "one_time"), (32, "one_time"), (64, "one_time")]


@pytest.mark.parametrize("cameras,capacity,cents", [(1, 8, 2499), (8, 8, 2499), (9, 16, 3749), (16, 16, 3749),
                                                    (17, 32, 6249), (32, 32, 6249), (33, 64, 9999), (64, 64, 9999)])
def test_each_camera_count_buys_the_smallest_license_that_covers_it(cameras, capacity, cents):
    import build_orders
    tier = build_orders.vms_license_for(cameras)
    assert (tier["capacity"], tier["cents"]) == (capacity, cents)
    quote = build_orders.order_quote({"plan": "basic_local", "cameras": cameras, "own_pc": True})
    assert [(line["capacity"], line["cents"]) for line in quote["one_time"]] == [(capacity, cents)]
    assert quote["due_today_cents"] == cents + 999 * cameras  # the license once, plus the first month


def test_a_license_checkout_on_an_old_stripe_price_is_refused(monkeypatch):
    import main
    monkeypatch.setattr(main, "_VERIFIED_STRIPE_PRICES", {})
    monkeypatch.setattr(main, "stripe_api_get", lambda path: {"unit_amount": 4999, "currency": "usd", "active": True})
    with pytest.raises(HTTPException) as refused:
        main.require_stripe_price_matches_catalog(OLD[8], 2499, None)
    assert refused.value.status_code == 503 and "PRICE_MISMATCH" in str(refused.value.detail)


@pytest.mark.parametrize("capacity", [8, 16, 32, 64])
def test_current_and_previous_prices_grant_the_same_capacity(prices, capacity):
    import build_orders
    import customer_entitlements as ce
    mapping = ce._load_vms_license_map()
    for price_id in (NEW[capacity][0], OLD[capacity]):
        assert mapping[price_id]["camera_slot_maximum"] == capacity
    assert pc.vms_license_price_ids(next(env for c, _cents, env in pc.VMS_LICENSES if c == capacity))[0] == NEW[capacity][0]
    assert pc.find_vms_license(capacity)["stripe_price_id"] == NEW[capacity][0]  # a new purchase uses the new Price
    granted = []
    import customer_entitlements
    monkey = pytest.MonkeyPatch()
    monkey.setattr(customer_entitlements, "upsert_entitlement", lambda **kw: granted.append(kw) or {"id": "e1"})
    try:
        for price_id in (NEW[capacity][0], OLD[capacity]):
            result = build_orders._grant_vms_license({"id": "cs_1", "customer": "cus_1"}, "cust-1", {
                "anyaicam_vms_license_price_id": price_id, "anyaicam_vms_license_capacity": str(capacity)})
            assert result == {"status": "vms_license_granted", "entitlement_id": "e1", "capacity": capacity}
        unknown = build_orders._grant_vms_license({"id": "cs_2"}, "cust-1", {
            "anyaicam_vms_license_price_id": "price_somebody_else", "anyaicam_vms_license_capacity": str(capacity)})
        assert unknown["status"] == "rejected"
        # A Price of one tier never grants another tier's capacity.
        other = 16 if capacity == 8 else 8
        mismatch = build_orders._grant_vms_license({"id": "cs_3"}, "cust-1", {
            "anyaicam_vms_license_price_id": OLD[other], "anyaicam_vms_license_capacity": str(capacity)})
        assert mismatch["status"] == "rejected"
    finally:
        monkey.undo()
    assert [g["camera_slot_quantity"] for g in granted] == [capacity, capacity]


def test_the_monthly_plans_are_unchanged_by_the_license_repricing():
    import per_camera_billing as billing
    assert {key: plan["monthly_cents_per_camera"] for key, plan in billing.PLANS.items()} == {
        "basic_local": 999, "ai_local": 1499, "hybrid": 1999}
