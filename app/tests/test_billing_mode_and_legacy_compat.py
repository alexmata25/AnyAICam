"""Billing compatibility (owner-approved 2026-10-15).

1. Stripe test and live customers are kept apart: a customer known to belong to
   the other Stripe mode never blocks or serves checkout or Manage billing; the
   mode is Stripe's own livemode answer, learned once and cached; an unknown mode
   fails closed (no new customer, no portal).
2. A legacy Local/Hybrid camera-slot plan and the per-camera system agree: My
   subscription and the checkout backend use one definition (existing_plan), so
   the page never offers a picker that the backend refuses.
3. Nothing is deleted, cancelled or migrated; per-camera prices are unchanged;
   hardware stays separate.
"""
import sqlite3
from datetime import datetime

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hardware_first_checkout import ORDER, _signup_and_verify, db_path, hw, license_portal, portal, site  # noqa: F401

pytestmark = pytest.mark.usefixtures("stripe_follows_events")
TEST_KEY, LIVE_KEY = "sk_test_billingcompat", "sk_live_billingcompat"


@pytest.fixture()
def stripe_modes(monkeypatch):
    """Stripe's GET /v1/customers/{id}, per customer: (status, body). Records every lookup."""
    import main
    import stripe_customer_mode
    state = {"answers": {}, "lookups": []}

    def use_key(key):
        monkeypatch.setenv("ANYAICAM_STRIPE_SECRET_KEY", key)
        monkeypatch.setattr(main, "STRIPE_SECRET_KEY", key)

    def fake_get(customer):
        state["lookups"].append(customer)
        return state["answers"].get(customer, (0, {}))
    monkeypatch.setattr(stripe_customer_mode, "_get_customer", fake_get)
    state["use_key"] = use_key
    use_key(TEST_KEY)
    return state


def _test_customer(cus, livemode):
    return 200, {"id": cus, "object": "customer", "livemode": livemode}


def _other_mode(cus, exists_in):
    return 404, {"error": {"message": f"No such customer: '{cus}'; a similar object exists in {exists_in} mode, but a "
                                      f"{'live' if exists_in == 'test' else 'test'} mode key was used to make this request."}}


def _owner(hw, db_path, email):
    _signup_and_verify(hw, email)
    customer_id = _one(db_path, "SELECT id FROM customers WHERE email=?", (email,))["id"]
    assert _signed_in(_login(hw["client"], email, customer_only=True))
    return customer_id


def _legacy_plan(db_path, customer_id, cus=None, product="camera_slots_local", quantity=8, status="active"):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_customer_id,"
                 "stripe_subscription_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (f"ent-{customer_id}-{product}", customer_id, product, quantity, status, cus, "sub_legacy_1", now, now))
    conn.commit()
    conn.close()


def _snapshot(db_path, customer_id):
    return sqlite3.connect(db_path).execute("SELECT product,status,camera_slot_quantity,stripe_customer_id FROM customer_entitlements "
                                            "WHERE customer_id=? ORDER BY product", (customer_id,)).fetchall()


def _plan_checkout(hw, plan="hybrid", cameras=4):
    return hw["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": plan, "camera_quantity": cameras})


def _subscription_sessions(hw):
    return [f for f in hw["captured"] if f.get("mode") == "subscription"]


# ------------------------------------------------------------ legacy plan: page and backend agree

def test_a_legacy_plan_is_shown_and_checkout_says_why_instead_of_doing_nothing(hw, db_path, stripe_modes):
    customer_id = _owner(hw, db_path, "legacy@example.test")
    stripe_modes["answers"]["cus_legacytest01"] = _test_customer("cus_legacytest01", False)
    _legacy_plan(db_path, customer_id, "cus_legacytest01")
    before = _snapshot(db_path, customer_id)
    response = _plan_checkout(hw)
    assert response.status_code == 409 and "earlier AnyAiCam camera plan" in response.json()["detail"]
    assert "Manage billing" in response.json()["detail"]
    page = hw["client"].get("/subscription-portal").text
    assert 'id="v2-legacy-plan"' in page and "AnyAiCam Local (earlier plan) · 8 cameras" in page
    assert 'id="v2-checkout-button"' not in page  # no picker the backend would refuse
    assert 'id="manage-billing-button"' in page
    assert _subscription_sessions(hw) == []
    assert _snapshot(db_path, customer_id) == before  # nothing changed, cancelled or migrated


def test_a_legacy_plan_with_unknown_billing_details_fails_closed_with_a_support_message(hw, db_path, stripe_modes):
    customer_id = _owner(hw, db_path, "unknown@example.test")
    _legacy_plan(db_path, customer_id, None)  # no Stripe customer recorded
    response = _plan_checkout(hw)
    assert response.status_code == 409 and "contact AnyAiCam support" in response.json()["detail"]
    page = hw["client"].get("/subscription-portal").text
    assert "couldn&#x27;t confirm" in page or "couldn't confirm" in page
    assert 'id="v2-checkout-button"' not in page
    assert _subscription_sessions(hw) == []


def test_the_storage_trial_uses_the_same_plan_rule(hw, db_path, stripe_modes):
    import storage_trial
    customer_id = _owner(hw, db_path, "trial@example.test")
    stripe_modes["answers"]["cus_trial0001"] = _test_customer("cus_trial0001", False)
    _legacy_plan(db_path, customer_id, "cus_trial0001")
    with override_target(sqlite_path=str(db_path)):
        with pytest.raises(storage_trial.TrialError) as refused:
            storage_trial.grant(customer_id, admin={"email": "admin@example.test"}, reason="evaluation")
    assert refused.value.status_code == 409 and "already has a camera plan" in refused.value.detail


def test_no_plan_still_offers_the_picker_at_the_verified_prices(hw, db_path, stripe_modes):
    import per_camera_billing as billing
    _owner(hw, db_path, "fresh@example.test")
    page = hw["client"].get("/subscription-portal").text
    assert 'id="v2-checkout-button"' in page and 'id="v2-legacy-plan"' not in page
    assert {k: p["monthly_cents_per_camera"] for k, p in billing.PLANS.items()} == {"basic_local": 999, "ai_local": 1499, "hybrid": 1999}
    assert _plan_checkout(hw).status_code == 200
    fields = _subscription_sessions(hw)[-1]
    assert fields["line_items[0][price]"] == "price_srv_hybrid" and fields["line_items[0][quantity]"] == "4"


# ------------------------------------------------------------ test / live separation

def test_a_test_mode_legacy_plan_does_not_block_live_checkout_and_a_live_customer_is_created(hw, db_path, stripe_modes):
    customer_id = _owner(hw, db_path, "golive@example.test")
    _legacy_plan(db_path, customer_id, "cus_oldtest0001")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO stripe_customer_bindings(customer_id,stripe_customer_id,created_at) VALUES(?,?,?)",
                 (customer_id, "cus_oldtest0001", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    stripe_modes["use_key"](LIVE_KEY)
    stripe_modes["answers"]["cus_oldtest0001"] = _other_mode("cus_oldtest0001", "test")
    before = _snapshot(db_path, customer_id)
    response = _plan_checkout(hw)
    assert response.status_code == 200, response.text
    fields = _subscription_sessions(hw)[-1]
    assert fields["customer"] != "cus_oldtest0001" and fields["customer"].startswith("cus_test_")  # the fake's new customer
    mode_binding = _one(db_path, "SELECT * FROM stripe_customer_mode_bindings WHERE customer_id=?", (customer_id,))
    assert mode_binding["livemode"] == 1 and mode_binding["stripe_customer_id"] == fields["customer"]
    # The original binding and the old plan are untouched.
    assert _one(db_path, "SELECT stripe_customer_id FROM stripe_customer_bindings WHERE customer_id=?", (customer_id,))["stripe_customer_id"] == "cus_oldtest0001"
    assert _snapshot(db_path, customer_id) == before
    assert _one(db_path, "SELECT livemode,source FROM stripe_customer_modes WHERE stripe_customer_id='cus_oldtest0001'") == \
        {"livemode": 0, "source": "other_mode_hint"}
    # A second checkout reuses the live customer without asking Stripe about the old one again.
    lookups = len(stripe_modes["lookups"])
    hw["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "basic_local", "camera_quantity": 2})
    assert len(stripe_modes["lookups"]) == lookups


def test_the_live_customer_key_is_distinct_from_the_test_one(hw, db_path, stripe_modes, monkeypatch):
    import main
    keys = []
    inner = main.stripe_api_post

    def recording(path, fields, idempotency_key=None):
        if path == "/v1/customers":
            keys.append(idempotency_key)
        return inner(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", recording)
    customer_id = _owner(hw, db_path, "keys@example.test")
    stripe_modes["use_key"](LIVE_KEY)
    assert _plan_checkout(hw).status_code == 200
    assert keys == [f"anyaicam-customer-{customer_id}-live"]


def test_an_unknown_mode_never_creates_a_duplicate_customer(hw, db_path, stripe_modes):
    customer_id = _owner(hw, db_path, "flaky@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO stripe_customer_bindings(customer_id,stripe_customer_id,created_at) VALUES(?,?,?)",
                 (customer_id, "cus_flaky00001", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    stripe_modes["use_key"](LIVE_KEY)
    stripe_modes["answers"]["cus_flaky00001"] = (0, {})  # network error
    response = _plan_checkout(hw)
    assert response.status_code == 503 and "temporarily unavailable" in response.json()["detail"]
    assert _subscription_sessions(hw) == []
    assert _one(db_path, "SELECT COUNT(*) AS n FROM stripe_customer_modes")["n"] == 0  # nothing decided on an error
    stripe_modes["answers"]["cus_flaky00001"] = _test_customer("cus_flaky00001", True)  # Stripe answers later: a live customer
    assert _plan_checkout(hw).status_code == 200
    assert _subscription_sessions(hw)[-1]["customer"] == "cus_flaky00001"


def test_a_500_or_unrelated_404_is_not_read_as_a_mode(hw, db_path, stripe_modes):
    import stripe_customer_mode
    stripe_modes["use_key"](LIVE_KEY)
    stripe_modes["answers"]["cus_x500"] = (500, {"error": {"message": "a similar object exists in test mode"}})
    stripe_modes["answers"]["cus_x404"] = (404, {"error": {"message": "No such customer: 'cus_x404'"}})
    with override_target(sqlite_path=str(db_path)):
        assert stripe_customer_mode.customer_livemode("cus_x500", lookup=True) is None
        assert stripe_customer_mode.customer_livemode("cus_x404", lookup=True) is None
        assert stripe_customer_mode.customer_livemode("not-a-customer", lookup=True) is None


def test_manage_billing_uses_only_the_current_modes_customer(hw, db_path, stripe_modes, monkeypatch):
    import customer_billing
    import main
    customer_id = _owner(hw, db_path, "portal@example.test")
    _legacy_plan(db_path, customer_id, "cus_portaltest1")
    conn = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_customer_id,created_at,updated_at) "
                 "VALUES('ent-live',?, 'vms_license', 8, 'active', 'cus_portallive1', ?, ?)", (customer_id, now, now))
    conn.commit()
    conn.close()
    monkeypatch.setattr(customer_billing, "portal_enabled", lambda: True)
    monkeypatch.setattr(customer_billing, "_portal_configuration", lambda m: {"id": "bpc_1"})
    monkeypatch.setattr(customer_billing, "configuration_problems", lambda c: [])
    opened = []
    inner = main.stripe_api_post

    def post(path, fields, idempotency_key=None):
        if path == "/v1/billing_portal/sessions":
            opened.append(dict(fields)["customer"])
            return {"url": "https://billing.stripe.test/session/1"}
        return inner(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", post)
    # Test key: both look like this mode's customers -> still "needs attention" (unchanged behaviour).
    stripe_modes["answers"]["cus_portaltest1"] = _test_customer("cus_portaltest1", False)
    stripe_modes["answers"]["cus_portallive1"] = _other_mode("cus_portallive1", "live")
    assert hw["client"].post("/api/customer/billing-portal").status_code == 200 and opened == ["cus_portaltest1"]
    # Live key: the test customer is left out, the live one opens.
    stripe_modes["use_key"](LIVE_KEY)
    assert hw["client"].post("/api/customer/billing-portal").status_code == 200 and opened[-1] == "cus_portallive1"


def test_manage_billing_fails_closed_while_a_customers_mode_is_unknown(hw, db_path, stripe_modes, monkeypatch):
    import customer_billing
    customer_id = _owner(hw, db_path, "portalflaky@example.test")
    _legacy_plan(db_path, customer_id, "cus_portalflaky")
    monkeypatch.setattr(customer_billing, "portal_enabled", lambda: True)
    stripe_modes["use_key"](LIVE_KEY)
    response = hw["client"].post("/api/customer/billing-portal")
    assert response.status_code == 503 and "not available right now" in response.json()["detail"]


def test_page_views_never_call_stripe_for_modes(hw, db_path, stripe_modes):
    customer_id = _owner(hw, db_path, "pageview@example.test")
    _legacy_plan(db_path, customer_id, "cus_pageview01")
    stripe_modes["lookups"].clear()
    for _ in range(3):
        assert hw["client"].get("/subscription-portal").status_code == 200
    assert stripe_modes["lookups"] == []


def test_hardware_purchase_stays_separate_from_a_legacy_plan(hw, db_path, stripe_modes):
    customer_id = _owner(hw, db_path, "hwlegacy@example.test")
    stripe_modes["answers"]["cus_hwlegacy01"] = _test_customer("cus_hwlegacy01", False)
    _legacy_plan(db_path, customer_id, "cus_hwlegacy01")
    response = hw["client"].post("/api/hardware-checkout", json={**ORDER, "email": "ignored@example.test"})
    assert response.status_code == 200, response.text
    fields = [f for f in hw["captured"] if f.get("metadata[anyaicam_build_order]") == "hardware_only"][-1]
    assert fields["mode"] == "payment" and not any(k.startswith("subscription_data") for k in fields)
