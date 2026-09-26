"""Provisioning Phase 4: security fix for a real, live bug confirmed via
a read-only trace against production (app.anyaicam.com) -- every POST to
POST /api/payments/stripe/webhook, including one with a stripe-signature
header, was rejected 403 "CSRF validation failed" by
cloud_security.ProductionSecurityMiddleware BEFORE the route's own
verify_stripe_webhook_signature() ever ran. Root cause: that middleware
only exempted `/api/appliance/*` and exactly `/partner-logout` from CSRF
-- Stripe's real webhook request carries a Stripe-Signature header, never
our anyaicam_csrf cookie/token pair, so it could never have passed.

Why this was never caught by the extensive existing webhook test suite
(test_stripe_webhook_entitlement_sync.py, etc.): cloud_config.settings.
csrf_enabled defaults to False (ANYAICAM_CSRF_ENABLED unset) and none of
those tests ever turned it on -- so this middleware path was never
actually exercised locally, only live, where the real deployment DOES
run with CSRF enabled. Every test in this file explicitly enables CSRF
(csrf_enabled=True, mirroring test_customer_registration_csrf.py's own
pattern) specifically to close that gap.

The fix (cloud_security.py): an EXACT-path exemption
(CSRF_EXEMPT_EXACT_PATHS = {'/api/payments/stripe/webhook'}), not a
prefix -- '/api/payments/*' and '/api/*' remain fully CSRF-protected.
The webhook route's own Stripe-Signature verification is completely
unchanged; this removes only the CSRF check, never authentication.
"""
import dataclasses
import hashlib
import hmac as hmac_module
import json
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import cloud_security
from database_backend import override_target

TIER_1_16 = "price_test_csrf_fix_1_16"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_stripe_webhook_csrf_exemption.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        import customer_entitlements as ce

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")
        monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret_for_csrf_fix")
        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {TIER_1_16: {"product": "camera_slots", "camera_slot_maximum": 16}})

        # CSRF explicitly ON, matching production -- see module docstring
        # for why every prior webhook test ran with this off and so never
        # exercised the bug this file targets. allowed_origins deliberately
        # NOT restricted to TestClient's origin: a real Stripe webhook
        # request never sends a browser-style Origin header at all, so
        # the middleware's separate Origin-allowlist check (a different
        # code path from CSRF) never triggers for it either, exactly like
        # this test's requests below.
        settings_patch = patch.object(
            cloud_security, "settings",
            dataclasses.replace(cloud_security.settings, csrf_enabled=True, secure_cookies=False),
        )
        settings_patch.start()
        try:
            with TestClient(main.app, follow_redirects=False) as test_client:
                yield test_client, main
        finally:
            settings_patch.stop()


def _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _checkout_event(event_id, *, price_id=TIER_1_16, email="real-customer@example.test", stripe_customer="cus_csrf_1", customer_id=None):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_{event_id}", "customer": stripe_customer, "customer_details": {"email": email},
            "payment_status": "paid", "client_reference_id": "primary", "metadata": metadata,
        }},
    }


def _signed_headers(payload_bytes, secret="whsec_test_secret_for_csrf_fix"):
    timestamp = int(time.time())
    signed_payload = f"{timestamp}".encode() + b"." + payload_bytes
    signature = hmac_module.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={timestamp},v1={signature}", "content-type": "application/json"}


def _post_webhook(test_client, event, *, signed=True, signature_header="t=1,v1=deadbeef"):
    payload = json.dumps(event).encode()
    headers = _signed_headers(payload) if signed else {"stripe-signature": signature_header, "content-type": "application/json"}
    return test_client.post("/api/payments/stripe/webhook", content=payload, headers=headers)


def _post_json_no_csrf(test_client, path, body=None):
    return test_client.post(path, json=body or {})


# ------------------------------------------------------- the exemption itself


def test_stripe_webhook_bypasses_csrf_even_with_no_cookie_or_token(client, db_path):
    """The core fix: a request with zero CSRF cookie/token must not be
    rejected 403 before ever reaching Stripe-Signature verification --
    it must reach the route and fail there instead (invalid signature,
    not CSRF), proving the exemption is exact-path and functional."""
    test_client, main = client
    response = _post_webhook(test_client, _checkout_event("evt_1"), signed=False)
    assert response.status_code != 403 or "CSRF" not in response.text
    assert response.json().get("detail") == "Invalid Stripe webhook signature."


def test_normal_post_endpoints_still_require_csrf(client, db_path):
    """Regression guard: the fix must be exact-path, not a broad
    /api/payments/* or /api/* carve-out. A same-shaped POST under
    /api/payments/ that is NOT the webhook path must still be CSRF-
    blocked exactly as before. Uses a real signed-in session (but
    deliberately no CSRF cookie/token) so the request gets past this
    route's own authentication and isolates the CSRF check specifically
    -- a fully anonymous request 401s on authentication first, which
    would prove nothing about CSRF."""
    test_client, main = client
    main.save_users([{"id": "csrf-test-1", "email": "csrf-test@example.test", "role": "viewer", "enabled": True, "camera_ids": []}])
    token = main.create_session("csrf-test-1")
    response = test_client.post(
        "/api/payments/checkout", json={"plan": "starter", "quantity": 1},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 403
    assert "CSRF" in response.json().get("detail", "")


def test_appliance_exemption_remains_unchanged(client, db_path):
    """The pre-existing /api/appliance/ exemption must still work,
    proving this fix is additive, not a rewrite of the exemption logic."""
    test_client, main = client
    # No real appliance/credential seeded -- this only proves the request
    # reaches route-level auth (401/403 from authenticate_appliance()),
    # never the CSRF layer (which would also be a 403, but with the
    # "CSRF validation failed" detail specifically).
    response = test_client.post("/api/provisioning/refresh")
    assert response.json().get("detail") != "CSRF validation failed."


def test_partner_logout_exemption_remains_unchanged(client, db_path):
    test_client, main = client
    response = test_client.post("/partner-logout")
    assert response.status_code != 403 or response.json().get("detail") != "CSRF validation failed."


# ------------------------------------------------- Stripe signature security


def test_missing_signature_is_rejected_by_webhook_logic_not_csrf(client, db_path):
    test_client, main = client
    response = test_client.post(
        "/api/payments/stripe/webhook",
        content=json.dumps(_checkout_event("evt_missing_sig")).encode(),
        headers={"content-type": "application/json"},  # no stripe-signature header at all
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stripe webhook signature."


def test_invalid_signature_is_rejected_by_webhook_logic(client, db_path):
    test_client, main = client
    response = _post_webhook(test_client, _checkout_event("evt_bad_sig"), signed=False)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stripe webhook signature."


def test_valid_signature_reaches_event_handling_and_grants_entitlement(client, db_path):
    test_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    response = _post_webhook(test_client, _checkout_event("evt_valid_sig", customer_id="cust-1"), signed=True)
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
    assert total == 16


def test_webhook_idempotency_preserved_with_csrf_enabled(client, db_path):
    """The legacy JSON-file idempotency (record_stripe_webhook_event())
    must still gate a duplicate exactly as before, now that CSRF is on."""
    test_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    event = _checkout_event("evt_dup_csrf", customer_id="cust-1")

    first = _post_webhook(test_client, event, signed=True)
    second = _post_webhook(test_client, event, signed=True)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("duplicate") is True

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
        entitlements = ce.get_entitlements_for_customer("cust-1")
    assert total == 16
    assert len(entitlements) == 1
