"""Provisioning Phase 2/3: proves POST /api/payments/checkout carries
authoritative identity through Stripe metadata instead of trusting
customer-supplied email as primary identity when the caller is already
signed in via the authoritative customer system (partner_identity()),
while preserving every existing legacy field/behavior for a caller with
no authoritative session at all (current_user()/users.json/
billing_accounts.json untouched -- see main.py's create_stripe_checkout()
for the exact diff and rationale). Phase 3 additionally proves the fixed-
tier hardening: a customer-submitted `quantity` never reaches Stripe as
a slot-count vector (line-item quantity hard-coded to 1, no camera-slot-
bearing metadata sent) -- the server-selected Stripe Price ID
(`anyaicam_stripe_price_id`) is the only camera-slot-relevant signal.

stripe_api_post() (the actual network call to Stripe) is monkeypatched
to capture the `fields` list instead of making a real HTTP request --
this file never talks to Stripe.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_stripe_checkout_authoritative_identity.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "STRIPE_PRICE_STARTER", "price_test_starter")
        monkeypatch.setattr(main, "STRIPE_PRICE_PROFESSIONAL", "price_test_pro")
        monkeypatch.setattr(main, "STRIPE_PRICE_ENTERPRISE", "price_test_ent")
        monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")

        captured = {}

        def _fake_stripe_api_post(path, fields):
            captured["path"] = path
            captured["fields"] = fields
            return {"id": "cs_test_123", "url": "https://checkout.stripe.test/cs_test_123"}

        monkeypatch.setattr(main, "stripe_api_post", _fake_stripe_api_post)

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client, captured


def _fields_dict(fields):
    """fields is a list of (key, value) tuples -- Stripe's form-encoding
    shape, which allows repeated keys like line_items[0][...]. A plain
    dict is fine here since none of the keys this test inspects repeat."""
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


def _legacy_session_cookie(main, email="legacy-user@example.test"):
    """A plain legacy VMS user (users.json/current_user()) -- the 'no
    authoritative session at all' case this file's legacy-path tests
    exercise. Distinct from an authoritative customer_owner session."""
    main.save_users([{"id": "legacy-1", "email": email, "role": "viewer", "enabled": True, "camera_ids": []}])
    return main.create_session("legacy-1")


# ------------------------------------------------- legacy/anonymous path


def test_anonymous_checkout_keeps_every_existing_legacy_field(client):
    test_client, captured = client
    import main
    token = _legacy_session_cookie(main)
    response = test_client.post(
        "/api/payments/checkout", json={"plan": "starter", "quantity": 1},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 200
    fields = _fields_dict(captured["fields"])
    # Every field the legacy flow already sent must still be present, unchanged.
    assert fields["mode"] == "subscription"
    assert fields["line_items[0][price]"] == "price_test_starter"
    assert fields["metadata[anyaicam_plan]"] == "starter"
    assert "metadata[anyaicam_account_id]" in fields
    assert "metadata[anyaicam_user_id]" in fields
    # No authoritative session -> no authoritative customer id metadata at all.
    assert "metadata[anyaicam_customer_id]" not in fields
    assert "subscription_data[metadata][anyaicam_customer_id]" not in fields


def test_customer_submitted_quantity_is_never_sent_to_stripe_as_a_slot_multiplier(client):
    """Phase 3: fixed camera-slot tiers -- a customer-submitted `quantity`
    must never reach Stripe as the line-item quantity (it is hard-coded
    to 1) or as camera-slot-bearing metadata, even though the request
    field itself still exists on the model for backward compatibility."""
    test_client, captured = client
    import main
    token = _legacy_session_cookie(main)
    response = test_client.post(
        "/api/payments/checkout", json={"plan": "professional", "quantity": 6},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 200
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][quantity]"] == "1"
    assert "metadata[anyaicam_camera_slot_quantity]" not in fields
    assert "subscription_data[metadata][anyaicam_camera_slot_quantity]" not in fields


def test_checkout_carries_the_server_selected_price_id_never_a_browser_value(client):
    """The sole source of camera-slot entitlement quantity going forward
    -- server-selected via stripe_price_map(), independently of whatever
    the request body contains."""
    test_client, captured = client
    import main
    token = _legacy_session_cookie(main)
    response = test_client.post(
        "/api/payments/checkout", json={"plan": "professional", "quantity": 1},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 200
    fields = _fields_dict(captured["fields"])
    assert fields["metadata[anyaicam_stripe_price_id]"] == "price_test_pro"
    assert fields["subscription_data[metadata][anyaicam_stripe_price_id]"] == "price_test_pro"


# --------------------------------------------- authoritative signed-in path


def test_signed_in_customer_checkout_carries_the_authoritative_customer_id(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="signedin@example.test")
    import partner_portal
    response = test_client.post(
        "/api/payments/checkout",
        json={"plan": "starter", "quantity": 3},
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
    )
    assert response.status_code == 200
    fields = _fields_dict(captured["fields"])
    assert fields["metadata[anyaicam_customer_id]"] == "cust-1"
    assert fields["subscription_data[metadata][anyaicam_customer_id]"] == "cust-1"
    assert fields["metadata[anyaicam_stripe_price_id]"] == "price_test_starter"
    assert "metadata[anyaicam_camera_slot_quantity]" not in fields  # the customer-submitted quantity (3) is never sent as slot-bearing metadata


def test_signed_in_customer_checkout_prefers_the_authoritative_email_for_stripe_customer_email(client, db_path):
    """Do NOT trust customer-supplied email as primary identity when the
    customer is already signed in -- the authoritative customers.email
    must win over whatever the legacy current_user()/billing_accounts
    path would have used."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="authoritative-email@example.test")
    import partner_portal
    response = test_client.post(
        "/api/payments/checkout",
        json={"plan": "starter", "quantity": 1},
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie(email="authoritative-email@example.test")},
    )
    assert response.status_code == 200
    fields = _fields_dict(captured["fields"])
    assert fields.get("customer_email") == "authoritative-email@example.test"


def test_non_customer_owner_session_is_treated_as_legacy_not_authoritative(client, db_path):
    """A partner/administrator session must not be mistaken for a
    customer's own authoritative identity."""
    test_client, captured = client
    import partner_portal
    non_owner_cookie = partner_portal._token("partner@example.test", "partner_owner", "partner-1", None, None)
    response = test_client.post(
        "/api/payments/checkout",
        json={"plan": "starter", "quantity": 1},
        cookies={partner_portal.SESSION_COOKIE: non_owner_cookie},
    )
    assert response.status_code == 200
    fields = _fields_dict(captured["fields"])
    assert "metadata[anyaicam_customer_id]" not in fields
