"""My subscription under billing v2 (2026-10-05): what each plan includes,
optional analytics sold without overlap, Face Access kept separate, nothing a
customer holds removed, legacy plans not migrated, server-side eligibility.

Plan contents come from per_camera_billing.PLANS and match feature gating
(analytics_entitlements.account_wide_feature_active). People Counting, LPR and
PPE are granted only by the analytics packages, so they stay optional -- but a
package is never offered or sold for analytics the account already has.
"""
import re

import pytest

from database_backend import override_target
from test_per_camera_billing_v2 import db_path, initialized_db, seed_customer  # noqa: F401 -- fixtures

PACKAGE_ENV = {
    "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS": "price_t_ai_essentials",
    "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_PROFESSIONAL": "price_t_ai_professional",
    "ANYAICAM_STRIPE_PRICE_ANALYTICS_VEHICLE_INTELLIGENCE": "price_t_vehicle",
    "ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS": "price_t_advanced",
    "ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN": "price_t_talk_down",
}
PLAN_PRICES = {"basic_local": "price_t_basic", "ai_local": "price_t_ai", "hybrid": "price_t_hybrid"}
OWNER = {"customer_id": "cust-v2", "role": "customer_owner"}


@pytest.fixture()
def prices(monkeypatch):
    import per_camera_billing as billing
    for env, value in PACKAGE_ENV.items():
        monkeypatch.setenv(env, value)
    for plan_key, value in PLAN_PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], value)


def _plan(db_path, monkeypatch, plan_key, quantity=4):
    import per_camera_billing as billing
    import stripe_state
    seed_customer(db_path)
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _s: None)
    with override_target(sqlite_path=str(db_path)):
        billing._upsert_current({"id": "sub_offer", "customer": "cus_offer", "status": "active", "current_period_end": 1800000000,
                                 "metadata": {"anyaicam_customer_id": "cust-v2"},
                                 "items": {"data": [{"id": "si", "quantity": quantity, "price": {"id": PLAN_PRICES[plan_key]}}]}})


def _page(db_path, identity=OWNER):
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        return billing.customer_portal_page(identity, billing.entitlement_for_customer(identity["customer_id"]))


def _hold(db_path, addon_key, customer_id="cust-v2"):
    import analytics_entitlements as analytics
    with override_target(sqlite_path=str(db_path)):
        analytics.upsert_addon_subscription(customer_id=customer_id, addon_key=addon_key, status="active", stripe_subscription_id=f"sub_{addon_key}")
        for grant in analytics._grants_for(addon_key):
            analytics.upsert_analytics_subscription(customer_id=customer_id, analytic_key=grant, status="active",
                                                    stripe_subscription_id=f"sub_{addon_key}")


def _included(html):
    section = re.search(r'id="v2-included">(.*?)</section>', html, re.S).group(1)
    return re.findall(r'data-included="([^"]+)"', section)


def _addon_rows(html):
    section = re.search(r'id="v2-addons">(.*?)</section>', html, re.S).group(1)
    return {key: body for key, body in re.findall(r'data-addon-row="([a-z_]+)">(.*?)</div>', section, re.S)}


def _offer(db_path, addon_key, plan_key=None, customer_id="cust-v2"):
    import subscription_offers
    with override_target(sqlite_path=str(db_path)):
        return subscription_offers.addon_offer(customer_id, addon_key, plan_key)


# ------------------------------------------------------------ what each plan includes

def test_basic_local_shows_its_own_inclusions_and_no_ai(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "basic_local")
    included = _included(_page(db_path))
    for label in ("Local VMS", "Local recording", "Local playback", "Basic motion and events", "Camera management",
                  "Local recording retention: 2 days standard"):
        assert label in included
    for absent in ("Person and vehicle detection", "Smart Motion", "Talk Down / two-way audio and AAC Voice Call on supported cameras"):
        assert absent not in included
    assert not any(label.startswith("Cloud EVENT") for label in included)
    html = _page(db_path)
    assert "Available with Hybrid" in html


def test_ai_local_shows_its_core_ai_features_as_included(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "ai_local")
    included = _included(_page(db_path))
    for label in ("Person and vehicle detection", "Smart Motion", "AI event search and filters", "Intelligent notifications",
                  "Core AACO video and event retrieval", "Talk Down / two-way audio and AAC Voice Call on supported cameras",
                  "Local recording retention: 7 days standard"):
        assert label in included
    assert not any(label.startswith("Cloud EVENT") for label in included)


def test_hybrid_shows_ai_features_plus_cloud_event_retention(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "hybrid")
    included = _included(_page(db_path))
    for label in ("Person and vehicle detection", "Smart Motion", "Talk Down / two-way audio and AAC Voice Call on supported cameras",
                  "Hybrid cloud services", "Remote cloud services",
                  "Cloud EVENT retention: 14 days (event clips, not continuous recording)", "Local recording retention: 7 days standard"):
        assert label in included


@pytest.mark.parametrize("plan_key,expected", [("basic_local", "$9.99 per camera/month · $39.96"), ("ai_local", "$14.99 per camera/month · $59.96"),
                                               ("hybrid", "$24.99 per camera/month · $99.96")])
def test_current_plan_states_cameras_and_monthly_cost(db_path, monkeypatch, prices, plan_key, expected):
    _plan(db_path, monkeypatch, plan_key, quantity=4)
    html = _page(db_path)
    assert "4 licensed cameras" in html and expected in html


def test_the_inclusion_lists_match_feature_gating(db_path, monkeypatch, prices):
    """What the page says is included is what the gate switches on."""
    import analytics_entitlements as analytics
    from partner_db import connection
    for plan_key, talk_down in (("basic_local", False), ("ai_local", True), ("hybrid", True)):
        _plan(db_path, monkeypatch, plan_key)
        with override_target(sqlite_path=str(db_path)), connection() as db:
            assert analytics.account_wide_feature_active(db, "cust-v2", "talk_down") is talk_down
            assert analytics.account_wide_feature_active(db, "cust-v2", "voice_call") is talk_down
            for premium in ("people_counting", "lpr", "ppe"):
                assert analytics.account_wide_feature_active(db, "cust-v2", premium) is False


# ------------------------------------------------------------ optional analytics, no overlap

def test_ai_local_and_hybrid_never_show_talk_down_as_an_add(db_path, monkeypatch, prices):
    for plan_key in ("ai_local", "hybrid"):
        _plan(db_path, monkeypatch, plan_key)
        html = _page(db_path)
        assert 'data-addon-key="talk_down"' not in html and 'data-addon-row="talk_down"' not in html
        assert _offer(db_path, "talk_down", plan_key)["state"] == "included"


def test_basic_local_can_still_add_talk_down(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "basic_local")
    rows = _addon_rows(_page(db_path))
    assert 'data-addon-key="talk_down"' in rows["talk_down"] and "$4.99/mo per site" in rows["talk_down"]


def test_optional_analytics_are_one_section_with_what_each_package_adds(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "ai_local")
    html = _page(db_path)
    rows = _addon_rows(html)
    assert set(rows) == {"ai_essentials", "ai_professional", "vehicle_intelligence", "advanced_analytics"}
    assert "People Counting<br>" in rows["ai_essentials"] and "$7.99/mo" in rows["ai_essentials"]
    assert "Adds: License Plate Recognition (LPR)" in rows["vehicle_intelligence"] and "$14.99/mo" in rows["vehicle_intelligence"]
    assert "Adds: People Counting (advanced rules and reports), PPE detection" in rows["ai_professional"]
    assert "$24.99/mo" in rows["advanced_analytics"]
    for old in ("AI Essentials", "AI Professional", "Vehicle Intelligence", "Premium Add-ons", "Cloud Overflow"):
        assert old not in html
    assert 'id="optional-analytics-note"' in html


def test_a_package_already_covered_by_another_is_neither_offered_nor_sold(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "hybrid")
    _hold(db_path, "advanced_analytics")
    rows = _addon_rows(_page(db_path))
    assert '<span class="pill">Active</span>' in rows["advanced_analytics"]
    for covered in ("ai_essentials", "ai_professional", "vehicle_intelligence"):
        assert "data-addon-key" not in rows[covered] and "Already included in your Complete analytics" in rows[covered]
        assert _offer(db_path, covered, "hybrid")["state"] == "covered"


def test_a_package_overlapping_what_the_account_has_is_not_sold_but_a_disjoint_one_is(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "ai_local")
    _hold(db_path, "vehicle_intelligence")  # LPR
    rows = _addon_rows(_page(db_path))
    # Complete would charge for LPR again: no Add, says why.
    assert "data-addon-key" not in rows["advanced_analytics"]
    assert "Includes License Plate Recognition (LPR), which you already have." in rows["advanced_analytics"]
    assert _offer(db_path, "advanced_analytics", "ai_local")["state"] == "overlaps"
    # Packages that add only analytics the account lacks are still offered.
    assert 'data-addon-key="ai_essentials"' in rows["ai_essentials"] and 'data-addon-key="ai_professional"' in rows["ai_professional"]


def test_the_api_refuses_an_overlapping_package(db_path, monkeypatch, owner_client):
    client, calls = owner_client
    _plan(db_path, monkeypatch, "ai_local")
    _hold(db_path, "ai_essentials")  # People Counting
    response = _buy(client, "ai_professional")  # People Counting + PPE
    assert response.status_code == 409 and "which you already have" in response.json()["detail"]
    assert not [c for c in calls if c[0] == "/v1/checkout/sessions"]


def test_face_access_stays_a_separate_premium_product(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "hybrid")
    html = _page(db_path)
    assert "Premium: Face Access" in html or "Face Access" in html
    assert "facial_recognition" not in "".join(_included(html)).lower()
    import subscription_offers
    assert "facial_recognition" not in subscription_offers.plan_included_grants("hybrid")


# ------------------------------------------------------------ existing entitlements preserved

def test_an_earlier_talk_down_add_on_on_ai_local_stays_active_and_is_explained(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "ai_local")
    _hold(db_path, "talk_down")
    rows = _addon_rows(_page(db_path))
    assert "Active (earlier add-on). Talk Down is now included with your plan." in rows["talk_down"]
    import analytics_entitlements as analytics
    with override_target(sqlite_path=str(db_path)):
        assert "talk_down" in analytics.active_addon_keys("cust-v2")  # nothing cancelled


def test_held_packages_are_never_removed_by_the_reorganised_page(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "basic_local")
    for key in ("ai_essentials", "vehicle_intelligence"):
        _hold(db_path, key)
    rows = _addon_rows(_page(db_path))
    assert '<span class="pill">Active</span>' in rows["ai_essentials"] and '<span class="pill">Active</span>' in rows["vehicle_intelligence"]
    import analytics_entitlements as analytics
    with override_target(sqlite_path=str(db_path)):
        assert {"people_counting", "lpr"} <= set(analytics.get_active_analytics_for_customer("cust-v2"))


def test_a_legacy_plan_holder_keeps_the_legacy_page_and_plan(db_path, monkeypatch, prices):
    import main
    seed_customer(db_path)
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement, get_entitlements_for_customer
        upsert_entitlement(customer_id="cust-v2", product="camera_slots_local", camera_slot_quantity=8)
        _hold(db_path, "advanced_analytics")
        html = main._customer_subscription_portal_page(OWNER)
        assert "Local vs Hybrid" in html  # still the grandfathered page, not migrated
        assert [e["product"] for e in get_entitlements_for_customer("cust-v2") if e["status"] == "active"].count("camera_slots_local") == 1
    assert "Complete analytics: People Counting + LPR + PPE" in html
    assert "AI Essentials" not in html and "Cloud Overflow" not in html
    assert 'data-addon-key="ai_essentials"' not in html  # covered by the Complete package they hold
    assert "Already included in your Complete analytics" in html
    assert 'data-addon-key="talk_down"' in html  # a legacy plan does not include Talk Down


# ------------------------------------------------------------ plan choice and changes

def test_without_a_plan_the_three_plans_are_compared_from_the_catalog(db_path, monkeypatch, prices):
    seed_customer(db_path)
    html = _page(db_path)
    table = re.search(r'id="v2-plan-compare".*?</table>', html, re.S).group(0)
    assert "$9.99/camera/mo" in table and "$14.99/camera/mo" in table and "$24.99/camera/mo" in table
    assert "Cloud EVENT retention" in table and "14 days" in table
    assert 'id="v2-checkout-button"' in html


def test_the_change_form_keeps_the_licensed_camera_count(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "ai_local", quantity=6)
    html = _page(db_path)
    assert 'id="v2-change-quantity" type="number" min="1" max="64" value="6"' in html
    for key in ("basic_local", "ai_local", "hybrid"):
        assert f'<option value="{key}"' in html


# ------------------------------------------------------------ server-side purchase eligibility

@pytest.fixture()
def owner_client(db_path, monkeypatch, prices):
    import main
    import partner_portal
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(main, "stripe_api_post", lambda path, fields, idempotency_key=None: calls.append((path, dict(fields)))
                        or ({"id": "cus_offer"} if path == "/v1/customers" else {"id": "cs_t_1", "url": "https://checkout.stripe.test/1"}))
    with override_target(sqlite_path=str(db_path)):
        with TestClient(main.app, follow_redirects=False) as client:
            client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token("owner@example.test", "customer_owner", None, "cust-v2", None))
            yield client, calls


def _buy(client, key, quantity=1):
    return client.post("/api/customer/analytics/checkout", json={"addon_key": key, "quantity": quantity})


def test_the_api_refuses_talk_down_on_ai_local_and_covered_packages(db_path, monkeypatch, owner_client):
    client, calls = owner_client
    _plan(db_path, monkeypatch, "ai_local")
    assert _buy(client, "talk_down").status_code == 409
    _hold(db_path, "advanced_analytics")
    response = _buy(client, "ai_essentials")
    assert response.status_code == 409 and "Already included" in response.json()["detail"]
    assert not [c for c in calls if c[0] == "/v1/checkout/sessions"]


def test_the_api_still_sells_a_genuine_add_on_at_the_server_price(db_path, monkeypatch, owner_client):
    client, calls = owner_client
    _plan(db_path, monkeypatch, "ai_local")
    # A browser-sent price is ignored: the Price comes from the server catalog only.
    response = client.post("/api/customer/analytics/checkout",
                           json={"addon_key": "vehicle_intelligence", "quantity": 1, "price_id": "price_evil", "unit_amount": 1})
    assert response.status_code == 200, response.text
    sessions = [fields for path, fields in calls if path == "/v1/checkout/sessions"]
    assert sessions and sessions[-1]["line_items[0][price]"] == "price_t_vehicle"
    assert "price_evil" not in str(sessions) and "unit_amount" not in str(sessions)
