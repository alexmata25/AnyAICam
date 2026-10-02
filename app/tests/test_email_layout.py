"""Shared branded email frame and the password reset email (2026-09-29)."""
import email_layout


def test_reset_email_is_branded_with_a_button_and_plain_text_link():
    subject, text, html = email_layout.password_reset_email("https://portal.example/customer-reset-password?token=abc123")
    assert subject == "Reset your AnyAiCam password"
    assert "https://portal.example/customer-reset-password?token=abc123" in text and "expires in one hour" in text
    assert ">AnyAiCam</div>" in html and "Choose a new password</a>" in html
    assert "ignore this email" in html and "ignore this email" in text


def test_admin_initiated_reset_says_so():
    _, text, _ = email_layout.password_reset_email("https://p/x?token=t", initiated_by_admin=True)
    assert text.startswith("An AnyAiCam administrator started a password reset")


def test_link_and_preheader_are_escaped():
    _, _, html = email_layout.password_reset_email('https://p/r?token="><script>')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>" not in email_layout.wrap("x", preheader="<b>hi</b>")
