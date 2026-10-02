"""Direct self-service customer onboarding (2026-10-02 launch requirement).

Website -> create account -> verify email -> choose Local/Hybrid -> Stripe
checkout -> entitlement -> My subscription -> licensed installer download.
A direct customer needs no administrator approval and no partner
assignment; partner-assisted onboarding (/customer-register, partner
workspace) keeps working unchanged. Stripe is mocked throughout -- the
checkout is the existing camera-slot / VMS-licence checkout, not a copy.
"""
import json
import re
import sqlite3

import pytest

from database_backend import override_target
from test_customer_downloads import _publish, package, storage  # noqa: F401 -- fixtures
from test_pricing_ff_commission import _stripe_maps, db_path, license_portal, portal  # noqa: F401 -- fixtures

LINK = re.compile(r"/customer/verify-email\?token=([A-Za-z0-9_\-]+)")
PASSWORD = "direct-owner-pass-1"


@pytest.fixture()
def site(license_portal, db_path, monkeypatch, tmp_path):
    client, captured, _ = license_portal
    import email_service
    import main
    import customer_entitlements as ce
    mail = []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            # The real service refuses unknown types; so does this fake.
            if message_type not in email_service.EMAIL_TYPES:
                raise ValueError("Unsupported email type.")
            if to.startswith("smtp-down"):
                raise OSError("connection refused")
            mail.append({"to": to, "subject": subject, "text": text})
            return {"status": "sent"}
    monkeypatch.setattr(email_service, "get_email_service", lambda: _Mail())
    monkeypatch.setattr(main, "verify_stripe_webhook_signature", lambda payload, signature: True)
    monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
    monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
    monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")
    _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_LOCAL_1_8="price_local_8")
    monkeypatch.setattr(ce, "PRICE_ID_VMS_LICENSE_MAP", ce._load_vms_license_map())
    try:
        import direct_onboarding
    except ImportError:  # only while proving these tests fail without the feature
        direct_onboarding = None
    if direct_onboarding:
        direct_onboarding._signup_limiter.events.clear()
        direct_onboarding._verify_limiter.events.clear()
    return client, captured, mail


def _signup(client, email, name="Dana Direct", password=PASSWORD):
    return client.post("/api/customer/direct-signup", json={"name": name, "email": email, "password": password,
                                                            "confirm_password": password})


def _token(mail, email):
    return LINK.search([m for m in mail if m["to"] == email][-1]["text"]).group(1)


def _login(client, email, password=PASSWORD, **flags):
    client.cookies.clear()
    return client.post("/api/partner-login", json={"email": email, "password": password, **flags})


def _signed_in(response):
    import partner_portal
    return response.status_code in (200, 303) and partner_portal.SESSION_COOKIE in response.cookies


def _one(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(sql, args).fetchone()
        return dict(found) if found else None
    finally:
        conn.close()


def _count(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def _direct_owner(client, mail, email="dana@example.test"):
    assert _signup(client, email).status_code == 200
    page = client.get(f"/customer/verify-email?token={_token(mail, email)}")
    assert page.status_code == 200 and "Your account is ready" in page.text
    assert _signed_in(_login(client, email, customer_only=True))
    return email


def _webhook(client, event):
    return client.post("/api/payments/stripe/webhook", content=json.dumps(event),
                       headers={"stripe-signature": "t=1,v1=test", "content-type": "application/json"})


def _completed(event_id, price_id, customer_id, email, mode="subscription"):
    return {"id": event_id, "type": "checkout.session.completed", "data": {"object": {
        "id": f"cs_{event_id}", "customer": f"cus_{customer_id[:6]}", "mode": mode, "payment_status": "paid",
        "customer_details": {"email": email}, "client_reference_id": customer_id,
        "metadata": {"anyaicam_stripe_price_id": price_id, "anyaicam_customer_id": customer_id}}}}


# ------------------------------------------------------------------ 1. create and verify

def test_a_visitor_creates_and_verifies_a_direct_owner_account(site, db_path):
    client, _, mail = site
    page = client.get("/customer-signup")
    assert page.status_code == 200 and "Create your AnyAiCam account" in page.text and "/customer-register" in page.text
    login_page = open("customer-login.html", encoding="utf-8").read()
    assert 'href="/customer-signup"' in login_page and 'href="/customer-register"' in login_page  # both paths offered
    response = _signup(client, "Dana@Example.test")
    assert response.status_code == 200 and "Check your email" in response.json()["message"]
    # Nothing exists, and nobody can sign in, until the address is proven.
    assert _count(db_path, "SELECT COUNT(*) FROM customers WHERE email='dana@example.test'") == 0
    assert _login(client, "dana@example.test", customer_only=True).status_code == 403
    raw = _token(mail, "dana@example.test")
    stored = _one(db_path, "SELECT * FROM direct_signups WHERE email='dana@example.test'")
    assert raw not in json.dumps(stored) and PASSWORD not in json.dumps(stored)  # hash only
    assert client.get(f"/customer/verify-email?token={raw}").status_code == 200
    user = _one(db_path, "SELECT * FROM partner_users WHERE email='dana@example.test'")
    assert user["role"] == "customer_owner" and user["approved"] == 1 and user["account_status"] == "active"
    assert _one(db_path, "SELECT 1 AS ok FROM identity_grants WHERE user_id=? AND role='customer_owner' AND scope_type='customer' AND scope_id=?",
                (user["id"], user["customer_id"]))
    assert _signed_in(_login(client, "dana@example.test", customer_only=True))
    assert client.get("/subscription-portal").status_code == 200


def test_a_wrong_or_expired_link_creates_nothing(site, db_path):
    client, _, mail = site
    _signup(client, "late@example.test")
    assert client.get("/customer/verify-email?token=not-a-real-token").status_code == 400
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE direct_signups SET expires_at='2000-01-01T00:00:00'")
    conn.commit()
    conn.close()
    assert client.get(f"/customer/verify-email?token={_token(mail, 'late@example.test')}").status_code == 400
    assert _count(db_path, "SELECT COUNT(*) FROM customers") == 0


def test_weak_or_mismatched_passwords_are_refused(site):
    client, _, mail = site
    assert _signup(client, "weak@example.test", password="short").status_code == 400
    mismatch = client.post("/api/customer/direct-signup", json={"name": "W", "email": "w2@example.test",
                                                                "password": PASSWORD, "confirm_password": PASSWORD + "x"})
    assert mismatch.status_code == 400 and mail == []


# ------------------------------------------------------------------ 2. no attribution

def test_a_direct_customer_is_a_house_customer_with_no_partner_or_salesperson(site, db_path):
    client, _, mail = site
    email = _direct_owner(client, mail)
    customer = _one(db_path, "SELECT * FROM customers WHERE email=?", (email,))
    assert customer["partner_id"] == "anyaicam-primary" and customer["onboarding_channel"] == "direct"
    import sales_commissions
    assert sales_commissions.attribution_for(customer["id"]) is None
    paid = {"id": "evt_inv", "type": "invoice.paid", "data": {"object": {
        "id": "in_1", "object": "invoice", "customer": "cus_x", "subscription": "sub_1", "amount_paid": 1499, "tax": 0,
        "currency": "usd", "charge": "ch_1", "payment_intent": "pi_1",
        "subscription_details": {"metadata": {"anyaicam_customer_id": customer["id"]}},
        "lines": {"data": [{"price": {"id": "price_local_8"}, "amount": 1499}]}}}}
    sales_commissions.sync_commissions_from_stripe_event(paid)
    assert sales_commissions.ledger_for() == []  # nobody earns commission on a direct customer
    # Partner-assisted customers keep NULL: the channel is explicit, not inferred.
    assert _count(db_path, "SELECT COUNT(*) FROM customers WHERE onboarding_channel IS NOT NULL AND onboarding_channel<>'direct'") == 0


# ------------------------------------------------------------------ 3-6. purchase, entitlement, download

def test_direct_owner_buys_local_gets_entitlement_and_downloads_the_installer(site, db_path, storage, package):
    client, captured, mail = site
    email = _direct_owner(client, mail)
    customer_id = _one(db_path, "SELECT id FROM customers WHERE email=?", (email,))["id"]
    _publish(package)
    # Before buying: no download.
    assert client.get("/api/customer/downloads/vms-installer").status_code == 403
    plan = client.post("/api/customer/camera-slots/checkout", json={"plan_type": "local", "tier_label": "1-8"})
    assert plan.status_code == 200, plan.text
    assert captured[-1]["metadata[anyaicam_customer_id]"] == customer_id and captured[-1]["customer_email"] == email
    licence = client.post("/api/customer/vms-license/checkout", json={"capacity": 8})
    assert licence.status_code == 200, licence.text
    assert captured[-1]["metadata[anyaicam_customer_id]"] == customer_id
    assert _webhook(client, _completed("evt_plan", "price_local_8", customer_id, email)).status_code == 200
    assert _webhook(client, _completed("evt_lic", "price_vms_8", customer_id, email, mode="payment")).status_code == 200
    import customer_entitlements as ce
    assert ce.total_camera_slots(customer_id) == 8 and ce.vms_license_capacity(customer_id) == 8
    portal_page = client.get("/subscription-portal").text
    assert "vms-installer" in portal_page
    download = client.get("/api/customer/downloads/vms-installer")
    assert download.status_code == 200 and download.content == package.read_bytes()


def test_an_unlicensed_direct_owner_cannot_download(site, db_path, storage, package):
    client, _, mail = site
    _direct_owner(client, mail, "nolicence@example.test")
    _publish(package)
    refused = client.get("/api/customer/downloads/vms-installer")
    assert refused.status_code == 403 and "license is required" in refused.json()["detail"]


# ------------------------------------------------------------------ 7. partner-assisted path unchanged

def test_partner_assisted_registration_still_needs_approval(site, db_path):
    client, _, mail = site
    client.cookies.clear()
    form = client.post("/customer-register", data={"display_name": "Pat Partner", "email": "pat@example.test", "password": "partner-path-1"})
    assert form.status_code in (200, 303)
    request = _one(db_path, "SELECT status,partner_id FROM customer_registration_requests WHERE email='pat@example.test'")
    assert request and request["status"] == "pending"
    assert _count(db_path, "SELECT COUNT(*) FROM customers WHERE email='pat@example.test'") == 0
    assert "Request secure access" in client.get("/customer-register").text
    # And that pending request blocks a parallel direct identity for the same email.
    _signup(client, "pat@example.test")
    assert not LINK.search(mail[-1]["text"]) and _count(db_path, "SELECT COUNT(*) FROM direct_signups") == 0


# ------------------------------------------------------------------ 8. no partner/admin access

def test_a_direct_customer_cannot_enter_partner_or_admin_portals(site, db_path):
    client, _, mail = site
    email = _direct_owner(client, mail)
    assert client.get("/api/partner/customers").status_code in (401, 403)
    for page in ("/admin-portal", "/admin-customers"):
        response = client.get(page)
        # Either refused outright or the existing "Your current role does not include" page.
        assert response.status_code in (303, 401, 403) or "Your current role does not include" in response.text, page
        assert response.status_code != 303 or "admin" not in response.headers["location"]
    assert client.post("/api/admin/sales-attribution", json={"customer_id": "x", "salesperson": email}).status_code in (401, 403)
    assert _login(client, email, partner_only=True).status_code == 403


# ------------------------------------------------------------------ 9. duplicates

def test_duplicate_signups_and_link_replays_never_create_a_second_identity(site, db_path):
    client, _, mail = site
    _signup(client, "dup@example.test")
    first = _token(mail, "dup@example.test")
    _signup(client, "DUP@example.test")
    second = _token(mail, "dup@example.test")
    assert first != second and _count(db_path, "SELECT COUNT(*) FROM direct_signups WHERE verified_at IS NULL") == 1
    assert client.get(f"/customer/verify-email?token={first}").status_code == 400  # replaced
    assert client.get(f"/customer/verify-email?token={second}").status_code == 200
    assert client.get(f"/customer/verify-email?token={second}").status_code == 400  # single use
    assert _count(db_path, "SELECT COUNT(*) FROM customers WHERE email='dup@example.test'") == 1
    assert _count(db_path, "SELECT COUNT(*) FROM partner_users WHERE email='dup@example.test'") == 1
    # Signing up again with a live account sends a "you already have one" note, no link.
    again = _signup(client, "dup@example.test")
    assert again.status_code == 200 and again.json()["message"] == _signup(client, "brand-new@example.test").json()["message"]
    assert "already has one" in [m for m in mail if m["to"] == "dup@example.test"][-1]["text"]
    assert _count(db_path, "SELECT COUNT(*) FROM direct_signups WHERE email='dup@example.test' AND verified_at IS NULL") == 0


def test_an_email_claimed_elsewhere_before_verification_is_not_attached(site, db_path):
    """A partner onboards the same address while the direct link is unused:
    the link must not create a second customer or attach to theirs."""
    client, _, mail = site
    _signup(client, "race@example.test")
    raw = _token(mail, "race@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-9','P9','2026-01-01')")
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-9','partner-9','Theirs','race@example.test','active','2026-01-01')")
    conn.commit()
    conn.close()
    page = client.get(f"/customer/verify-email?token={raw}")
    assert page.status_code == 400 and "already associated" in page.text
    assert _count(db_path, "SELECT COUNT(*) FROM customers WHERE email='race@example.test'") == 1
    assert _count(db_path, "SELECT COUNT(*) FROM partner_users WHERE email='race@example.test'") == 0


def test_a_replayed_purchase_webhook_grants_the_entitlement_once_and_only_to_its_buyer(site, db_path):
    client, _, mail = site
    a = _direct_owner(client, mail, "a@example.test")
    b = _direct_owner(client, mail, "b@example.test")
    a_id = _one(db_path, "SELECT id FROM customers WHERE email=?", (a,))["id"]
    b_id = _one(db_path, "SELECT id FROM customers WHERE email=?", (b,))["id"]
    assert a_id != b_id
    event = _completed("evt_once", "price_local_8", a_id, a)
    assert _webhook(client, event).status_code == 200
    assert _webhook(client, event).status_code == 200
    import customer_entitlements as ce
    assert ce.total_camera_slots(a_id) == 8 and ce.total_camera_slots(b_id) == 0
    assert _count(db_path, "SELECT COUNT(*) FROM customer_entitlements WHERE customer_id=?", (a_id,)) == 1


# ------------------------------------------------------------------ email delivery

def test_the_real_email_service_accepts_the_signup_email_type():
    import email_service
    assert "email_verification" in email_service.EMAIL_TYPES


def test_a_mail_outage_is_reported_plainly_and_creates_nothing(site, db_path):
    client, _, mail = site
    down = _signup(client, "smtp-down@example.test")
    assert down.status_code == 503 and "couldn't send" in down.json()["detail"]
    assert _count(db_path, "SELECT COUNT(*) FROM customers") == 0
