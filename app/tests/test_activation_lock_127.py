"""VMS 1.2.7 activation flow (owner decision 2026-10-16).

The first purchase is made through Build Your System ("Activate Your System"); My subscription shows its purchase
controls disabled until a verified purchase activates the account, and the server refuses every My subscription
checkout before then. A customer whose own PC is already installed and linked returns to it after paying (no download),
and the one-time licence is never sold twice. Stripe is the launch suite's fake; nothing here reaches Stripe.
"""
import sqlite3
from datetime import datetime

import pytest

from test_stripe_billing_launch import OWNER_A, _cookie, env  # noqa: F401 -- fixture

CID = "cust-A"
pytestmark = pytest.mark.real_activation


def _exec(env, sql, args=()):
    conn = sqlite3.connect(env["path"])
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _now():
    return datetime.now().isoformat()


def _paid_appliance(env, status="paid"):
    _exec(env, "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,status,"
               "created_at,updated_at) VALUES('hw-1',?,'AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter','price_hw',1,124999,?,?,?)",
          (CID, status, _now(), _now()))


def _licence(env, status="active"):
    _exec(env, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
               "VALUES('lic-1',?,'vms_license',8,?,?,?)", (CID, status, _now(), _now()))


def _plan_row(env, status):
    _exec(env, "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,"
               "stripe_subscription_id,stripe_subscription_item_id,stripe_price_id,status,local_retention_days,created_at,"
               "updated_at) VALUES('v2-1',?,'basic_local',1,'cus_A','sub_A','si_A','price_basic',?,2,?,?)",
          (CID, status, _now(), _now()))


@pytest.fixture()
def plan_prices(monkeypatch):
    import main
    import per_camera_billing as billing
    for plan_key in billing.PLANS:
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], f"price_{plan_key}")
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", lambda *args: None)


def _stripe_posts(env):
    return [path for path, _ in env["posts"]]


# ------------------------------------------------------------------ what counts as activated

def test_a_new_account_is_not_activated(env):
    import per_camera_billing as billing
    assert billing.account_activated(CID) is False


@pytest.mark.parametrize("setup", ["licence", "paid_appliance", "plan_active", "plan_cancelled", "plan_payment_failed", "legacy_plan"])
def test_verified_purchases_activate_the_account(env, setup):
    import per_camera_billing as billing
    {"licence": lambda: _licence(env), "paid_appliance": lambda: _paid_appliance(env),
     "plan_active": lambda: _plan_row(env, "active"), "plan_cancelled": lambda: _plan_row(env, "cancelled"),
     "legacy_plan": lambda: _exec(env, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,"
                                       "created_at,updated_at) VALUES('leg-1',?,'camera_slots_local',8,'active',?,?)", (CID, _now(), _now())),
     "plan_payment_failed": lambda: (_plan_row(env, "suspended"),
                                     _exec(env, "UPDATE camera_plan_entitlements_v2 SET suspended_reason='payment_failed'"))}[setup]()
    assert billing.account_activated(CID) is True


@pytest.mark.parametrize("setup", ["saved_selection", "refunded_appliance", "suspended_licence", "refunded_plan"])
def test_incomplete_or_reversed_purchases_do_not_activate(env, setup):
    import per_camera_billing as billing
    if setup == "saved_selection":
        billing.save_build_selection(CID, {"plan": "basic_local", "cameras": 1})
    elif setup == "refunded_appliance":
        _paid_appliance(env, status="refunded")
    elif setup == "refunded_plan":
        _plan_row(env, "suspended")
        _exec(env, "UPDATE camera_plan_entitlements_v2 SET suspended_reason='refunded' WHERE id='v2-1'")
    else:
        _licence(env, status="suspended")
    assert billing.account_activated(CID) is False


# ------------------------------------------------------------------ server-side lock (no Stripe call)

@pytest.mark.parametrize("path,body", [
    ("/api/v2/customer/subscription/checkout", {"plan_key": "basic_local", "camera_quantity": 1}),
    ("/api/customer/analytics/checkout", {"addon_key": "ai_essentials", "quantity": 1}),
    ("/api/customer/vms-license/checkout", {"capacity": 8}),
    ("/api/payments/hardware-checkout", {"sku": "AIC-RELAY-NUMATO-3CH", "quantity": 1}),
])
def test_every_my_subscription_purchase_is_refused_before_activation(env, plan_prices, path, body):
    import per_camera_billing as billing
    response = env["client"].post(path, json=body, cookies=_cookie(*OWNER_A))
    detail = response.json()["detail"]
    assert response.status_code == 409 and detail["code"] == "activate_first" and detail["message"] == billing.ACTIVATE_FIRST
    assert detail["activate_url"] == "https://anyaicam.com/build-your-system.html?source=installed" == billing.ACTIVATE_URL
    assert "/v1/checkout/sessions" not in _stripe_posts(env)


def test_after_activation_the_plan_checkout_is_allowed_again(env, plan_prices):
    _plan_row(env, "cancelled")  # an earlier plan: resubscribes here, never back through Build Your System
    response = env["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "basic_local", "camera_quantity": 1},
                                  cookies=_cookie(*OWNER_A))
    assert response.status_code == 200, response.text
    assert "/v1/checkout/sessions" in _stripe_posts(env)


# ------------------------------------------------------------------ My subscription page

def _portal(env):
    response = env["client"].get("/subscription-portal", cookies=_cookie(*OWNER_A))
    assert response.status_code == 200
    return response.text


def test_portal_before_activation_shows_disabled_controls_and_the_activate_link(env):
    import per_camera_billing as billing
    page = _portal(env)
    assert 'id="v2-activate-first"' in page and billing.ACTIVATE_URL in page and "Activate Your System" in page
    assert 'id="v2-checkout-button" data-locked="1" disabled' in page  # the real control, disabled
    assert 'id="addons-after-activation"' in page
    assert page.index('id="v2-activate-first"') < page.index('id="v2-plan-summary"')  # the way forward comes first
    assert "Not activated yet. Choose Activate Your System" in page
    assert 'addon-buy-button" data-addon-key' not in page  # every Add button carries disabled


def test_portal_after_activation_keeps_the_normal_picker(env):
    _paid_appliance(env)  # hardware-first: the plan follows delivery; the optional early start stays available
    page = _portal(env)
    assert 'id="v2-activate-first"' not in page and 'id="addons-after-activation"' not in page


# ------------------------------------------------------------------ Build Your System order: licence never twice

def test_a_selection_without_hardware_is_the_own_pc_order_with_its_licence(env):
    import per_camera_billing as billing
    import build_orders
    billing.save_build_selection(CID, {"plan": "basic_local", "cameras": 1})
    progress = billing.build_progress(CID)
    assert progress.get("own_pc") and build_orders.order_kind(progress) == build_orders.OWN_PC_ORDER
    quote = build_orders.order_quote(progress)
    assert [line["cents"] for line in quote["one_time"]] == [2499] and quote["due_today_cents"] == 999 + 2499


def test_an_account_that_owns_the_licence_is_not_charged_it_again(env):
    import per_camera_billing as billing
    import build_orders
    _licence(env)
    billing.save_build_selection(CID, {"plan": "basic_local", "cameras": 1, "own_pc": 1})
    progress = billing.build_progress(CID)
    assert not progress.get("own_pc") and build_orders.order_quote(progress)["one_time"] == []
    page = env["client"].get("/order-summary", cookies=_cookie(*OWNER_A)).text
    assert 'id="order-license-owned"' in page and "Already on your account ($0.00)" in page


def test_order_summary_wording_for_own_pc(env):
    import per_camera_billing as billing
    billing.save_build_selection(CID, {"plan": "ai_local", "cameras": 5, "own_pc": 1})
    page = env["client"].get("/order-summary", cookies=_cookie(*OWNER_A)).text
    assert "A one-time AnyAiCam VMS software license is required when using your own computer." in page


# ------------------------------------------------------------------ after payment: return to the installed VMS

def _paid_own_pc(env, monkeypatch, linked):
    import per_camera_billing as billing
    billing.save_build_selection(CID, {"plan": "basic_local", "cameras": 1, "own_pc": 1})
    monkeypatch.setattr(billing, "order_paid", lambda customer_id: True)
    monkeypatch.setattr(billing, "_setup_needed", lambda customer_id: True)
    if linked:
        _exec(env, "INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,'Home',?)", (CID, _now()))
        _exec(env, "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('app-1',?,'site-1','AIC-TEST1',?)",
              (CID, _now()))


def test_order_complete_sends_an_installed_and_linked_pc_back_to_its_vms(env, monkeypatch):
    _paid_own_pc(env, monkeypatch, linked=True)
    page = env["client"].get("/order-complete", cookies=_cookie(*OWNER_A)).text
    assert 'id="order-return-to-vms"' in page and "nothing to download" in page
    assert 'id="order-download"' not in page and "/api/customer/downloads/vms-installer" not in page


def test_order_complete_still_offers_the_download_when_no_pc_is_linked(env, monkeypatch):
    _paid_own_pc(env, monkeypatch, linked=False)
    page = env["client"].get("/order-complete", cookies=_cookie(*OWNER_A)).text
    assert 'id="order-return-to-vms"' not in page and 'id="order-ready"' in page


def test_order_email_for_an_installed_pc_never_mentions_a_download():
    import order_funnel
    _, _, text, _ = order_funnel._order_email("Ann", {"own_pc": 1}, None, "AnyAiCam Basic Local subscription for 1 camera",
                                              installed=True)
    assert "nothing to download" in text and "download AnyAiCam" not in text


# ------------------------------------------------------------------ wording

def test_appliance_licence_line_uses_the_owner_wording():
    import order_funnel
    import hardware_checkout
    for module in (order_funnel, hardware_checkout):
        source = open(module.__file__, encoding="utf-8").read()
        assert "FREE, included with your PC ($0.00)" in source and "Included with your appliance</span>" not in source


def test_add_camera_refusal_before_activation_points_to_activation(env):
    import customer_entitlements
    assert customer_entitlements.capacity_refusal(CID).startswith("Activate your system to add cameras")


# ------------------------------------------------------------------ failed, cancelled, expired payments never activate

def _own_pc_session(status, payment_status="unpaid"):
    from test_stripe_billing_launch import _checkout  # noqa: F401 -- same event shape as the launch suite
    return {"id": f"evt_{status}_{payment_status}", "type": status, "created": 2_000, "data": {"object": {
        "id": f"cs_{status}", "object": "checkout.session", "mode": "subscription", "payment_status": payment_status,
        "status": "expired" if status.endswith("expired") else "complete", "customer": "cus_A", "subscription": None,
        "customer_details": {"email": "a@example.test"},
        "metadata": {"anyaicam_customer_id": CID, "anyaicam_build_order": "own_pc", "anyaicam_plan_key": "basic_local",
                     "anyaicam_camera_quantity": "1", "anyaicam_billing_version": "2", "anyaicam_stripe_price_id": "price_basic_local",
                     "anyaicam_vms_license_price_id": "price_lic_8", "anyaicam_vms_license_capacity": "8"}}}}


def _nothing_granted(env):
    import per_camera_billing as billing
    conn = sqlite3.connect(env["path"])
    licences = conn.execute("SELECT COUNT(*) FROM customer_entitlements WHERE customer_id=?", (CID,)).fetchone()[0]
    plans = conn.execute("SELECT COUNT(*) FROM camera_plan_entitlements_v2 WHERE customer_id=?", (CID,)).fetchone()[0]
    paid = conn.execute("SELECT COUNT(*) FROM hardware_orders WHERE customer_id=? AND status='paid'", (CID,)).fetchone()[0]
    conn.close()
    return licences == 0 and plans == 0 and paid == 0 and billing.account_activated(CID) is False


@pytest.mark.parametrize("event_type,payment_status", [
    ("checkout.session.completed", "unpaid"),            # card declined / bank debit not settled
    ("checkout.session.async_payment_failed", "unpaid"),  # delayed payment failed
    ("checkout.session.expired", "unpaid"),               # abandoned or cancelled checkout
])
def test_an_unpaid_failed_or_expired_own_pc_checkout_never_activates(env, plan_prices, monkeypatch, event_type, payment_status):
    from test_stripe_billing_launch import _deliver
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", "price_lic_8")
    response = _deliver(env, _own_pc_session(event_type, payment_status))
    assert response.status_code == 200, response.text
    assert _nothing_granted(env)


def test_an_incomplete_first_subscription_payment_never_activates(env, plan_prices):
    from test_stripe_billing_launch import _deliver, _sub_event
    subscription = {"id": "sub_inc", "object": "subscription", "customer": "cus_A", "status": "incomplete",
                    "current_period_end": 1_893_456_000, "cancel_at_period_end": False, "latest_invoice": "in_inc",
                    "items": {"data": [{"id": "si_inc", "price": {"id": "price_basic_local"}, "quantity": 1}]},
                    "metadata": {"anyaicam_customer_id": CID, "anyaicam_billing_version": "2", "anyaicam_plan_key": "basic_local"}}
    env["stripe"]["sub_inc"] = subscription
    assert _deliver(env, _sub_event("evt_inc", "created", subscription, 2_100)).status_code == 200
    assert _nothing_granted(env)


def test_an_unpaid_appliance_checkout_never_activates(env):
    from test_stripe_billing_launch import _deliver
    event = {"id": "evt_hw_unpaid", "type": "checkout.session.completed", "created": 2_200, "data": {"object": {
        "id": "cs_hw_unpaid", "object": "checkout.session", "mode": "payment", "payment_status": "unpaid",
        "customer": "cus_A", "payment_intent": "pi_hw_unpaid",
        "metadata": {"anyaicam_customer_id": CID, "anyaicam_build_order": "appliance", "anyaicam_plan_key": "hybrid",
                     "anyaicam_camera_quantity": "4", "anyaicam_hardware_items": "AIC-APPLIANCE-RYZEN-STARTER:1"}}}}
    assert _deliver(env, event).status_code == 200
    assert _nothing_granted(env)


def _old_flow_appliance(env, order_status):
    """A Build Your System appliance order from before hardware-first: the order plus its deferred storage plan."""
    _exec(env, "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,status,"
               "stripe_checkout_session_id,created_at,updated_at) VALUES('hw-old',?,'AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter',"
               "'price_hw',1,124999,?,'cs_old',?,?)", (CID, order_status, _now(), _now()))
    _exec(env, "INSERT INTO deferred_storage_plans(customer_id,plan_key,camera_quantity,checkout_session_id,state,created_at,updated_at) "
               "VALUES(?,'hybrid',4,'cs_old','pending_activation',?,?)", (CID, _now(), _now()))


@pytest.mark.parametrize("order_status,activated", [("paid", True), ("refunded", False), ("disputed", False)])
def test_an_old_flow_storage_plan_counts_only_while_its_appliance_is_paid(env, order_status, activated):
    """Green 2026-10-16: a refund or dispute leaves deferred_storage_plans in place; it no longer activates."""
    import per_camera_billing as billing
    _old_flow_appliance(env, order_status)
    assert billing.account_activated(CID) is activated
