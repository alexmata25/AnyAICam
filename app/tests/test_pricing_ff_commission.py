"""Approved working pricing, Friends & Family and salesperson commission
(2026-09-30): pricing_catalog.py, friends_family.py, sales_commissions.py
and their wiring into entitlements, checkout and the Stripe webhook."""
import inspect
import sqlite3

import pytest
from fastapi.testclient import TestClient

import pricing_catalog as pc
from database_backend import override_target


# ================================================================ catalog

@pytest.mark.parametrize("plan_type,tier,dollars", [
    ("local", "1-8", "14.99"), ("local", "9-16", "24.99"), ("local", "17-32", "39.99"), ("local", "33-64", "69.99"),
    ("hybrid", "1-8", "24.99"), ("hybrid", "9-16", "39.99"), ("hybrid", "17-32", "69.99"), ("hybrid", "33-64", "99.99"),
])
def test_base_plan_prices(plan_type, tier, dollars):
    plan = pc.find_base_plan(plan_type, tier)
    assert plan["monthly_cents"] == pc._cents(dollars)
    assert plan["billing_type"] == "recurring"


def test_camera_limits_are_licensing_tiers_only():
    assert [p["camera_slot_maximum"] for p in pc.base_plans() if p["plan_type"] == "local"] == [8, 16, 32, 64]


def test_secure_edge_smart_motion_and_aaco_are_included_and_never_charged():
    assert set(pc.INCLUDED_FEATURE_KEYS) == {"secure_edge", "smart_motion", "aaco"}
    sold = {a["addon_key"] for a in pc.addons()}
    granted = {k for a in pc.addons() for k in a["grants"]}
    assert not (set(pc.INCLUDED_FEATURE_KEYS) & sold)
    assert not (set(pc.INCLUDED_FEATURE_KEYS) & granted)
    # A base plan alone is a complete, purchasable quote.
    assert pc.quote(plan_type="local", tier_label="1-8")["monthly_cents"] == 1499


@pytest.mark.parametrize("key,dollars", [
    ("ai_essentials", "7.99"), ("ai_professional", "14.99"), ("vehicle_intelligence", "14.99"),
    ("advanced_analytics", "24.99"), ("talk_down", "4.99"),
])
def test_addon_prices(key, dollars):
    assert pc.find_addon(key)["monthly_cents"] == pc._cents(dollars)


def test_package_contents():
    grants = {a["addon_key"]: set(a["grants"]) for a in pc.addons()}
    assert grants["ai_essentials"] == {"people_counting"}
    assert grants["ai_professional"] == {"people_counting", "ppe"}
    assert grants["vehicle_intelligence"] == {"lpr"}
    assert grants["advanced_analytics"] == {"people_counting", "lpr", "ppe"}


@pytest.mark.parametrize("plan_type,tier,total", [
    ("local", "1-8", 3998), ("local", "9-16", 4998), ("local", "17-32", 6498), ("local", "33-64", 9498),
    ("hybrid", "1-8", 4998), ("hybrid", "9-16", 6498), ("hybrid", "17-32", 9498), ("hybrid", "33-64", 12498),
])
def test_advanced_analytics_is_a_flat_add_on_never_per_camera(plan_type, tier, total):
    quote = pc.quote(plan_type=plan_type, tier_label=tier, addon_keys=["advanced_analytics"])
    assert quote["monthly_cents"] == total
    advanced = next(line for line in quote["lines"] if line["label"] == "Advanced Analytics")
    assert advanced["quantity"] == 1 and advanced["cents"] == 2499


def test_advanced_is_not_the_sum_of_the_individual_packages():
    parts = sum(pc.find_addon(k)["monthly_cents"] for k in ("ai_essentials", "ai_professional", "vehicle_intelligence"))
    assert pc.find_addon("advanced_analytics")["monthly_cents"] == 2499 != parts


def test_talk_down_is_per_site_and_includes_voice_call():
    talk_down = pc.find_addon("talk_down")
    assert talk_down["unit"] == "per_site"
    assert set(talk_down["grants"]) == {"talk_down", "voice_call"}
    assert not any("voice_call" in a["addon_key"] for a in pc.addons())  # no second charge
    assert pc.quote(plan_type="local", tier_label="1-8", talk_down_sites=1)["monthly_cents"] == 1998
    assert pc.quote(plan_type="hybrid", tier_label="1-8", talk_down_sites=1)["monthly_cents"] == 2998
    assert pc.quote(plan_type="local", tier_label="1-8", talk_down_sites=3)["monthly_cents"] == 1499 + 3 * 499


def test_face_access_is_separate_per_door_and_not_in_advanced():
    tiers = {t["size"]: t["monthly_cents_per_door"] for t in pc.face_access_tiers()}
    assert tiers == {"small": 3999, "medium": 4999, "large": 6999}
    assert "facial_recognition" not in pc.find_addon("advanced_analytics")["grants"]


def test_face_access_size_follows_enrolled_people():
    """Owner-approved 2026-09-30: Small up to 25, Medium 26-100, Large
    101-500; over 500 is Enterprise / custom pricing."""
    assert [pc.face_access_size_for(n) for n in (0, 25, 26, 100, 101, 500, 501)] == [
        "small", "small", "medium", "medium", "large", "large", "enterprise"]
    assert all(t["sellable"] for t in pc.face_access_tiers())


def test_cloud_overflow_has_no_invented_price_and_is_not_bundled():
    overflow = pc.find_addon("cloud_overflow")
    assert overflow["monthly_cents"] is None and not overflow["sellable"]
    assert all("cloud_overflow" not in a["grants"] for a in pc.addons() if a["addon_key"] != "cloud_overflow")


def test_friends_family_percentages_and_quote():
    # The one-time VMS license has no approved Friends & Family discount (0%).
    assert pc.FRIENDS_FAMILY_PERCENT_OFF == {"base": 50, "analytics": 25, "hardware": 0, "vms_license": 0, "face_access": 0}
    quote = pc.quote(plan_type="local", tier_label="1-8", addon_keys=["advanced_analytics"], friends_family_approved=True)
    base, advanced = quote["lines"]
    assert base["cents"] == 1499 - 750  # 50% of $14.99, rounded to the cent like Stripe
    assert advanced["cents"] == 2499 - 625  # 25% of $24.99
    assert quote["monthly_list_cents"] == 3998


def test_public_catalog_exposes_prices_but_no_stripe_ids_or_commissions():
    import json
    text = json.dumps(pc.public_catalog())
    assert "price_" not in text and "ANYAICAM_STRIPE" not in text and "commission" not in text.lower()
    assert "14.99" in text and "99.99" in text


def test_customer_entitlements_tiers_come_from_the_catalog():
    import customer_entitlements as ce
    assert [(t[0], t[1], t[4], t[5], t[7]) for t in ce.PLAN_TIERS] == [
        (p["plan_type"], p["tier_label"], p["camera_slot_maximum"], p["monthly_cents"] / 100, "recurring") for p in pc.base_plans()]


def test_no_duplicate_plan_prices_left_in_python_sources():
    """One source of truth: the old tier prices are gone from code."""
    import customer_entitlements, analytics_entitlements, main
    for module in (customer_entitlements, analytics_entitlements):
        source = inspect.getsource(module)
        for old in ("19.99", "29.99", "49.99", "89.99", "149.99"):
            assert old not in source, (module.__name__, old)


# ================================================================ database fixtures

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "pricing.db"
    with override_target(sqlite_path=str(path)):
        from partner_db import initialize_database
        initialize_database()
        yield path


def _seed(db_path, *, customer_id="cust-1", email="owner@example.test", salesperson=True, cameras=3):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','P','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                 (customer_id, "partner-1", "Real Customer", email, "active", "2026-01-01", "sales@example.test" if salesperson else None))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Home", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) "
                 "VALUES('sales-1','partner-1','sales@example.test','Sam Sales','salesperson','x',1,'2026-01-01')")
    for n in range(1, cameras + 1):
        conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,name,camera_number,created_at) VALUES(?,?,?,?,?,?)",
                     (f"cam-{n}", customer_id, "site-1", f"Camera {n}", n, "2026-01-01"))
    conn.commit()
    conn.close()


# ================================================================ entitlements

def _assign(db_path, camera_id, key):
    import customer_analytics_panel as cap
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)), connection() as db:
        cap.assign_entitlement(db, camera_id, key, now="2026-09-30T00:00:00")


def test_smart_motion_is_on_every_camera_with_a_paid_plan_and_not_without(db_path):
    import customer_analytics_panel as cap
    _seed(db_path)
    with pytest.raises(cap.LicenseLimitExceeded):
        _assign(db_path, "cam-1", "smart_motion")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    for camera in ("cam-1", "cam-2", "cam-3"):
        _assign(db_path, camera, "smart_motion")


def _stripe_maps(monkeypatch, **prices):
    import analytics_entitlements as ae
    import customer_entitlements as ce
    for env_var, value in prices.items():
        monkeypatch.setenv(env_var, value)
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", ce._load_price_tier_map())


def _checkout_event(event_id, price_id, customer_id="cust-1", stripe_customer="cus_1", quantity=None):
    metadata = {"anyaicam_stripe_price_id": price_id, "anyaicam_customer_id": customer_id}
    if quantity:
        metadata["anyaicam_quantity"] = str(quantity)
    return {"id": event_id, "type": "checkout.session.completed", "data": {"object": {
        "id": f"cs_{event_id}", "customer": stripe_customer, "mode": "subscription", "payment_status": "paid",
        "customer_details": {"email": "owner@example.test"}, "metadata": metadata}}}


def _cancel_event(event_id, price_id, stripe_customer="cus_1"):
    return {"id": event_id, "type": "customer.subscription.deleted", "data": {"object": {
        "id": f"sub_{price_id}", "customer": stripe_customer, "status": "canceled",
        "items": {"data": [{"price": {"id": price_id}, "quantity": 1}]}, "metadata": {"anyaicam_customer_id": "cust-1"}}}}


def test_a_flat_package_covers_every_camera_not_one(db_path, monkeypatch):
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS="price_adv")
    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_adv", "price_adv"))
    for camera in ("cam-1", "cam-2", "cam-3"):
        _assign(db_path, camera, "lpr")
        _assign(db_path, camera, "people_counting")


def test_overlapping_packages_keep_a_shared_feature_when_one_is_cancelled(db_path, monkeypatch):
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS="price_ess",
                 ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_PROFESSIONAL="price_pro")
    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_ess", "price_ess"))
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_pro", "price_pro"))
        ae.sync_analytics_from_stripe_event(_cancel_event("evt_ess_cancel", "price_ess"))
        assert ae.get_active_analytics_for_customer("cust-1") == ["people_counting", "ppe"]
        assert ae.active_addon_keys("cust-1") == ["ai_professional"]
        ae.sync_analytics_from_stripe_event(_cancel_event("evt_pro_cancel", "price_pro"))
        assert ae.get_active_analytics_for_customer("cust-1") == []


def test_talk_down_purchase_grants_voice_call_automatically(db_path, monkeypatch):
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN="price_td")
    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        ae.sync_analytics_from_stripe_event(_checkout_event("evt_td", "price_td", quantity=2))
        assert ae.get_active_analytics_for_customer("cust-1") == ["talk_down", "voice_call"]
        from partner_db import row
        assert row("SELECT quantity FROM addon_subscriptions WHERE customer_id='cust-1' AND addon_key='talk_down'")["quantity"] == 2


def test_old_advanced_purchases_keep_everything_they_had(db_path, monkeypatch):
    """Existing Advanced customers keep smart_motion etc.: nothing a
    customer already has is removed by the new package mapping."""
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        for key in ("smart_motion", "people_counting", "lpr", "ppe"):
            ae.upsert_analytics_subscription(customer_id="cust-1", analytic_key=key, status="active")
        assert ae.get_active_analytics_for_customer("cust-1") == ["lpr", "people_counting", "ppe", "smart_motion"]


# ================================================================ portal + Friends & Family

@pytest.fixture()
def portal(db_path, tmp_path, monkeypatch, fake_stripe_prices):
    import main
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", "price_local_8")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_adv")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "price_td")
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_BASE", "coupon_ff_base")
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_ANALYTICS", "coupon_ff_analytics")
    sent = []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text})

    import purchase_notifications
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    captured = []

    def _fake_stripe(path, fields):
        captured.append(dict(fields))
        return {"id": f"cs_{len(captured)}", "url": f"https://checkout.stripe.test/{len(captured)}"}

    monkeypatch.setattr(main, "stripe_api_post", _fake_stripe)
    with override_target(sqlite_path=str(db_path)):
        with TestClient(main.app, follow_redirects=False) as client:
            yield client, captured, sent


def _cookie(email, role, customer_id=None):
    import partner_portal
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, customer_id, None)}


OWNER = ("owner@example.test", "customer_owner", "cust-1")


def _make_global_admin(db_path, email="admin@example.test"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partner_users(id,email,name,role,password_hash,approved,created_at) "
                 "VALUES('admin-1',?,'Admin','administrator','x',1,'2026-01-01')", (email,))
    conn.execute("INSERT OR IGNORE INTO identity_grants(id,user_id,role,scope_type,granted_at) "
                 "VALUES('grant-1','admin-1','administrator','global','2026-01-01')")
    conn.commit()
    conn.close()
    return _cookie(email, "administrator")


def _checkout_plan(client):
    return client.post("/api/customer/camera-slots/checkout", json={"plan_type": "local", "tier_label": "1-8"}, cookies=_cookie(*OWNER))


def test_friends_family_request_pending_blocks_checkout_and_emails_the_admin(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    assert client.get("/api/customer/friends-family", cookies=_cookie(*OWNER)).json() == {"status": "none"}
    first = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()
    assert first["status"] == "pending"
    again = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()
    assert again["request_id"] == first["request_id"]  # never duplicated
    assert len(sent) == 1
    assert "/admin/friends-family/" + first["request_id"] in sent[0]["text"]
    assert "Real Customer" in sent[0]["subject"]
    held = _checkout_plan(client)
    assert held.status_code == 409 and "administrator approval" in held.json()["detail"]
    assert captured == []  # no Stripe session while the price is unsettled


def test_opening_the_review_link_never_approves_and_needs_an_administrator(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    request_id = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]
    assert client.get(f"/admin/friends-family/{request_id}", cookies=_cookie(*OWNER)).status_code == 403
    assert client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "approve"},
                       cookies=_cookie(*OWNER)).status_code == 403
    # A partner-scoped administrator is not enough.
    assert client.get(f"/admin/friends-family/{request_id}", cookies=_cookie("company-admin@example.test", "administrator")).status_code == 403
    admin = _make_global_admin(db_path)
    page = client.get(f"/admin/friends-family/{request_id}", cookies=admin)
    assert page.status_code == 200 and "Approve" in page.text and "Decline" in page.text
    assert client.get("/api/customer/friends-family", cookies=_cookie(*OWNER)).json()["status"] == "pending"


def test_approval_applies_the_coupons_before_checkout_without_promo_stacking(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    request_id = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]
    decided = client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "approve"}, cookies=_make_global_admin(db_path))
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    assert client.get("/api/customer/friends-family", cookies=_cookie(*OWNER)).json()["status"] == "approved"
    assert _checkout_plan(client).status_code == 200
    plan = captured[-1]
    assert plan["discounts[0][coupon]"] == "coupon_ff_base"
    assert "allow_promotion_codes" not in plan
    assert plan["metadata[anyaicam_friends_family]"] == "approved"
    analytics = client.post("/api/customer/analytics/checkout", json={"addon_key": "advanced_analytics"}, cookies=_cookie(*OWNER))
    assert analytics.status_code == 200
    assert captured[-1]["discounts[0][coupon]"] == "coupon_ff_analytics"
    assert "allow_promotion_codes" not in captured[-1]
    # A second decision is refused.
    again = client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "decline"}, cookies=_make_global_admin(db_path))
    assert again.status_code == 409


def test_approved_customer_is_never_charged_full_price_when_the_coupon_is_missing(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    request_id = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]
    client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "approve"}, cookies=_make_global_admin(db_path))
    monkeypatch.delenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_BASE")
    response = _checkout_plan(client)
    assert response.status_code == 503 and "COUPON_REQUIRED" in response.json()["detail"]
    assert captured == []


def test_decline_restores_normal_checkout_with_promotion_codes(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    request_id = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]
    client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "decline"}, cookies=_make_global_admin(db_path))
    assert client.get("/api/customer/friends-family", cookies=_cookie(*OWNER)).json()["status"] == "declined"
    assert _checkout_plan(client).status_code == 200
    assert captured[-1]["allow_promotion_codes"] == "true"
    assert "discounts[0][coupon]" not in captured[-1]


def test_talk_down_checkout_is_billed_per_site(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    response = client.post("/api/customer/analytics/checkout", json={"addon_key": "talk_down", "quantity": 3}, cookies=_cookie(*OWNER))
    assert response.status_code == 200
    assert captured[-1]["line_items[0][quantity]"] == "3"
    flat = client.post("/api/customer/analytics/checkout", json={"addon_key": "advanced_analytics", "quantity": 64}, cookies=_cookie(*OWNER))
    assert flat.status_code == 200
    assert captured[-1]["line_items[0][quantity]"] == "1"  # never multiplied by cameras


def test_unsellable_items_cannot_be_checked_out(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_CLOUD_OVERFLOW", "price_overflow")
    for key in ("cloud_overflow", "facial_recognition"):
        response = client.post("/api/customer/analytics/checkout", json={"addon_key": key}, cookies=_cookie(*OWNER))
        assert response.status_code == 400, key
    assert captured == []


def test_hardware_is_never_discounted():
    import main
    source = inspect.getsource(main.create_hardware_checkout)
    assert "friends_family" not in source and "discounts[" not in source
    assert '("allow_promotion_codes", "false")' in source
    assert pc.friends_family_percent("hardware") == 0


def test_catalog_api_is_public_and_customer_safe(portal):
    client, captured, sent = portal
    body = client.get("/api/pricing/catalog").json()
    assert {p["monthly"] for p in body["base_plans"]} >= {14.99, 24.99, 39.99, 69.99, 99.99}
    assert body["friends_family"]["requires_administrator_approval"] is True


# ================================================================ commissions

def _invoice_event(event_id, invoice_id, price_id, amount_cents, *, sub="sub_base", tax=0, customer_id="cust-1"):
    return {"id": event_id, "type": "invoice.paid", "data": {"object": {
        "id": invoice_id, "object": "invoice", "customer": "cus_1", "subscription": sub, "amount_paid": amount_cents,
        "tax": tax, "currency": "usd", "charge": f"ch_{invoice_id}", "payment_intent": f"pi_{invoice_id}",
        "subscription_details": {"metadata": {"anyaicam_customer_id": customer_id}},
        "lines": {"data": [{"price": {"id": price_id}}]}}}}


@pytest.fixture()
def ledger(db_path, monkeypatch):
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_LOCAL_1_8="price_local_8", ANYAICAM_STRIPE_PRICE_LOCAL_9_16="price_local_16",
                 ANYAICAM_STRIPE_PRICE_LOCAL_17_32="price_local_32", ANYAICAM_STRIPE_PRICE_LOCAL_33_64="price_local_64",
                 ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS="price_adv")
    with override_target(sqlite_path=str(db_path)):
        import sales_commissions
        yield sales_commissions


def _entries(sc, kind=None):
    return [e for e in sc.ledger_for() if kind is None or e["kind"] == kind]


@pytest.mark.parametrize("price_id,slots,cents", [
    ("price_local_8", 8, 4000), ("price_local_16", 16, 6000), ("price_local_32", 32, 9000), ("price_local_64", 64, 12500),
])
def test_activation_commission_by_tier_on_the_first_paid_month(ledger, price_id, slots, cents):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", price_id, 1499))
    activation = _entries(sc, "activation")
    assert len(activation) == 1
    assert activation[0]["amount_cents"] == cents and activation[0]["camera_slot_tier"] == slots
    assert activation[0]["salesperson_user_id"] == "sales-1" and activation[0]["plan_type"] == "local"
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_2", "in_2", price_id, 1499))
    assert len(_entries(sc, "activation")) == 1  # once per customer


def test_recurring_commission_is_20_percent_of_collected_revenue_for_12_paid_months_only(ledger):
    sc = ledger
    for month in range(1, 15):
        sc.sync_commissions_from_stripe_event(_invoice_event(f"evt_{month}", f"in_{month}", "price_local_8", 1499))
    recurring = _entries(sc, "recurring")
    assert len(recurring) == 12
    assert {e["amount_cents"] for e in recurring} == {300}  # 20% of $14.99 = $2.998
    assert sorted(e["paid_month_index"] for e in recurring) == list(range(1, 13))


def test_first_year_example_matches_the_approved_structure(ledger, db_path):
    """Local 8 + Ryzen 7 Starter, 12 paid months: $40 + $50 + ~$35.98."""
    sc = ledger
    for month in range(1, 13):
        sc.sync_commissions_from_stripe_event(_invoice_event(f"evt_{month}", f"in_{month}", "price_local_8", 1499))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_checkout_session_id,quantity,amount_cents,currency,status,fulfillment_status,created_at,updated_at) "
                 "VALUES('hw-1','cust-1','AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter','price_hw','cs_hw',1,124999,'usd','paid','paid','2026-01-01','2026-01-01')")
    conn.commit()
    conn.close()
    sc.sync_commissions_from_stripe_event({"id": "evt_hw", "type": "checkout.session.completed",
                                           "data": {"object": {"id": "cs_hw", "mode": "payment", "payment_status": "paid"}}})
    summary = sc.summarize(sc.ledger_for())
    assert summary["by_kind_cents"] == {"activation": 4000, "hardware": 5000, "recurring": 3600}
    # 12 x round(20% of 1499) = 12 x 300 = $36.00 (per-payment cent rounding of $35.976)
    assert summary["earned_cents"] == 12600


def test_failed_or_unpaid_invoices_earn_nothing(ledger):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_zero", "in_zero", "price_local_8", 0))
    failed = {"id": "evt_fail", "type": "invoice.payment_failed", "data": {"object": {"id": "in_f"}}}
    assert sc.sync_commissions_from_stripe_event(failed)["status"] == "ignored"
    assert _entries(sc) == []


def test_cancellation_stops_commission_after_the_last_paid_month(ledger):
    sc = ledger
    for month in range(1, 5):
        sc.sync_commissions_from_stripe_event(_invoice_event(f"evt_{month}", f"in_{month}", "price_local_8", 1499))
    # Cancelled: Stripe sends no further paid invoices, so nothing more is earned.
    assert len(_entries(sc, "recurring")) == 4


def test_webhook_retries_never_double_count(ledger):
    sc = ledger
    event = _invoice_event("evt_1", "in_1", "price_local_8", 1499)
    sc.sync_commissions_from_stripe_event(event)
    sc.sync_commissions_from_stripe_event(event)
    assert len(_entries(sc, "recurring")) == 1 and len(_entries(sc, "activation")) == 1


def test_tax_is_not_commissionable(ledger):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", "price_local_8", 1624, tax=125))
    assert _entries(sc, "recurring")[0]["basis_cents"] == 1499


def test_refunds_and_disputes_reverse_the_commission_they_generated(ledger):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", "price_local_8", 1499))
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_2", "in_2", "price_local_8", 1499))
    # Partial refunds are cumulative in Stripe: 50% then 100%.
    half = {"id": "evt_r1", "type": "charge.refunded", "data": {"object": {"object": "charge", "id": "ch_in_2", "payment_intent": "pi_in_2",
                                                                          "invoice": "in_2", "amount": 1499, "amount_refunded": 750}}}
    sc.sync_commissions_from_stripe_event(half)
    month2 = next(e for e in _entries(sc, "recurring") if e["stripe_invoice_id"] == "in_2")
    assert month2["status"] == "earned" and month2["amount_cents"] == 150
    full = {"id": "evt_r2", "type": "charge.refunded", "data": {"object": {"object": "charge", "id": "ch_in_2", "payment_intent": "pi_in_2",
                                                                          "invoice": "in_2", "amount": 1499, "amount_refunded": 1499}}}
    sc.sync_commissions_from_stripe_event(full)
    month2 = next(e for e in _entries(sc, "recurring") if e["stripe_invoice_id"] == "in_2")
    assert month2["status"] == "reversed"
    # A dispute on the first payment reverses its recurring AND activation commission.
    dispute = {"id": "evt_d", "type": "charge.dispute.created", "data": {"object": {"object": "dispute", "charge": "ch_in_1",
                                                                                 "payment_intent": "pi_in_1", "amount": 1499}}}
    sc.sync_commissions_from_stripe_event(dispute)
    assert all(e["status"] == "reversed" for e in _entries(sc) if e["stripe_invoice_id"] == "in_1")
    assert sc.summarize(sc.ledger_for())["earned_cents"] == 0


@pytest.mark.parametrize("sku,cents", [
    ("AIC-APPLIANCE-RYZEN-STARTER", 5000), ("AIC-APPLIANCE-RYZEN-ENTERPRISE", 7500),
    ("AIC-APPLIANCE-RYZEN-AAC-FACIAL", 10000), ("AIC-RELAY-NUMATO-3CH", 0),
])
def test_hardware_commission_by_appliance(ledger, db_path, sku, cents):
    sc = ledger
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_checkout_session_id,quantity,amount_cents,currency,status,fulfillment_status,created_at,updated_at) "
                 "VALUES('hw-1','cust-1',?,'Item','price_hw','cs_hw',1,1000,'usd','paid','paid','2026-01-01','2026-01-01')", (sku,))
    conn.commit()
    conn.close()
    sc.sync_commissions_from_stripe_event({"id": "evt_hw", "type": "checkout.session.completed",
                                           "data": {"object": {"id": "cs_hw", "mode": "payment", "payment_status": "paid"}}})
    assert sum(e["amount_cents"] for e in _entries(sc, "hardware")) == cents


def test_no_hardware_means_no_hardware_commission(ledger):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", "price_local_8", 1499))
    assert _entries(sc, "hardware") == []


def test_friends_family_sales_earn_zero_commission_but_keep_attribution(ledger, db_path):
    sc = ledger
    import friends_family
    request, _ = friends_family.create_request(customer_id="cust-1", email="owner@example.test")
    friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", "price_local_8", 750))
    entries = _entries(sc)
    assert {e["kind"] for e in entries} == {"activation", "recurring"}
    assert all(e["amount_cents"] == 0 and e["status"] == "not_eligible_friends_family" and e["salesperson_user_id"] == "sales-1"
               for e in entries)


def test_no_attribution_means_no_commission_rows(ledger, db_path):
    sc = ledger
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE customers SET created_by=NULL WHERE id='cust-1'")
    conn.commit()
    conn.close()
    sc.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", "price_local_8", 1499))
    assert _entries(sc) == []


def test_commission_is_a_webhook_step_not_a_browser_calculation():
    import main
    assert "sales_commissions" in [name for name, _ in main._stripe_webhook_steps()]
    assert "sales_commissions" in inspect.getsource(main._stripe_webhook_steps)


# ================================================================ Stripe authority

def test_success_page_visit_grants_nothing(portal, db_path):
    """Entitlements only ever come from verified webhooks: returning from
    Stripe to the success URL with a session id changes nothing."""
    client, captured, sent = portal
    _seed(db_path)
    client.get("/customer/setup?camera_plan_payment=success&session_id=cs_forged", cookies=_cookie(*OWNER))
    client.get("/subscription-portal?session_id=cs_forged", cookies=_cookie(*OWNER))
    from customer_entitlements import total_camera_slots
    from analytics_entitlements import get_active_analytics_for_customer
    assert total_camera_slots("cust-1") == 0
    assert get_active_analytics_for_customer("cust-1") == []


def test_unsigned_webhook_is_rejected(portal):
    client, captured, sent = portal
    response = client.post("/api/payments/stripe/webhook", content=b'{"id":"evt_x","type":"invoice.paid"}',
                           headers={"stripe-signature": "t=1,v1=forged"})
    assert response.status_code == 400


# ================================================================ visibility

def test_commission_records_are_scoped_to_the_salesperson(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_LOCAL_1_8="price_local_8")
    import sales_commissions
    sales_commissions.sync_commissions_from_stripe_event(_invoice_event("evt_1", "in_1", "price_local_8", 1499))
    mine = client.get("/api/sales/commissions", cookies=_cookie("sales@example.test", "salesperson"))
    assert mine.status_code == 200 and mine.json()["summary"]["earned_cents"] == 4000 + 300
    assert client.get("/api/sales/commissions", cookies=_cookie(*OWNER)).status_code == 403
    page = client.get("/partner-revenue", cookies=_cookie("sales@example.test", "salesperson"))
    assert "Earned commissions" in page.text and "Real Customer" in page.text and "cust-1" not in page.text


def test_friends_family_panel_is_offered_where_customers_buy(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    assert 'id="friends-family-panel"' in client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert 'id="friends-family-panel"' in client.get("/customer/setup", cookies=_cookie(*OWNER)).text


def test_my_subscription_shows_catalog_prices_and_included_features(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "Local 8 cameras &middot; 8 licensed camera slots &middot; $14.99/mo" in html
    assert "Secure Edge, Smart Motion, AACO" in html
    assert "$24.99/mo · Includes: People Counting, LPR, PPE" in html
    assert "<span>Talk Down (includes AAC Voice Call)<br><span class=\"health-detail\">$4.99/mo per site</span>" in html


# ================================================================ one-time VMS software license

@pytest.mark.parametrize("capacity,cents", [(8, 4999), (16, 7999), (32, 12999), (64, 19999)])
def test_vms_license_prices_are_one_time(capacity, cents):
    lic = pc.find_vms_license(capacity)
    assert lic["one_time_cents"] == cents and lic["billing_type"] == "one_time"


def test_vms_license_does_not_change_the_monthly_plans():
    assert [p["monthly_cents"] for p in pc.base_plans()] == [1499, 2499, 3999, 6999, 2499, 3999, 6999, 9999]


@pytest.mark.parametrize("plan_type,tier,capacity,cents", [
    ("local", "1-8", 8, 4999), ("local", "9-16", 16, 7999), ("hybrid", "17-32", 32, 12999), ("hybrid", "33-64", 64, 19999),
])
def test_diy_quote_adds_the_matching_license_and_appliance_quote_includes_it(plan_type, tier, capacity, cents):
    diy = pc.quote(plan_type=plan_type, tier_label=tier, with_appliance=False)
    assert diy["one_time_cents"] == cents and f"{capacity} cameras" in diy["one_time_lines"][0]["label"]
    appliance = pc.quote(plan_type=plan_type, tier_label=tier, with_appliance=True)
    assert appliance["one_time_cents"] == 0 and appliance["one_time_lines"][0]["included"] is True
    assert diy["monthly_cents"] == appliance["monthly_cents"]  # separate line item; plan price unchanged


def _paid_appliance_order(db_path, customer_id="cust-1", status="paid"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_checkout_session_id,quantity,amount_cents,currency,status,fulfillment_status,created_at,updated_at) "
                 "VALUES('hw-app',?,'AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter','price_hw','cs_hw_app',1,124999,'usd',?,'paid','2026-01-01','2026-01-01')",
                 (customer_id, status))
    conn.commit()
    conn.close()


@pytest.fixture()
def license_portal(portal, monkeypatch):
    for capacity in (8, 16, 32, 64):
        monkeypatch.setenv(f"ANYAICAM_STRIPE_PRICE_VMS_LICENSE_{capacity}", f"price_vms_{capacity}")
    return portal


def test_diy_customer_buys_the_one_time_license_for_the_chosen_capacity(license_portal, db_path):
    client, captured, sent = license_portal
    _seed(db_path)
    response = client.post("/api/customer/vms-license/checkout", json={"capacity": 16}, cookies=_cookie(*OWNER))
    assert response.status_code == 200, response.text
    fields = captured[-1]
    assert fields["mode"] == "payment"  # one-time, never a subscription
    assert fields["line_items[0][price]"] == "price_vms_16" and fields["line_items[0][quantity]"] == "1"
    assert fields["metadata[anyaicam_product_class]"] == "vms_license"
    assert fields["metadata[anyaicam_vms_license_capacity]"] == "16"
    assert not any(key.startswith("subscription_data") for key in fields)
    assert client.post("/api/customer/vms-license/checkout", json={"capacity": 12}, cookies=_cookie(*OWNER)).status_code == 400


def test_appliance_customer_is_never_charged_for_the_license(license_portal, db_path):
    client, captured, sent = license_portal
    _seed(db_path)
    _paid_appliance_order(db_path)
    response = client.post("/api/customer/vms-license/checkout", json={"capacity": 8}, cookies=_cookie(*OWNER))
    assert response.status_code == 409 and "appliance already includes" in response.json()["detail"]
    assert captured == []


def test_license_webhook_grants_capacity_without_adding_camera_slots(license_portal, db_path, monkeypatch):
    _seed(db_path)
    import customer_entitlements as ce
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_LOCAL_1_8="price_local_8")
    monkeypatch.setattr(ce, "PRICE_ID_VMS_LICENSE_MAP", ce._load_vms_license_map())
    event = {"id": "evt_lic", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_lic", "customer": "cus_1", "mode": "payment", "payment_status": "paid",
        "customer_details": {"email": "owner@example.test"},
        "metadata": {"anyaicam_stripe_price_id": "price_vms_8", "anyaicam_customer_id": "cust-1"}}}}
    assert ce.sync_entitlement_from_stripe_event(event)["status"] == "entitlement_updated"
    assert ce.vms_license_capacity("cust-1") == 8
    assert ce.total_camera_slots("cust-1") == 0  # a license is not camera slots
    ce.upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    assert ce.total_camera_slots("cust-1") == 8  # never double-counted
    client, captured, sent = license_portal
    again = client.post("/api/customer/vms-license/checkout", json={"capacity": 8}, cookies=_cookie(*OWNER))
    assert again.status_code == 409 and "already covers 8" in again.json()["detail"]


def test_appliance_included_license_follows_the_plan_capacity(db_path):
    _seed(db_path)
    _paid_appliance_order(db_path)
    import customer_entitlements as ce
    assert ce.appliance_includes_vms_license("cust-1")
    ce.upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=32)
    assert ce.vms_license_capacity("cust-1") == 32


def test_an_unpaid_appliance_order_does_not_include_a_license(db_path):
    _seed(db_path)
    _paid_appliance_order(db_path, status="pending")
    import customer_entitlements as ce
    assert not ce.appliance_includes_vms_license("cust-1")


def test_license_checkout_waits_for_a_pending_friends_family_decision(license_portal, db_path):
    client, captured, sent = license_portal
    _seed(db_path)
    client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER))
    assert client.post("/api/customer/vms-license/checkout", json={"capacity": 8}, cookies=_cookie(*OWNER)).status_code == 409
    assert captured == []


def test_plan_checkout_is_unaffected_by_the_license(license_portal, db_path):
    client, captured, sent = license_portal
    _seed(db_path)
    assert _checkout_plan(client).status_code == 200
    assert captured[-1]["mode"] == "subscription" and captured[-1]["line_items[0][price]"] == "price_local_8"


def test_license_payment_earns_no_commission(ledger, db_path, monkeypatch):
    sc = ledger
    result = sc.sync_commissions_from_stripe_event({"id": "evt_lic", "type": "checkout.session.completed",
                                                    "data": {"object": {"id": "cs_lic", "mode": "payment", "payment_status": "paid"}}})
    assert result["status"] == "ignored" and _entries(sc) == []


def test_my_subscription_offers_the_license_to_diy_and_shows_it_included_with_an_appliance(license_portal, db_path):
    client, captured, sent = license_portal
    _seed(db_path)
    import customer_entitlements as ce
    ce.upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=16)
    diy = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "Software license for 16 cameras" in diy and "$79.99 one-time" in diy and 'id="vms-license-buy"' in diy
    _paid_appliance_order(db_path)
    owner = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "Included with your AnyAiCam appliance &middot; 16 cameras" in owner and 'id="vms-license-buy"' not in owner


# ================================================================ Stripe object script (offline)

def _load_price_script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "tools" / "stripe_create_catalog_prices.py"
    spec = importlib.util.spec_from_file_location("stripe_create_catalog_prices", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_price_script_creates_new_prices_at_catalog_amounts_and_one_time_licenses():
    script = _load_price_script()
    items = {i["env"]: i for i in script.catalog_prices()}
    assert items["ANYAICAM_STRIPE_PRICE_HYBRID_33_64"]["amount"] == 9999 and items["ANYAICAM_STRIPE_PRICE_HYBRID_33_64"]["interval"] == "month"
    assert items["ANYAICAM_STRIPE_PRICE_VMS_LICENSE_64"]["amount"] == 19999 and items["ANYAICAM_STRIPE_PRICE_VMS_LICENSE_64"]["interval"] is None
    assert items["ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS"]["amount"] == 2499
    assert "ANYAICAM_STRIPE_PRICE_ANALYTICS_CLOUD_OVERFLOW" not in items  # no invented price
    # lookup keys embed the amount, so a changed price always becomes a NEW Price object
    assert all(str(i["amount"]) in i["lookup_key"] for i in items.values())
    coupons = {c["env"]: c["percent_off"] for c in script.catalog_coupons()}
    assert coupons == {"ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_BASE": 50, "ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_ANALYTICS": 25}


def test_price_script_refuses_live_keys(monkeypatch, capsys):
    script = _load_price_script()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_not_real")
    monkeypatch.setattr("sys.argv", ["stripe_create_catalog_prices.py", "--apply"])
    assert script.main() == 2
    assert "TEST-mode" in capsys.readouterr().err


# ================================================================ Stripe price guard

def test_checkout_refuses_a_stripe_price_that_does_not_match_the_catalog(portal, db_path, fake_stripe_prices):
    """A Price ID left over from earlier pricing (Stripe amounts are
    immutable) must never charge a customer something other than what
    they were shown."""
    client, captured, sent = portal
    _seed(db_path)
    fake_stripe_prices["price_local_8"] = {"id": "price_local_8", "unit_amount": 1499, "currency": "usd", "active": True,
                                           "recurring": {"interval": "month"}}
    assert _checkout_plan(client).status_code == 200  # matches: $14.99 monthly
    import main
    main._VERIFIED_STRIPE_PRICES.clear()
    fake_stripe_prices["price_local_8"]["unit_amount"] = 60  # an old sandbox amount
    captured.clear()
    response = _checkout_plan(client)
    assert response.status_code == 503 and "PRICE_MISMATCH" in response.json()["detail"]
    assert captured == []


def test_checkout_refuses_a_one_time_price_for_a_monthly_item(license_portal, db_path, fake_stripe_prices):
    client, captured, sent = license_portal
    _seed(db_path)
    fake_stripe_prices["price_vms_8"] = {"id": "price_vms_8", "unit_amount": 4999, "currency": "usd", "active": True,
                                         "recurring": {"interval": "month"}}  # wrong: the license is one-time
    response = client.post("/api/customer/vms-license/checkout", json={"capacity": 8}, cookies=_cookie(*OWNER))
    assert response.status_code == 503 and "PRICE_MISMATCH" in response.json()["detail"]
    assert captured == []



# ================================================================ Face Access sizing

def _enroll(db_path, count, customer_id="cust-1"):
    conn = sqlite3.connect(db_path)
    conn.executemany("INSERT INTO facial_people(id,customer_id,display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     [(f"person-{customer_id}-{i}", customer_id, f"Person {i}", "active", "2026-01-01", "2026-01-01") for i in range(count)])
    conn.commit()
    conn.close()


def _doors(db_path, camera_ids):
    conn = sqlite3.connect(db_path)
    for camera_id in camera_ids:
        conn.execute("UPDATE cameras SET door_access_enabled=1 WHERE id=?", (camera_id,))
    conn.commit()
    conn.close()


@pytest.fixture()
def face_portal(portal, monkeypatch):
    for size in ("SMALL", "MEDIUM", "LARGE"):
        monkeypatch.setenv(f"ANYAICAM_STRIPE_PRICE_FACE_ACCESS_{size}", f"price_face_{size.lower()}")
    return portal


def test_face_access_is_sold_in_the_customers_size_billed_per_door(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    _enroll(db_path, 30)  # Medium
    _doors(db_path, ["cam-1", "cam-2"])
    small = client.post("/api/customer/analytics/checkout", json={"addon_key": "face_access_small"}, cookies=_cookie(*OWNER))
    assert small.status_code == 400 and "Face Access Medium" in small.json()["detail"]
    medium = client.post("/api/customer/analytics/checkout", json={"addon_key": "face_access_medium", "quantity": 1}, cookies=_cookie(*OWNER))
    assert medium.status_code == 200, medium.text
    assert captured[-1]["line_items[0][price]"] == "price_face_medium"
    assert captured[-1]["line_items[0][quantity]"] == "2"  # every door set up, decided server-side
    assert "discounts[0][coupon]" not in captured[-1]


def test_face_access_gets_no_friends_family_discount(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    request_id = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]
    client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "approve"}, cookies=_make_global_admin(db_path))
    response = client.post("/api/customer/analytics/checkout", json={"addon_key": "face_access_small"}, cookies=_cookie(*OWNER))
    assert response.status_code == 200
    assert "discounts[0][coupon]" not in captured[-1]
    assert captured[-1]["metadata[anyaicam_friends_family]"] == "approved"


def test_over_500_people_is_enterprise_contact_not_an_online_purchase(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    _enroll(db_path, 501)
    for size in ("small", "medium", "large"):
        response = client.post("/api/customer/analytics/checkout", json={"addon_key": f"face_access_{size}"}, cookies=_cookie(*OWNER))
        assert response.status_code == 400 and "Enterprise" in response.json()["detail"]
    assert captured == []
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "Face Access Enterprise" in html and "custom pricing" in html
    assert 'data-addon-key="face_access_' not in html


def test_my_subscription_offers_only_the_matching_face_access_size(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    _enroll(db_path, 120)  # Large
    _doors(db_path, ["cam-1"])
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert 'data-addon-key="face_access_large"' in html
    assert "$69.99/mo per door · Includes: Facial Recognition · 1 door" in html
    assert 'data-addon-key="face_access_small"' not in html and 'data-addon-key="face_access_medium"' not in html
