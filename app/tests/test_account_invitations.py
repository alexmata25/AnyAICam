"""Account invitations never persist or return temporary passwords
(2026-10-02, Codex finding).

Partner-assisted onboarding, portal user invitations and approved partner
applications created accounts with a temporary password stored in
invitations.email_preview and returned by the API even after the email was
delivered; some invitations never expired. They now use hashed,
single-use, expiring links (account_invitations.py). These tests inspect DB
rows, previews and API JSON without printing any secret.
"""
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

import account_invitations
import partner_workspace
from database_backend import override_target
from partner_db import connection, initialize_database, verify_password
from test_customer_onboarding_identity_grants import _fake_request, _partner_owner_identity, _route, _seed_customer
from test_partner_portal_pass import ALPHA, client, sent_mail  # noqa: F401 -- fixtures

TOKEN = re.compile(r"/accept-invitation\?token=([A-Za-z0-9_\-]+)")


def _no_secret_anywhere(row: dict, preview: str, response_json: dict, raw: str):
    assert row["temporary_password_hash"] is None
    assert row["token_hash"] and raw not in str(dict(row)), "the raw link token is never stored"
    assert row["expires_at"], "every invitation expires"
    assert "Temporary password" not in preview and raw not in preview
    assert "temporary_password" not in response_json


def _invitation(email):
    with connection() as db:
        return dict(db.execute("SELECT * FROM invitations WHERE email=?", (email,)).fetchone())


# ------------------------------------------------------------------ portal user invitations

def test_an_emailed_invitation_returns_no_secret_and_stores_none(client, sent_mail):
    response = client.post("/api/partner/users/invite", json={"email": "tech@example.test", "role": "technician"}, cookies=ALPHA)
    body = response.json()
    assert response.status_code == 200 and body["email_status"] == "sent"
    assert "invitation_link" not in body  # delivered by email: nothing to hand over
    raw = TOKEN.search(sent_mail[0][2]).group(1)
    row = _invitation("tech@example.test")
    _no_secret_anywhere(row, row["email_preview"], body, raw)
    assert row["token_hash"] == account_invitations.token_hash(raw)
    with connection() as db:
        user = db.execute("SELECT password_hash,must_change_password FROM partner_users WHERE email='tech@example.test'").fetchone()
    assert not verify_password("", user["password_hash"])  # nobody knows a password for it


def test_the_link_is_single_use_and_sets_the_invitees_own_password(client, sent_mail):
    client.post("/api/partner/users/invite", json={"email": "tech2@example.test", "role": "technician"}, cookies=ALPHA)
    raw = TOKEN.search(sent_mail[0][2]).group(1)
    assert "Set your AnyAiCam password" in client.get(f"/accept-invitation?token={raw}").text
    ok = client.post("/api/invitations/accept", json={"token": raw, "password": "my-own-password-123"})
    assert ok.status_code == 200 and ok.json()["destination"] == "/partner-login"
    with connection() as db:
        user = db.execute("SELECT password_hash FROM partner_users WHERE email='tech2@example.test'").fetchone()
    assert verify_password("my-own-password-123", user["password_hash"])
    replay = client.post("/api/invitations/accept", json={"token": raw, "password": "attacker-password-999"})
    assert replay.status_code == 400
    assert "can't be used" in client.get(f"/accept-invitation?token={raw}").text
    assert _invitation("tech2@example.test")["status"] == "accepted"


def test_an_expired_link_cannot_be_used(client, sent_mail):
    client.post("/api/partner/users/invite", json={"email": "late@example.test", "role": "technician"}, cookies=ALPHA)
    raw = TOKEN.search(sent_mail[0][2]).group(1)
    with connection() as db:
        db.execute("UPDATE invitations SET expires_at=? WHERE email='late@example.test'", ((datetime.now() - timedelta(minutes=1)).isoformat(),))
    assert client.post("/api/invitations/accept", json={"token": raw, "password": "too-late-password-1"}).status_code == 400


def test_when_email_fails_the_link_is_returned_once_for_a_manual_handoff(client, monkeypatch):
    class Failing:
        def send(self, *args, **kwargs):
            return {"status": "failed"}
    monkeypatch.setattr(partner_workspace, "get_email_service", lambda: Failing())
    body = client.post("/api/partner/users/invite", json={"email": "handoff@example.test", "role": "technician"}, cookies=ALPHA).json()
    raw = TOKEN.search(body["invitation_link"]).group(1)
    row = _invitation("handoff@example.test")
    _no_secret_anywhere(row, row["email_preview"], body, raw)
    assert client.post("/api/invitations/accept", json={"token": raw, "password": "handoff-password-1"}).status_code == 200


def test_household_links_and_account_links_never_cross(client, sent_mail):
    """A household link (invitee_user_id NULL) is not accepted here, and an
    account link is not accepted by the household join."""
    client.post("/api/partner/users/invite", json={"email": "tech3@example.test", "role": "technician"}, cookies=ALPHA)
    raw = TOKEN.search(sent_mail[0][2]).group(1)
    import household_users
    with connection() as db:
        assert household_users.invitation_for_token(db, raw, datetime.now()) is None


# ------------------------------------------------------------------ partner-assisted onboarding

def test_onboarding_creates_an_owner_without_any_temporary_password(tmp_path, monkeypatch):
    onboard_customer = _route("/api/partner/customers/onboard")
    db_path = tmp_path / "onboard.db"
    sent = []

    class Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None):
            sent.append(text)
            return {"status": "sent"}
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('anyaicam-primary','AnyAiCam','2026-01-01')")
        conn.commit()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_owner_identity())
        monkeypatch.setattr(partner_workspace, "get_email_service", lambda: Mail())
        import pricing_config
        config = pricing_config.load_pricing()
        config["partner"]["pricing_mode"] = "percentage"
        config["partner"]["percentage_discount"] = 20
        monkeypatch.setattr(pricing_config, "load_pricing", lambda: config)
        from test_customer_onboarding_identity_grants import ONBOARDING_PAYLOAD
        result = onboard_customer(_fake_request(), dict(ONBOARDING_PAYLOAD))
        raw = TOKEN.search(sent[0]).group(1)
        row = _invitation(ONBOARDING_PAYLOAD["email"])
        _no_secret_anywhere(row, result["invitation_email_preview"], result, raw)
        assert "invitation_link" not in result  # emailed
        assert row["role"] == "customer_owner" and row["invitee_user_id"]


# ------------------------------------------------------------------ approved partner applications

def test_an_approved_partner_application_gets_a_link_not_a_password(client, monkeypatch):
    import website_partner
    sent = []

    class Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None):
            sent.append(text)
            return {"status": "sent"}
    monkeypatch.setattr(website_partner, "get_email_service", lambda: Mail())
    from global_admin_helper import make_live_global_admin
    import partner_portal
    make_live_global_admin("platform@anyaicam.test")
    admin = {partner_portal.SESSION_COOKIE: partner_portal._token("platform@anyaicam.test", "administrator", "anyaicam-primary")}
    with connection() as db:
        db.execute("INSERT INTO partner_applications(id,company_name,contact_name,email,status,submitted_at) "
                   "VALUES('app-1','Acme Installs','Ana','ana@acme.test','pending','2026-10-02')")
    response = client.put("/api/admin/partner-applications/app-1", json={"status": "approved"}, cookies=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    raw = TOKEN.search(sent[0]).group(1)
    row = _invitation("ana@acme.test")
    _no_secret_anywhere(row, body["email_preview"], body, raw)
    assert "invitation_link" not in body
    assert client.post("/api/invitations/accept", json={"token": raw, "password": "partner-owner-pass-1"}).json()["destination"] == "/partner-login"
