"""A re-sent email carries the same Message-ID (Codex 1.2.7 finding #4).

Delivery is at-least-once: after a crash between "the provider accepted it" and "it was recorded", the claim's
lease expires and the email is sent again. Neither SMTP nor SES SendEmail nor Twilio Messages has an
idempotency key, so the best the product can do is make every copy of one logical email carry one stable
Message-ID, which de-duplicating clients and gateways can collapse.
"""
from types import SimpleNamespace

import pytest

import email_service

SMTP_SETTINGS = SimpleNamespace(email_backend="smtp", smtp_host="smtp.example.test", smtp_port=587,
                                smtp_username="", smtp_password="", email_from="AnyAiCam <alerts@anyaicam.com>")


@pytest.fixture()
def smtp(monkeypatch):
    sent = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(email_service, "settings", SMTP_SETTINGS)
    monkeypatch.setattr(email_service.smtplib, "SMTP", _Client)
    return sent


def test_the_stable_message_id_is_deterministic_and_well_formed(monkeypatch):
    monkeypatch.setattr(email_service, "settings", SMTP_SETTINGS)
    first = email_service.stable_message_id("notification", "n-1", "email")
    assert first == email_service.stable_message_id("notification", "n-1", "email")
    assert first != email_service.stable_message_id("notification", "n-2", "email")
    assert first.startswith("<anyaicam-") and first.endswith("@anyaicam.com>") and " " not in first


def test_both_sends_of_one_alert_carry_the_identical_message_id_header(smtp, monkeypatch):
    import notification_service
    monkeypatch.setattr(notification_service, "get_email_service", lambda: email_service.SMTPEmail())
    monkeypatch.setattr(notification_service, "_rich_alert_content", lambda notification: None)
    alert = {"id": "n-crash-1", "title": "Person detected", "message": "Front Door"}
    for _ in range(2):  # the original send and the at-least-once re-send after a crash
        assert notification_service.EmailChannel().send(alert, "owner@example.test")["status"] == "sent"
    ids = [message["Message-ID"] for message in smtp]
    assert len(ids) == 2 and ids[0] == ids[1] == email_service.stable_message_id("notification", "n-crash-1", "email")
    other = notification_service.EmailChannel().send({"id": "n-other", "title": "t", "message": "m"}, "owner@example.test")
    assert other["status"] == "sent" and smtp[-1]["Message-ID"] != ids[0]


def test_an_invalid_message_id_is_never_written_into_the_header(smtp):
    email_service.SMTPEmail().send("appliance_alert", "owner@example.test", "s", "t",
                                   metadata={"message_id": "<bad id\r\nBcc: x@evil.test>"})
    assert smtp[-1]["Message-ID"] != "<bad id\r\nBcc: x@evil.test>" and smtp[-1]["Bcc"] is None


def test_a_retried_outbox_email_reuses_its_message_id(tmp_path, monkeypatch):
    from database_backend import override_target
    from partner_db import initialize_database
    import purchase_notifications as pn
    seen, outcomes = [], iter([{"status": "failed", "error": "535"}, {"status": "sent"}])

    class _Backend:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            seen.append((metadata or {}).get("message_id"))
            return next(outcomes)
    monkeypatch.setattr(pn, "get_email_service", lambda: _Backend())
    with override_target(sqlite_path=str(tmp_path / "outbox.db")):
        initialize_database()
        for expected in ("failed", "sent"):
            assert pn._send_once(event_id="evt-msgid", notification_type="hardware_order_confirmation", customer_id=None,
                                 recipient_email="buyer@example.test", subject="s", text="t", test_mode=False)["status"] == expected
    assert len(seen) == 2 and seen[0] == seen[1] and seen[0].startswith("<anyaicam-")
