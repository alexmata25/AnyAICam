"""Friends & Family coupon percentage (Codex review of 41d2af4).

The approved policy is in pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF: 50% of
the recurring base plan, 25% of eligible analytics, 0% for hardware, the
one-time VMS license and Face Access (no coupon at all). The coupon configured
for a class must take exactly that percentage on Stripe and no fixed amount;
anything else pauses checkout (503) rather than giving the wrong discount.
"""
import pytest

from database_backend import override_target
from test_hybrid_build_system_flow import (_checkout, _checkouts, _customer_id, _new_customer,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

PLAN_ONLY = "plan=ai_local&cameras=3"


@pytest.fixture()
def coupons(monkeypatch):
    """Stripe's coupons as a test sets them; reads are counted."""
    import main
    state = {"coupon_ff_base": {"percent_off": 50, "valid": True}, "coupon_ff_analytics": {"percent_off": 25, "valid": True},
             "reads": 0, "fail": False}
    previous = main.stripe_api_get

    def get(path):
        if path.startswith("/v1/coupons/"):
            state["reads"] += 1
            if state["fail"]:
                raise OSError("Stripe unreachable")
            coupon_id = path.split("/")[3].split("?")[0]
            return {"id": coupon_id, **state.get(coupon_id, {})}
        return previous(path)
    monkeypatch.setattr(main, "stripe_api_get", get)
    return state


def _approved(client, mail, db_path, email):
    import friends_family
    _new_customer(client, mail, email, PLAN_ONLY)
    customer_id = _customer_id(db_path, email)
    with override_target(sqlite_path=str(db_path)):
        request, _ = friends_family.create_request(customer_id=customer_id, email=email)
        friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")
    return customer_id


def test_the_policy_is_authoritative_in_the_catalog():
    import pricing_catalog
    assert pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF == {"base": 50, "analytics": 25, "hardware": 0, "vms_license": 0, "face_access": 0}


def test_a_correctly_configured_coupon_is_used(shop, db_path, coupons):
    client, captured, mail, _, _ = shop
    customer_id = _approved(client, mail, db_path, "ok@example.test")
    assert _checkout(client).status_code == 200
    assert _checkouts(captured)[-1]["discounts[0][coupon]"] == "coupon_ff_base"
    import friends_family
    with override_target(sqlite_path=str(db_path)):
        assert friends_family.checkout_discount(customer_id, "analytics")["coupon"] == "coupon_ff_analytics"


@pytest.mark.parametrize("configured", [{"percent_off": 25, "valid": True}, {"percent_off": 100, "valid": True},
                                        {"amount_off": 500, "currency": "usd", "valid": True},
                                        {"percent_off": 50, "amount_off": 100, "valid": True}, {"percent_off": 50, "valid": False}, {}])
def test_a_base_coupon_that_does_not_match_the_policy_pauses_checkout(shop, db_path, coupons, configured):
    client, captured, mail, _, _ = shop
    coupons["coupon_ff_base"] = configured
    _approved(client, mail, db_path, "bad@example.test")
    response = _checkout(client)
    assert response.status_code == 503 and "COUPON_MISMATCH" in response.json()["detail"]
    assert _checkouts(captured) == []


def test_an_analytics_coupon_with_the_base_percentage_is_refused(shop, db_path, coupons):
    import friends_family
    client, _, mail, _, _ = shop
    customer_id = _approved(client, mail, db_path, "analytics@example.test")
    coupons["coupon_ff_analytics"] = {"percent_off": 50, "valid": True}
    with override_target(sqlite_path=str(db_path)):
        with pytest.raises(RuntimeError, match="COUPON_MISMATCH"):
            friends_family.checkout_discount(customer_id, "analytics")


def test_an_unreadable_coupon_pauses_checkout(shop, db_path, coupons):
    client, captured, mail, _, _ = shop
    coupons["fail"] = True
    _approved(client, mail, db_path, "unread@example.test")
    response = _checkout(client)
    assert response.status_code == 503 and "COUPON_UNVERIFIED" in response.json()["detail"] and _checkouts(captured) == []


def test_excluded_classes_never_carry_a_coupon(shop, db_path, coupons):
    import friends_family
    client, _, mail, _, _ = shop
    customer_id = _approved(client, mail, db_path, "excluded@example.test")
    with override_target(sqlite_path=str(db_path)):
        for excluded in ("hardware", "vms_license", "face_access"):
            assert friends_family.checkout_discount(customer_id, excluded)["coupon"] is None
    assert coupons["reads"] == 0  # nothing to verify when there is no coupon


def test_a_verified_coupon_is_read_from_stripe_once(shop, db_path, coupons):
    import friends_family
    client, _, mail, _, _ = shop
    customer_id = _approved(client, mail, db_path, "once@example.test")
    with override_target(sqlite_path=str(db_path)):
        for _ in range(3):
            friends_family.checkout_discount(customer_id, "base")
    assert coupons["reads"] == 1
