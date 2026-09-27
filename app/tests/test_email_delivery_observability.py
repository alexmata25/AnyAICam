"""Email delivery observability (2026-09-27).

Staging sent no email for a week -- every send was rejected by the SMTP
server with 535 "Username and Password not accepted" -- yet the product
recorded 4,522 failed notification emails and 4 failed password-reset
emails with no reason at all, and readiness kept reporting
password_reset_email_ready=True (it only checks configuration). Now:
- every email record keeps the provider's reason (never a credential);
- readiness carries the most recent real delivery outcome;
- a configured SMTP backend whose latest send failed raises a warning.
No real email is sent.
"""
import json
import sqlite3

import pytest

import cloud_features
import cloud_config
import main
import notification_service
from cloud_config import Settings
from database_backend import override_target
from email_service import email_error_fields
from test_customer_password_reset_hardening import _cloud_settings, _rows, _seed_user, client, db_path  # noqa: F401 (fixtures)

SMTP_535 = "(535, b'5.7.8 Username and Password not accepted. For more information, go to ...')"


class _FailingEmail:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append(message_type)
        return {"type": message_type, "to": to, "status": "failed", "error": SMTP_535, "created_at": "2026-09-27T12:00:00"}


def test_error_fields_only_for_failed_sends():
    assert email_error_fields({"status": "failed", "error": SMTP_535}) == {"error": SMTP_535}
    assert email_error_fields({"status": "sent"}) == {}
    assert email_error_fields({"status": "preview", "error": "ignored"}) == {}
    assert email_error_fields(None) == {}
    assert len(email_error_fields({"status": "failed", "error": "x" * 1000})["error"]) == 300


def test_notification_email_keeps_the_provider_reason(monkeypatch):
    monkeypatch.setattr(notification_service, "get_email_service", lambda: _FailingEmail())
    result = notification_service.EmailChannel().send({"id": "n1", "title": "Person detected"}, "owner@example.test")
    assert result["status"] == "failed" and result["error"] == SMTP_535


def test_a_failed_password_reset_email_records_why(db_path, client, monkeypatch):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: _FailingEmail())
    monkeypatch.setattr(cloud_features, "settings", _cloud_settings())
    assert client.post("/api/password-reset/request", json={"email": "owner@example.test"}).status_code == 200
    row = _rows(db_path, "SELECT status, metadata_json FROM email_messages")[0]
    metadata = json.loads(row["metadata_json"])
    assert row["status"] == "failed" and metadata["error"] == SMTP_535 and metadata["expires_minutes"] == 60
    assert "old-password" not in row["metadata_json"]


def _deliveries(path, rows):
    conn = sqlite3.connect(path)
    for table, values in rows:
        if table == "account":
            conn.execute("INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)", values)
        else:
            conn.execute("INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at) VALUES(?,?,?,?,?,?,?,?,?)", values)
    conn.commit()
    conn.close()


def test_readiness_reports_the_latest_real_delivery_and_warns_when_it_failed(db_path, client, monkeypatch):
    monkeypatch.setattr(cloud_config, "settings", Settings(email_backend="smtp", smtp_host="smtp.example", email_from="no-reply@anyaicam.example"))
    with override_target(sqlite_path=db_path):
        assert main._last_email_delivery() is None
        assert not [i for i in main.configuration_issues() if i["key"] == "ANYAICAM_EMAIL_DELIVERY"]
        _deliveries(db_path, [
            ("account", ("m1", "password_reset", "a@example.test", "sent", "smtp", "{}", "2026-09-27T10:00:00")),
            ("alert", ("d1", "n1", "email", "failed", "configured_email", SMTP_535, "b@example.test", 1, "2026-09-27T11:00:00")),
        ])
        last = main._last_email_delivery()
        assert last == {"kind": "notification", "status": "failed", "at": "2026-09-27T11:00:00", "error": SMTP_535}
        warning = [i for i in main.configuration_issues() if i["key"] == "ANYAICAM_EMAIL_DELIVERY"]
        assert warning and warning[0]["severity"] == "warning" and "535" in warning[0]["message"]
        assert main.cloud_configuration_snapshot()["email_last_delivery"]["status"] == "failed"
        # A later successful send clears the warning.
        _deliveries(db_path, [("account", ("m2", "password_reset", "a@example.test", "sent", "smtp", "{}", "2026-09-27T12:00:00"))])
        assert main._last_email_delivery()["status"] == "sent"
        assert not [i for i in main.configuration_issues() if i["key"] == "ANYAICAM_EMAIL_DELIVERY"]


def test_no_email_warning_when_email_is_not_configured_for_smtp(db_path, client, monkeypatch):
    monkeypatch.setattr(cloud_config, "settings", Settings(email_backend="preview"))
    with override_target(sqlite_path=db_path):
        _deliveries(db_path, [("alert", ("d1", "n1", "email", "failed", "configured_email", "x", "b@example.test", 1, "2026-09-27T11:00:00"))])
        assert not [i for i in main.configuration_issues() if i["key"] == "ANYAICAM_EMAIL_DELIVERY"]
