"""Staging end-to-end validation for the Phase 5/6 purchase-notification
work: real HTTP requests against the real, unmodified FastAPI app
(main.app) hitting the real POST /api/payments/stripe/webhook route --
real CSRF enforcement (matching production, not the test-only default
off), real Stripe-Signature HMAC verification (a genuine signature is
computed and sent, not bypassed via monkeypatch), real entitlement/
hardware-order sync, real purchase-notification dispatch, and real
PreviewEmail rendering of the resulting email content.

What this IS: the strongest validation achievable without a Stripe TEST/
SANDBOX secret key, which was not available anywhere in this environment
(searched extensively; none found) -- so this cannot create a real Stripe
Checkout Session or receive a webhook Stripe itself actually sent. What
it verifies instead is everything downstream of "Stripe delivered a
webhook": the exact same route, signature check, CSRF posture,
entitlement/hardware sync, and notification code that would run for a
real event, driven by hand-built event payloads shaped exactly like
Stripe's real ones (same structure the rest of this test suite already
uses). Price IDs are test-only synthetic strings, monkeypatched into
PRICE_ID_CAMERA_SLOT_MAP/HARDWARE_PRICE_MAP for this file only -- no real
Stripe Price ID exists for any of them (see hardware_orders.py's and
customer_entitlements.py's own docstrings on this point).

What this is NOT: a real Stripe TEST-mode purchase. That requires a
Stripe TEST secret key to (a) verify/create the four Local/Hybrid-style
test tiers used here as real Stripe Products/Prices and (b) actually run
a Checkout Session through Stripe's own servers -- neither was available.
See the staging validation report for exactly what remains blocked on
that credential.
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
import email_service
from database_backend import override_target

TIER_LOCAL_1_8 = "price_staging_local_1_8"
TIER_HYBRID_1_8 = "price_staging_hybrid_1_8"
RYZEN_STARTER_STAGING = "price_staging_ryzen_starter"
TEST_WEBHOOK_SECRET = "whsec_staging_validation_secret"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_staging_e2e_validation.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        import customer_entitlements as ce
        import hardware_orders as ho

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")
        # Real signature verification path, not bypassed -- the webhook
        # secret is set to a known test value and _signed_headers() below
        # computes a genuine HMAC against it, exactly like Stripe's own
        # signing scheme (mirrors test_stripe_webhook_csrf_exemption.py).
        monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
            TIER_LOCAL_1_8: {"product": "camera_slots_local", "camera_slot_maximum": 8},
            TIER_HYBRID_1_8: {"product": "camera_slots_hybrid", "camera_slot_maximum": 8},
        })
        monkeypatch.setattr(ho, "HARDWARE_PRICE_MAP", {
            RYZEN_STARTER_STAGING: {"sku": "AIC-APPLIANCE-RYZEN-STARTER", "product": "ryzen_starter", "name": "AnyAiCam Ryzen Starter Appliance", "amount_cents": 124999},
        })

        email_patch = dataclasses.replace(email_service.settings, email_backend="preview", email_preview_dir=str(tmp_path / "email-preview"))
        monkeypatch.setattr(email_service, "settings", email_patch)

        # Real CSRF enforcement ON, matching production -- proves the
        # Phase 4 CSRF exemption (app/cloud_security.py) still holds for
        # this webhook alongside all this session's new Phase 5/6 code.
        settings_patch = patch.object(
            cloud_security, "settings",
            dataclasses.replace(cloud_security.settings, csrf_enabled=True, secure_cookies=False),
        )
        settings_patch.start()
        try:
            with TestClient(main.app, follow_redirects=False) as test_client:
                yield test_client, tmp_path
        finally:
            settings_patch.stop()


def _seed_customer(db_path, customer_id="cust-1", email="staging-buyer@example.test", name="Sam Staging", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, name, email, "active", "2026-01-01"),
        )
        conn.commit()


def _checkout_event(event_id, price_id, *, email="staging-buyer@example.test", stripe_customer="cus_staging_1", customer_id=None, mode="subscription"):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_{event_id}", "mode": mode, "customer": stripe_customer, "customer_details": {"email": email},
            "payment_status": "paid", "client_reference_id": "primary", "metadata": metadata,
        }},
    }


def _signed_headers(payload_bytes, secret=TEST_WEBHOOK_SECRET):
    """A genuine Stripe-Signature header: real HMAC-SHA256 over
    `{timestamp}.{payload}`, exactly matching Stripe's own scheme and
    this app's verify_stripe_webhook_signature() implementation."""
    timestamp = int(time.time())
    signed_payload = f"{timestamp}".encode() + b"." + payload_bytes
    signature = hmac_module.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={timestamp},v1={signature}", "content-type": "application/json"}


def _post_webhook(test_client, event):
    payload = json.dumps(event).encode()
    return test_client.post("/api/payments/stripe/webhook", content=payload, headers=_signed_headers(payload))


def _previews(tmp_path):
    preview_dir = tmp_path / "email-preview"
    if not preview_dir.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(preview_dir.glob("*.json"))]


# =============================================================== ITEM 4


def test_local_1_8_full_stack_checkout_and_service_ready_email(client, db_path):
    test_client, tmp_path = client
    _seed_customer(db_path)

    response = _post_webhook(test_client, _checkout_event("evt_staging_local", TIER_LOCAL_1_8, customer_id="cust-1"))
    assert response.status_code == 200  # real CSRF exemption + real signature verification both passed

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
        entitlements = {e["product"]: e["camera_slot_quantity"] for e in ce.get_entitlements_for_customer("cust-1")}
    assert total == 8
    assert entitlements == {"camera_slots_local": 8}

    previews = _previews(tmp_path)
    assert len(previews) == 1
    email = previews[0]
    assert email["type"] == "account_ready"
    assert "Local" in email["text"]
    assert "8" in email["text"]
    assert "https://app.anyaicam.com" in email["text"]

    # Duplicate webhook delivery (Stripe retry semantics: same event id,
    # re-signed and re-sent) must not create a second email or a second
    # entitlement grant.
    replay = _post_webhook(test_client, _checkout_event("evt_staging_local", TIER_LOCAL_1_8, customer_id="cust-1"))
    assert replay.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        total_after_replay = ce.total_camera_slots("cust-1")
    assert total_after_replay == 8
    assert len(_previews(tmp_path)) == 1


# =============================================================== ITEM 5


def test_hybrid_1_8_full_stack_checkout_and_service_ready_email(client, db_path):
    test_client, tmp_path = client
    _seed_customer(db_path)

    response = _post_webhook(test_client, _checkout_event("evt_staging_hybrid", TIER_HYBRID_1_8, customer_id="cust-1"))
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        entitlements = {e["product"]: e["camera_slot_quantity"] for e in ce.get_entitlements_for_customer("cust-1")}
    assert entitlements == {"camera_slots_hybrid": 8}

    previews = _previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "account_ready"
    assert "Hybrid" in previews[0]["text"]
    assert "8" in previews[0]["text"]


# =============================================================== ITEM 6


def test_pending_customer_full_stack_setup_email_then_ready_email(client, db_path):
    test_client, tmp_path = client

    response = _post_webhook(test_client, _checkout_event("evt_staging_pending", TIER_LOCAL_1_8, email="new-staging-buyer@example.test", customer_id=None))
    assert response.status_code == 200

    previews = _previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "setup_required"

    # No ready email exists yet -- account doesn't exist.
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        import purchase_notifications as pn
        _seed_customer(db_path, customer_id="cust-pending", email="new-staging-buyer@example.test", name="New Staging Buyer")
        ce.resolve_pending_links_for_customer("cust-pending", "new-staging-buyer@example.test")
        results = pn.notify_registration_resolved("cust-pending")
    assert results and results[0]["status"] == "sent"

    previews_after = _previews(tmp_path)
    assert len(previews_after) == 2
    assert sorted(p["type"] for p in previews_after) == ["account_ready", "setup_required"]


# =============================================================== ITEM 7


def test_hardware_purchase_full_stack_order_confirmation_no_camera_slot_email(client, db_path):
    test_client, tmp_path = client
    _seed_customer(db_path)

    response = _post_webhook(test_client, _checkout_event("evt_staging_hw", RYZEN_STARTER_STAGING, customer_id="cust-1", mode="payment"))
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        import hardware_orders as ho
        total_slots = ce.total_camera_slots("cust-1")
        orders = ho.get_orders_for_customer("cust-1")
    assert total_slots == 0  # hardware purchase never grants camera slots
    assert len(orders) == 1
    assert orders[0]["sku"] == "AIC-APPLIANCE-RYZEN-STARTER"

    previews = _previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "hardware_order_confirmation"


# =============================================================== ITEM 8


def test_email_delivery_failure_full_stack_entitlement_correct(client, db_path, monkeypatch):
    """First half of item 8: entitlement processing is fully correct and
    unaffected by an email send failure, and the failure is durably
    recorded (not silently lost)."""
    test_client, tmp_path = client
    _seed_customer(db_path)
    event = _checkout_event("evt_staging_flaky", TIER_LOCAL_1_8, customer_id="cust-1")

    import purchase_notifications as pn

    class _FailingBackend:
        def send(self, *a, **k):
            raise RuntimeError("staging SMTP simulated failure")

    monkeypatch.setattr(pn, "get_email_service", lambda: _FailingBackend())
    response = _post_webhook(test_client, event)
    assert response.status_code == 200  # webhook route itself never surfaces the email failure to Stripe

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        from partner_db import row
        total = ce.total_camera_slots("cust-1")
        failed_row = row("SELECT status,error_detail FROM provisioning_notifications WHERE stripe_event_id=?", ("evt_staging_flaky",))
    assert total == 8  # entitlement committed correctly regardless of email outcome
    assert failed_row["status"] == "failed"
    assert failed_row["error_detail"]  # the real exception text is durably recorded
    assert _previews(tmp_path) == []


def test_STAGING_FINDING_a_redelivered_event_never_reaches_the_retry_because_of_pre_existing_legacy_dedup(client, db_path, monkeypatch):
    """Real finding from this staging validation, not a pre-written
    expectation: main.py's webhook route has a pre-existing, Phase-6-
    unrelated idempotency gate --

        if not record_stripe_webhook_event(event): return {"status":
        "complete", "duplicate": True}

    -- that runs BEFORE process_stripe_webhook_event(), BEFORE the
    entitlement/hardware syncs, and BEFORE notify_from_stripe_event(),
    for ANY event id the route has seen before. A literal redelivery of
    the exact same Stripe event (a genuine Stripe retry, or a human
    clicking "resend" in the Stripe Dashboard) short-circuits at that
    gate and never reaches Phase 6's notification code at all -- so a
    'failed' provisioning_notifications row set on the first delivery is
    NOT actually retried by a real webhook redelivery today, contradicting
    what this test originally assumed before being run against the real
    route (a purely direct-function-call unit test would never have
    caught this, since it never goes through record_stripe_webhook_event()
    in the first place). This is the most important actionable output of
    this staging pass -- see the final report.
    """
    test_client, tmp_path = client
    _seed_customer(db_path)
    event = _checkout_event("evt_staging_flaky2", TIER_LOCAL_1_8, customer_id="cust-1")

    import purchase_notifications as pn

    class _FailingBackend:
        def send(self, *a, **k):
            raise RuntimeError("staging SMTP simulated failure")

    monkeypatch.setattr(pn, "get_email_service", lambda: _FailingBackend())
    _post_webhook(test_client, event)

    monkeypatch.setattr(pn, "get_email_service", email_service.get_email_service)
    redelivery = _post_webhook(test_client, event)  # same event id, real Stripe-retry shape
    assert redelivery.status_code == 200
    assert redelivery.json().get("duplicate") is True  # confirms the pre-existing gate is what fired

    with override_target(sqlite_path=str(db_path)):
        from partner_db import row
        still_failed = row("SELECT status FROM provisioning_notifications WHERE stripe_event_id=?", ("evt_staging_flaky2",))
    assert still_failed["status"] == "failed"  # never retried by the redelivery itself
    assert _previews(tmp_path) == []


def test_a_failed_notification_CAN_be_retried_by_directly_re_invoking_the_notifier(client, db_path, monkeypatch):
    """Proves the underlying send/retry logic itself is sound -- it's
    only unreachable via a real webhook redelivery (see the finding
    above). This is the shape a future explicit retry-sweep job (outside
    the webhook route) would need: re-run notify_from_stripe_event() for
    the original event directly, not rely on Stripe redelivering it."""
    test_client, tmp_path = client
    _seed_customer(db_path)
    event = _checkout_event("evt_staging_flaky3", TIER_LOCAL_1_8, customer_id="cust-1")

    import purchase_notifications as pn

    class _FailingBackend:
        def send(self, *a, **k):
            raise RuntimeError("staging SMTP simulated failure")

    monkeypatch.setattr(pn, "get_email_service", lambda: _FailingBackend())
    _post_webhook(test_client, event)
    monkeypatch.setattr(pn, "get_email_service", email_service.get_email_service)

    with override_target(sqlite_path=str(db_path)):
        result = pn.notify_from_stripe_event(event)  # a retry sweep would call exactly this
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
    assert result["status"] == "sent"
    assert total == 8  # still correct, still not duplicated
    previews = _previews(tmp_path)
    assert len(previews) == 1
    assert previews[0]["type"] == "account_ready"
