"""Regression coverage for email_service.SMTPEmail.send()'s real-failure
handling (2026-09-17). Found via real staging verification, not a unit
test: with the real (already-known, already-parked) bad Gmail app-
password on staging, a live call to POST /api/partner/customers/{id}/
accounts/{id}/reset-password crashed with an unhandled 500 --
smtplib.SMTPAuthenticationError propagated straight out of send()
through the route and FastAPI's own handler. Every caller of send()
(both password-reset routes, quote delivery, invitations, appliance
alerts, purchase notifications) assumed a failure either can't happen or
is the caller's problem; none of them actually caught anything. Fixed by
catching the real failure inside send() itself and returning a degraded
status (matching the status='failed' vocabulary notification_retry_worker
already expects and retries on) instead of raising -- protects every
caller at once, not just the one route that happened to surface it."""

import dataclasses
import smtplib

import pytest

import email_service


@pytest.fixture(autouse=True)
def _smtp_settings(monkeypatch):
    # Settings is a frozen dataclass -- replace the module-level instance
    # email_service.py itself reads, rather than mutating fields in place.
    smtp_settings = dataclasses.replace(
        email_service.settings,
        email_backend="smtp", smtp_host="smtp.example.test", smtp_port=587,
        smtp_username="sender@example.test", smtp_password="wrong-password",
        email_from="sender@example.test",
    )
    monkeypatch.setattr(email_service, "settings", smtp_settings)


class _RaisingSMTPClient:
    """Stands in for smtplib.SMTP's context-manager protocol, raising on
    login() -- the exact real failure shape (535 BadCredentials) hit
    live against staging."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted.")

    def send_message(self, message):
        raise AssertionError("send_message() must never be reached when login() fails")


def test_real_smtp_auth_failure_returns_a_failed_status_instead_of_raising(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", _RaisingSMTPClient)
    result = email_service.SMTPEmail().send("password_reset", "customer@example.test", "Reset your AnyAiCam password", "body")
    assert result["status"] == "failed"
    assert result["to"] == "customer@example.test"
    assert result["type"] == "password_reset"
    assert "error" in result


class _ConnectionRefusedClient(_RaisingSMTPClient):
    def __init__(self, *args, **kwargs):
        raise ConnectionRefusedError("Connection refused")


def test_real_connection_failure_also_returns_a_failed_status_not_a_raise(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", _ConnectionRefusedClient)
    result = email_service.SMTPEmail().send("password_reset", "customer@example.test", "subject", "body")
    assert result["status"] == "failed"


class _SucceedingSMTPClient(_RaisingSMTPClient):
    def login(self, username, password):
        self.logged_in = True

    def send_message(self, message):
        self.sent = message


def test_successful_send_is_unaffected_and_still_reports_sent(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", _SucceedingSMTPClient)
    result = email_service.SMTPEmail().send("password_reset", "customer@example.test", "subject", "body")
    assert result["status"] == "sent"
    assert "error" not in result
