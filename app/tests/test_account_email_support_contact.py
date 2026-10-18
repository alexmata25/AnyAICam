"""Account-recovery and email-confirmation emails tell a stuck customer how to get help (Green 2026-10-17, post-RC5).

The password-reset email and both email-confirmation emails (self-service signup, /customer-register) had no support
address or link. They now end with "Need help? Contact <ANYAICAM_SUPPORT_EMAIL> or visit <support page>."; the reset
link itself, its one-hour lifetime and the "ignore this email" guidance are unchanged.
"""
import email_layout
import purchase_notifications as pn
from test_direct_onboarding import _signup, site  # noqa: F401 -- fixture
from test_pricing_ff_commission import db_path, license_portal, portal  # noqa: F401 -- fixtures
from test_registration_email_verification_127 import db  # noqa: F401 -- fixture


def test_the_password_reset_email_names_the_support_contact_and_keeps_its_link():
    link = "https://app.example.test/customer-reset-password?token=abc123"
    subject, text, html = email_layout.password_reset_email(link)
    assert subject == "Reset your AnyAiCam password"
    assert link in text and "expires in one hour" in text
    assert pn.SUPPORT_EMAIL in text and pn.SUPPORT_LINK in text
    assert f"mailto:{pn.SUPPORT_EMAIL}" in html and pn.SUPPORT_LINK in html


def test_the_support_line_follows_the_configured_address(monkeypatch):
    monkeypatch.setattr(pn, "SUPPORT_EMAIL", "help@example.test")
    text, html = email_layout.support_contact()
    assert "help@example.test" in text and "mailto:help@example.test" in html


def test_the_support_line_escapes_html(monkeypatch):
    monkeypatch.setattr(pn, "SUPPORT_LINK", 'https://x.example/"><script>alert(1)</script>')
    text, html = email_layout.support_contact()
    assert "<script>" not in html and html.count("<a ") == 2


def test_the_self_service_confirmation_email_names_the_support_contact(site):
    client, _, mail = site
    assert _signup(client, "helpme@example.test").status_code == 200
    sent = [m for m in mail if m["to"] == "helpme@example.test"][-1]
    assert "/customer/verify-email?token=" in sent["text"] and pn.SUPPORT_EMAIL in sent["text"]


def test_the_account_request_confirmation_email_names_the_support_contact(db, monkeypatch):
    import customer_registration as cr
    import email_service
    sent = []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            sent.append({"type": message_type, "to": to, "text": text})
            return {"status": "sent"}
    monkeypatch.setattr(email_service, "get_email_service", lambda: _Mail())
    cr.create_pending_registration("Help Seeker", "seeker@example.test", "correct-horse-battery-staple")
    assert cr.send_registration_verification("seeker@example.test", "https://app.example.test")
    assert "/customer-register/verify-email?token=" in sent[-1]["text"] and pn.SUPPORT_EMAIL in sent[-1]["text"]
