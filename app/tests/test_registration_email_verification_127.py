"""Owner decision 2026-10-17: a registration request must prove email ownership before approval.

Approval runs resolve_pending_links_for_customer for the request's email, attaching any Stripe purchase made earlier
under that address. Without verification, anyone could register with another person's email and, once approved,
receive that person's pre-registration purchase. Requirements covered (owner's list):
1 unverified approval is blocked; 2 pending purchases/checkout state are preserved; 3 verify -> approve works without a
new checkout; 4 a reopened request cannot bypass verification; 5 verification is for the exact email being approved;
6 no account/purchase information leaks; 7 the verified workflow still works.
Green's review (F1-F5): the inbox owner chooses the password when confirming, a GET never confirms, the link base is
the configured URL only, reviewer resends are throttled, the token page sends no referrer and is not cached.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import customer_entitlements as ce
import customer_registration as cr
from database_backend import override_target
from partner_db import authenticate_detailed, initialize_database

MASTER = {"master": True, "email": "master@example.test", "partner_id": None}
EMAIL = "buyer@example.test"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "email_verify.db"
    with override_target(sqlite_path=path):
        initialize_database()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO partners(id,name,approval_status,source,created_at) "
                     "VALUES('partner-a','Partner A','approved','real','2026-01-01')")
        conn.commit()
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()


@pytest.fixture()
def client(db):
    app = FastAPI()
    cr.register_customer_registration_routes(app, lambda *a, **k: "", lambda request: {"email": "master@example.test"},
                                             lambda user: True)
    return TestClient(app)


def _request(conn, email=EMAIL):
    return conn.execute("SELECT * FROM customer_registration_requests WHERE email=?", (email,)).fetchone()


def _link_status(conn, email=EMAIL):
    row = conn.execute("SELECT status FROM pending_customer_links WHERE normalized_email=?", (email,)).fetchone()
    return row["status"] if row else None


def _paid_before_registration(email=EMAIL):
    ce.create_pending_link(email=email, product="professional", camera_slot_quantity=10,
                           raw_event={"id": "evt_paid_before_registration"})


def _confirm(email=EMAIL, password=PASSWORD):
    return cr.verify_registration_email(cr.issue_registration_verification(email, force=True), password)


# 1 + 2 ------------------------------------------------------------------------------------------------------------
def test_unverified_request_cannot_be_approved_and_the_pending_purchase_stays_pending(db):
    _paid_before_registration()
    cr.create_pending_registration("Impostor", EMAIL, PASSWORD)
    item = _request(db)
    with pytest.raises(HTTPException) as refused:
        cr.approve_registration(item["id"], "partner-a", MASTER)
    assert refused.value.status_code == 409 and "not been confirmed" in refused.value.detail
    assert _request(db)["status"] == "pending"
    assert db.execute("SELECT COUNT(*) FROM customers WHERE lower(email)=?", (EMAIL,)).fetchone()[0] == 0
    assert _link_status(db) == "pending"  # the purchase is untouched, waiting for a verified owner


# 3 + 7 ------------------------------------------------------------------------------------------------------------
def test_verify_then_approve_attaches_the_pending_purchase_without_a_new_checkout(db):
    _paid_before_registration()
    cr.create_pending_registration("Real Buyer", EMAIL, PASSWORD)
    raw = cr.issue_registration_verification(EMAIL)
    assert raw and cr.verify_registration_email(raw, PASSWORD)
    result = cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    assert result["status"] == "complete"
    assert ce.total_camera_slots(result["customer_id"]) == 10
    assert _link_status(db) == "resolved"
    user, reason = authenticate_detailed(EMAIL, PASSWORD)
    assert reason == "ok" and user


def test_a_confirmation_link_works_once_and_expires(db):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    now = datetime.now()
    raw = cr.issue_registration_verification(EMAIL, now=now)
    assert cr.verify_registration_email(raw, PASSWORD, now=now + timedelta(hours=cr.VERIFY_TTL_HOURS, minutes=1)) is False
    assert cr.verify_registration_email(raw, PASSWORD, now=now) is True
    assert cr.verify_registration_email(raw, PASSWORD, now=now) is False  # single use
    audit = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='customer_registration.email_verified'").fetchone()[0]
    assert audit == 1


def test_a_short_password_does_not_use_up_the_link(db):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    raw = cr.issue_registration_verification(EMAIL)
    assert cr.verify_registration_email(raw, "short") is False
    assert _request(db)["email_verified_at"] is None
    assert cr.verify_registration_email(raw, PASSWORD) is True


def test_a_new_link_replaces_the_old_one_and_resends_are_rate_limited(db):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    now = datetime.now()
    first = cr.issue_registration_verification(EMAIL, now=now)
    assert cr.issue_registration_verification(EMAIL, now=now + timedelta(seconds=30)) is None  # throttled
    second = cr.issue_registration_verification(EMAIL, now=now + timedelta(seconds=30), force=True)
    assert second and second != first
    assert cr.verify_registration_email(first, PASSWORD, now=now) is False
    assert cr.verify_registration_email(second, PASSWORD, now=now) is True
    assert cr.issue_registration_verification(EMAIL, now=now, force=True) is None  # already verified


# 4 ----------------------------------------------------------------------------------------------------------------
def test_a_reopened_request_must_be_verified_again(db):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    assert _confirm()
    db.execute("UPDATE customer_registration_requests SET status='rejected',decided_at='x',decided_by='admin' WHERE email=?", (EMAIL,))
    db.commit()
    assert cr.create_pending_registration("B", EMAIL, "another-long-passphrase-1") == ("pending", True)
    item = _request(db)
    assert item["email_verified_at"] is None and item["email_verified_for"] is None and item["verification_token_hash"] is None
    with pytest.raises(HTTPException) as refused:
        cr.approve_registration(item["id"], "partner-a", MASTER)
    assert refused.value.status_code == 409


# 5 ----------------------------------------------------------------------------------------------------------------
def test_proof_must_be_for_the_exact_email_being_approved(db):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    assert _confirm()
    db.execute("UPDATE customer_registration_requests SET email_verified_for='someone-else@example.test' WHERE email=?", (EMAIL,))
    db.commit()
    with pytest.raises(HTTPException) as refused:
        cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    assert refused.value.status_code == 409


def test_one_requests_link_never_verifies_another_request(db):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    cr.create_pending_registration("B", "other@example.test", PASSWORD)
    assert cr.verify_registration_email(cr.issue_registration_verification(EMAIL), PASSWORD)
    assert _request(db, "other@example.test")["email_verified_at"] is None
    assert _request(db)["email_verified_for"] == EMAIL


# 6 / Green F1: the inbox owner's password wins ------------------------------------------------------------------------
def test_an_impostors_earlier_request_never_gets_the_buyers_account(db):
    """The impostor submits first with the buyer's email and their own password; the buyer submits later (the row
    already exists, so their submitted password is not stored) and confirms from their inbox. The password chosen
    on confirmation is the account's: the impostor's never signs in."""
    _paid_before_registration()
    cr.create_pending_registration("Impostor", EMAIL, "impostor-password-123")
    assert cr.create_pending_registration("Real Buyer", EMAIL, "buyer-password-456") == ("pending", False)
    assert _confirm(password="buyer-password-456")
    result = cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    assert result["status"] == "complete" and _link_status(db) == "resolved"
    assert authenticate_detailed(EMAIL, "impostor-password-123")[0] is None
    user, reason = authenticate_detailed(EMAIL, "buyer-password-456")
    assert reason == "ok" and user


# 6 / Green F2 + F5: GET never confirms; the page carries no referrer and is not cached --------------------------------
def test_opening_the_link_never_confirms_and_reveals_nothing(db, client):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    raw = cr.issue_registration_verification(EMAIL)
    valid = client.get("/customer-register/verify-email", params={"token": raw})
    invalid = client.get("/customer-register/verify-email", params={"token": "not-a-real-token"})
    assert valid.status_code == invalid.status_code == 200
    assert valid.text.replace(raw, "") == invalid.text.replace("not-a-real-token", "")  # no validity oracle
    assert _request(db)["email_verified_at"] is None  # a mail scanner's prefetch changes nothing
    for response in (valid, invalid):
        assert response.headers["referrer-policy"] == "no-referrer" and response.headers["cache-control"] == "no-store"
        assert 'method="post"' in response.text and EMAIL not in response.text


def test_posting_the_form_confirms_and_sets_the_password(db, client):
    cr.create_pending_registration("A", EMAIL, "submitted-password-1")
    raw = cr.issue_registration_verification(EMAIL)
    mismatch = client.post("/customer-register/verify-email",
                           data={"token": raw, "password": "chosen-password-22", "confirm_password": "different-1234"})
    assert mismatch.status_code == 422 and _request(db)["email_verified_at"] is None
    done = client.post("/customer-register/verify-email",
                       data={"token": raw, "password": "chosen-password-22", "confirm_password": "chosen-password-22"})
    assert done.status_code == 200 and "Email confirmed" in done.text
    assert done.headers["referrer-policy"] == "no-referrer"
    assert _request(db)["email_verified_for"] == EMAIL
    cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    assert authenticate_detailed(EMAIL, "submitted-password-1")[0] is None
    assert authenticate_detailed(EMAIL, "chosen-password-22")[1] == "ok"


@pytest.mark.parametrize("token", ["", "x", "not-a-real-token", "a" * 500])
def test_unknown_or_malformed_links_reveal_nothing(db, client, token):
    assert cr.verify_registration_email(token, PASSWORD) is False
    response = client.post("/customer-register/verify-email",
                           data={"token": token, "password": PASSWORD, "confirm_password": PASSWORD})
    assert response.status_code == 400 and response.text.count("<p>") == 1 and EMAIL not in response.text


def test_the_confirmation_email_mentions_no_purchase_or_account_details(db, monkeypatch):
    _paid_before_registration()
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    sent = []

    class Capture:
        def send(self, message_type, to, subject, text, **kw):
            sent.append((message_type, to, subject, text))
            return {"status": "sent"}
    import email_service
    monkeypatch.setattr(email_service, "get_email_service", lambda: Capture())
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://app.example.test/")
    assert cr.send_registration_verification(EMAIL) is True
    message_type, to, subject, text = sent[0]
    assert message_type == "email_verification" and to == EMAIL
    assert "https://app.example.test/customer-register/verify-email?token=" in text
    visible = subject + text.replace(text.split("token=", 1)[1].split()[0], "<token>")  # the random token aside
    for secret in ("professional", "10", "camera", "slot", "purchase", "Stripe"):
        assert secret.lower() not in visible.lower()


# Green F3: never a link built from the request's Host --------------------------------------------------------------
def test_no_link_is_sent_without_the_configured_public_url(db, monkeypatch):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    sent = []
    import email_service
    monkeypatch.setattr(email_service, "get_email_service", lambda: type("M", (), {"send": lambda *a, **k: sent.append(a)})())
    monkeypatch.delenv("ANYAICAM_PUBLIC_URL", raising=False)
    assert cr.send_registration_verification(EMAIL) is False and sent == []
    assert _request(db)["verification_token_hash"] is None  # no link was even issued


# Green F4: a reviewer's resend keeps the throttle --------------------------------------------------------------------
def test_a_reviewer_resend_is_throttled(db, client, monkeypatch):
    cr.create_pending_registration("A", EMAIL, PASSWORD)
    sent = []
    import email_service
    monkeypatch.setattr(email_service, "get_email_service", lambda: type("M", (), {"send": lambda *a, **k: sent.append(a)})())
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://app.example.test")
    url = f"/api/customer-registration-requests/{_request(db)['id']}/resend-verification"
    assert client.post(url).status_code == 200 and len(sent) == 1
    assert client.post(url).status_code == 429 and len(sent) == 1
    assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='customer_registration.verification_resent'").fetchone()[0] == 1


def test_no_link_is_issued_for_an_email_without_a_pending_request(db):
    assert cr.issue_registration_verification("nobody@example.test") is None
