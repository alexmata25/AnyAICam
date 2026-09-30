"""Follow-ups from the 2026-09-30 staging Stripe / Friends & Family E2E.

1. Back from Stripe Checkout, the setup page now says what happened. It only
   reports "active" once the verified webhook has granted the purchase; the
   return URL itself grants nothing.
2. The setup wizard's step tabs navigate (they looked clickable but did
   nothing).
3. The approved Friends & Family message names everything that is not
   discounted (hardware, VMS licenses, Face Access), from the catalog.
4. Face Access is billed per door: no door, no checkout and no grant."""
import re
import sqlite3

import pytest

import analytics_entitlements as ae
from database_backend import override_target
from test_pricing_ff_commission import (  # noqa: F401
    OWNER, _checkout_event, _cookie, _doors, _make_global_admin, _seed, _stripe_maps, db_path, face_portal, portal,
)

SESSION = "cs_test_a1b2c3d4e5f6"


# ------------------------------------------------------------ 1. payment return

def _stripe_session(monkeypatch, *, customer_id="cust-1", status="complete", payment_status="paid", **metadata):
    import main
    calls = []

    def _get(path):
        calls.append(path)
        return {"id": SESSION, "status": status, "payment_status": payment_status,
                "metadata": {"anyaicam_customer_id": customer_id, **metadata}}

    monkeypatch.setattr(main, "stripe_api_get", _get)
    return calls


def _return_status(client):
    return client.get(f"/api/customer/setup/checkout-return?session_id={SESSION}", cookies=_cookie(*OWNER))


def test_setup_page_shows_a_confirmation_panel_after_a_successful_checkout(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    html = client.get(f"/customer/setup?camera_plan_payment=success&session_id={SESSION}", cookies=_cookie(*OWNER)).text
    assert 'id="payment-return"' in html and f'data-session-id="{SESSION}"' in html
    assert "Confirming your payment with Stripe" in html
    assert "/api/customer/setup/checkout-return?session_id=" in html


def test_cancelled_checkout_says_nothing_was_charged(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    html = client.get("/customer/setup?camera_plan_payment=cancelled", cookies=_cookie(*OWNER)).text
    assert "Checkout cancelled. Nothing was charged." in html


def test_no_panel_without_a_return_and_no_markup_from_a_malformed_session_id(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    assert 'id="payment-return"' not in client.get("/customer/setup", cookies=_cookie(*OWNER)).text
    html = client.get('/customer/setup?camera_plan_payment=success&session_id=cs_test_x"><script>alert(1)</script>', cookies=_cookie(*OWNER)).text
    assert 'id="payment-return"' not in html and "alert(1)" not in html


def test_paid_plan_reads_activating_until_the_webhook_grants_it_then_active(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    _stripe_session(monkeypatch, anyaicam_camera_slot_plan_type="local", anyaicam_camera_slot_tier_label="1-8")
    first = _return_status(client).json()
    assert first["state"] == "processing"
    assert first["title"] == "Payment received. Activating your Local 1-8 camera plan…"
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    second = _return_status(client).json()
    assert second["state"] == "active"
    assert second["title"] == "Payment received. Your Local 1-8 camera plan is active."


def test_the_return_check_grants_nothing(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    _stripe_session(monkeypatch, anyaicam_camera_slot_plan_type="local", anyaicam_camera_slot_tier_label="1-8")
    _return_status(client)
    client.get(f"/customer/setup?camera_plan_payment=success&session_id={SESSION}", cookies=_cookie(*OWNER))
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import total_camera_slots
        assert total_camera_slots("cust-1") == 0


def test_add_on_return_is_reported_by_its_own_name(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    _stripe_session(monkeypatch, anyaicam_addon_key="talk_down")
    assert _return_status(client).json()["title"].startswith("Payment received. Activating your Talk Down")
    with override_target(sqlite_path=str(db_path)):
        ae.upsert_addon_subscription(customer_id="cust-1", addon_key="talk_down", status="active", quantity=1)
    assert _return_status(client).json()["state"] == "active"


def test_unpaid_session_is_never_reported_as_received(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    _stripe_session(monkeypatch, status="open", payment_status="unpaid",
                    anyaicam_camera_slot_plan_type="local", anyaicam_camera_slot_tier_label="1-8")
    body = _return_status(client).json()
    assert body["state"] == "not_paid" and "received" not in body["title"].lower()


def test_another_customers_session_is_not_found(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    _stripe_session(monkeypatch, customer_id="cust-other", anyaicam_camera_slot_plan_type="local", anyaicam_camera_slot_tier_label="1-8")
    assert _return_status(client).status_code == 404


def test_stripe_unreachable_is_reported_plainly(portal, db_path, monkeypatch):
    import main
    client, captured, sent = portal
    _seed(db_path)

    def _down(path):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(main, "stripe_api_get", _down)
    body = _return_status(client).json()
    assert body["state"] == "unknown" and "couldn't check" in body["title"]


def test_malformed_session_id_is_refused_without_calling_stripe(portal, db_path, monkeypatch):
    client, captured, sent = portal
    _seed(db_path)
    calls = _stripe_session(monkeypatch)
    response = client.get("/api/customer/setup/checkout-return?session_id=../../v1/customers", cookies=_cookie(*OWNER))
    assert response.status_code == 400 and calls == []


# ------------------------------------------------------------ 2. wizard tabs

def test_setup_tabs_are_real_navigation(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    html = client.get("/customer/setup", cookies=_cookie(*OWNER)).text
    tabs = re.findall(r'<button type="button" class="workspace-tab[^"]*" data-step="(\d)"', html)
    assert tabs == ["1", "2", "3", "4", "5", "6", "7"]
    assert "setupTabs.forEach(tab=>tab.onclick=" in html  # every tab has a handler
    assert "setAttribute('aria-current','step')" in html


# ------------------------------------------------------------ 3. Friends & Family copy

def test_approved_message_names_everything_not_discounted():
    import friends_family
    message = friends_family.approved_message()
    assert message == ("Approved: 50% off your camera plan and 25% off analytics packages and Talk Down. "
                       "Hardware, VMS software licenses and Face Access are not discounted.")


def test_approved_customer_gets_the_catalog_message(portal, db_path):
    client, captured, sent = portal
    _seed(db_path)
    request_id = client.post("/api/customer/friends-family/request", json={}, cookies=_cookie(*OWNER)).json()["request_id"]
    client.post(f"/api/admin/friends-family/{request_id}/decision", json={"decision": "approve"}, cookies=_make_global_admin(db_path))
    status = client.get("/api/customer/friends-family", cookies=_cookie(*OWNER)).json()
    assert status["status"] == "approved"
    assert "VMS software licenses" in status["approved_message"] and "Face Access" in status["approved_message"]
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "Hardware is not discounted." not in html  # the old, incomplete wording


# ------------------------------------------------------------ 4. Face Access with no doors

def test_face_access_checkout_is_refused_with_no_door_and_nothing_goes_to_stripe(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    response = client.post("/api/customer/analytics/checkout", json={"addon_key": "face_access_small"}, cookies=_cookie(*OWNER))
    assert response.status_code in (400, 409)
    assert "billed per door" in response.json()["detail"]
    assert captured == []


def test_one_door_is_billed_as_exactly_one(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    _doors(db_path, ["cam-1"])
    response = client.post("/api/customer/analytics/checkout", json={"addon_key": "face_access_small"}, cookies=_cookie(*OWNER))
    assert response.status_code == 200
    assert captured[-1]["line_items[0][quantity]"] == "1"


def test_subscription_page_explains_the_missing_door_instead_of_offering_one(face_portal, db_path):
    client, captured, sent = face_portal
    _seed(db_path)
    html = client.get("/subscription-portal", cookies=_cookie(*OWNER)).text
    assert "No door cameras set up yet" in html and "1 door" not in html
    assert "billed per door" in html
    assert 'data-addon-key="face_access_small"' not in html


@pytest.mark.parametrize("metadata_quantity,amount_total", [(None, 3999), (1, 0)])
def test_webhook_grants_no_face_access_without_a_paid_door(db_path, monkeypatch, metadata_quantity, amount_total):
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_FACE_ACCESS_SMALL="price_face_small")
    event = _checkout_event("evt_face_zero", "price_face_small", quantity=metadata_quantity)
    event["data"]["object"]["amount_total"] = amount_total
    with override_target(sqlite_path=str(db_path)):
        result = ae.sync_analytics_from_stripe_event(event)
        assert result["status"] == "ignored"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM addon_subscriptions WHERE customer_id='cust-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM analytics_subscriptions WHERE customer_id='cust-1'").fetchone()[0] == 0
    conn.close()


def test_webhook_grants_face_access_for_a_paid_door(db_path, monkeypatch):
    _seed(db_path)
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_FACE_ACCESS_SMALL="price_face_small")
    event = _checkout_event("evt_face_one", "price_face_small", quantity=1)
    event["data"]["object"]["amount_total"] = 3999
    with override_target(sqlite_path=str(db_path)):
        assert ae.sync_analytics_from_stripe_event(event)["status"] == "analytics_subscription_updated"
        assert ae.row("SELECT status,quantity FROM addon_subscriptions WHERE customer_id='cust-1' AND addon_key='face_access_small'") == {
            "status": "active", "quantity": 1}
