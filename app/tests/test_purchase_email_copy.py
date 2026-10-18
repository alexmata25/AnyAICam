"""Purchase emails give the customer a real way to get help, in plain words (Green 2026-10-17, post-RC5 review).

Account-ready, setup-required, plan-updated and plan-cancelled said "contact our support team" (or nothing) with no
address or link, and the cancellation still used the legacy "camera-slot licensing" wording, now also sent for
My subscription (billing v2) plans. Every one now ends with the standard support footer (ANYAICAM_SUPPORT_EMAIL and
the support page), and the cancellation says how to start again.
"""
import pytest

import purchase_notifications as pn

TEMPLATES = {
    "account_ready": lambda: pn._account_ready_email("Dana", "AI Local", 4),
    "setup_required": lambda: pn._setup_required_email("AI Local", 4),
    "plan_updated": lambda: pn._plan_updated_email("Dana", "AI Local", 6),
    "plan_cancelled": lambda: pn._plan_cancelled_email("Dana", "AI Local"),
}


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_every_purchase_email_names_a_real_support_contact(kind):
    subject, text, html = TEMPLATES[kind]()
    assert pn.SUPPORT_EMAIL in text and pn.SUPPORT_LINK in text
    assert f"mailto:{pn.SUPPORT_EMAIL}" in html and pn.SUPPORT_LINK in html
    assert "our support team" not in text + html


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_no_legacy_camera_slot_wording_reaches_customers(kind):
    subject, text, html = TEMPLATES[kind]()
    assert "camera-slot" not in (subject + text + html).lower()


def test_the_cancellation_says_how_to_start_again():
    subject, text, html = pn._plan_cancelled_email("Dana", "AI Local")
    assert subject == "Your AnyAiCam subscription has been cancelled"
    assert "AI Local subscription has been cancelled" in text
    assert "/subscription-portal" in text and 'href="' in html and "/subscription-portal" in html


def test_the_setup_email_sends_guest_buyers_to_self_service_signup_and_exposes_nothing():
    """Owner 2026-10-17: guest buyers create their own account at /customer-signup; the purchase is attached only after
    they confirm the email (direct_onboarding). The link carries no email address, token or account detail."""
    subject, text, html = pn._setup_required_email("AI Local", 4)
    assert pn._portal_url("/customer-signup") in text and f'href="{pn._portal_url("/customer-signup")}"' in html
    assert "/customer-register" not in text + html
    assert "?" not in pn._portal_url("/customer-signup")  # no query string: nothing about the buyer in the link
    assert "@" not in text.replace(pn.SUPPORT_EMAIL, "")
    assert "confirm" in text
