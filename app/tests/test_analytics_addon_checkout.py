"""Commercial restructure confirmed 2026-09-21: POST /api/customer/
analytics/checkout -- the customer-facing checkout that was missing for
Advanced Analytics / Face Access add-ons.

analytics_entitlements.py already had a fully working, tested webhook
side (resolve_analytic()/_sync_checkout_completed()/upsert_analytics_
subscription(), already wired into the live POST /api/payments/stripe/
webhook route) -- but exactly the same gap test_camera_slot_checkout.py
documents for camera-slot tiers, nothing ever CREATED a Checkout Session
for one of these analytics Price IDs. This is that endpoint, mirroring
create_camera_slot_checkout()'s own tested pattern and safety discipline.

stripe_api_post() (the actual network call to Stripe) is monkeypatched
to capture the `fields` list instead of making a real HTTP request --
matching test_camera_slot_checkout.py's own established convention.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_analytics_addon_checkout.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_SMART_MOTION", "price_test_smart_motion")
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_FACIAL_RECOGNITION", "price_test_facial_recognition")
        monkeypatch.delenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_LPR", raising=False)

        captured = {}

        def _fake_stripe_api_post(path, fields):
            captured["path"] = path
            captured["fields"] = fields
            return {"id": "cs_test_analytics_1", "url": "https://checkout.stripe.test/cs_test_analytics_1"}

        monkeypatch.setattr(main, "stripe_api_post", _fake_stripe_api_post)

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client, captured


def _fields_dict(fields):
    return dict(fields)


def _owner_cookie(customer_id="cust-1", email="signedin@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _seed_tenant(db_path, customer_id="cust-1", email="signedin@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


# ------------------------------------------------------------ happy path


def test_advanced_analytics_key_creates_a_recurring_session_with_full_metadata(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "smart_motion"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["checkout_url"] == "https://checkout.stripe.test/cs_test_analytics_1"
    assert body["analytic_key"] == "smart_motion"
    assert body["billing_type"] == "recurring"

    fields = _fields_dict(captured["fields"])
    assert fields["mode"] == "subscription"
    assert fields["line_items[0][price]"] == "price_test_smart_motion"
    assert fields["line_items[0][quantity]"] == "1"
    assert fields["metadata[anyaicam_stripe_price_id]"] == "price_test_smart_motion"
    assert fields["metadata[anyaicam_customer_id]"] == "cust-1"
    assert fields["metadata[anyaicam_analytic_key]"] == "smart_motion"
    assert fields["subscription_data[metadata][anyaicam_stripe_price_id]"] == "price_test_smart_motion"
    assert fields["subscription_data[metadata][anyaicam_customer_id]"] == "cust-1"
    assert fields["client_reference_id"] == "cust-1"
    assert fields["customer_email"] == "owner@example.test"


def test_face_access_key_resolves_to_its_own_price_not_smart_motions(client, db_path):
    """Cross-add-on tampering guard: facial_recognition (Face Access)
    must never resolve to a different analytic's price."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "facial_recognition"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][price]"] == "price_test_facial_recognition"
    assert fields["metadata[anyaicam_analytic_key]"] == "facial_recognition"


# --------------------------------------------------------- ownership gate


def test_no_session_is_rejected(client):
    test_client, captured = client
    response = test_client.post("/api/customer/analytics/checkout", json={"analytic_key": "smart_motion"})
    assert response.status_code == 401
    assert "path" not in captured, "Stripe must never be called for an unauthenticated request"


def test_partner_owner_session_is_rejected_not_treated_as_a_customer(client):
    test_client, captured = client
    import partner_portal
    non_owner_cookie = partner_portal._token("partner@example.test", "partner_owner", "partner-1", None, None)
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "smart_motion"},
        cookies={partner_portal.SESSION_COOKIE: non_owner_cookie},
    )
    assert response.status_code == 403
    assert "path" not in captured


def test_customer_viewer_role_is_rejected(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="viewer@example.test")
    import partner_portal
    viewer_cookie = partner_portal._token("viewer@example.test", "customer_viewer", None, "cust-1", None)
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "smart_motion"},
        cookies={partner_portal.SESSION_COOKIE: viewer_cookie},
    )
    assert response.status_code == 403
    assert "path" not in captured


# ------------------------------------------------ catalog/price validation


def test_unknown_analytic_key_is_rejected(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "not_a_real_analytic"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 400
    assert "path" not in captured


def test_analytic_with_no_configured_price_id_fails_closed(client, db_path):
    """ANYAICAM_STRIPE_PRICE_ANALYTICS_LPR is deliberately left unset by
    this file's own fixture -- a real catalog entry that simply isn't
    purchasable yet must be refused, never silently substituted."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "lpr"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 503
    assert "PRICE_ID_REQUIRED" in response.json()["detail"]
    assert "path" not in captured


def test_a_submitted_price_id_field_is_ignored_server_side_resolution_wins(client, db_path):
    """Tampering guard: the request model has no price_id field at all,
    so even a client that adds one to the raw JSON body can never
    influence which Stripe price is actually charged."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "smart_motion", "price_id": "price_attacker_supplied"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][price]"] == "price_test_smart_motion"


# --------------------------------------------- webhook-to-entitlement path


def test_webhook_processes_the_exact_metadata_this_endpoint_sends_into_a_real_subscription(client, db_path, monkeypatch):
    """Integration proof that this endpoint's metadata contract is
    actually compatible with the existing, unmodified analytics webhook
    handler -- not just visually similar."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"analytic_key": "smart_motion"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])

    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
        event = {
            "id": "evt_test_1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_analytics_1",
                "customer": "cus_test_1",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {
                    "anyaicam_stripe_price_id": fields["metadata[anyaicam_stripe_price_id]"],
                    "anyaicam_customer_id": fields["metadata[anyaicam_customer_id]"],
                },
            }},
        }
        result = ae.sync_analytics_from_stripe_event(event)
        assert result["status"] == "analytics_subscription_updated"
        assert result["analytic_key"] == "smart_motion"
        assert ae.get_active_analytics_for_customer("cust-1") == ["smart_motion"]


def test_webhook_ignores_an_unverified_price_id_grants_nothing(client, db_path):
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    with override_target(sqlite_path=str(db_path)):
        from analytics_entitlements import sync_analytics_from_stripe_event, get_active_analytics_for_customer
        event = {
            "id": "evt_test_2",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_forged",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {"anyaicam_stripe_price_id": "price_never_configured", "anyaicam_customer_id": "cust-1"},
            }},
        }
        result = sync_analytics_from_stripe_event(event)
        assert result["status"] == "ignored"
        assert get_active_analytics_for_customer("cust-1") == []
