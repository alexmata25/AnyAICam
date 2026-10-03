"""Focused regression coverage for Household invitation email delivery."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import email_service
import main
import pytest


class CapturingEmailService:
    def __init__(self, status="sent", error=None):
        self.status = status
        self.error = error
        self.messages = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.messages.append(
            {
                "type": message_type,
                "to": to,
                "subject": subject,
                "text": text,
                "metadata": metadata,
            }
        )
        if self.error:
            raise self.error
        return {"id": "test-message", "status": self.status}


@pytest.fixture
def invitation_context(tmp_path, monkeypatch):
    service = CapturingEmailService()
    monkeypatch.setattr(main, "USER_INVITES_FILE", tmp_path / "user_invites.json")
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://portal.example.test")
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "owner-1", "role": "administrator"})
    monkeypatch.setattr(main, "record_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(email_service, "get_email_service", lambda: service)
    request = SimpleNamespace(headers={}, cookies={}, base_url="https://portal.example.test/")
    return SimpleNamespace(tmp_path=tmp_path, service=service, request=request)


def _create(context, email="guest@example.test"):
    return main.create_user_invite(
        context.request,
        main.UserInviteCreateModel(
            email=email,
            display_name="Household Guest",
            role="view-only",
            all_cameras=True,
        ),
    )


def _stored_invites(context):
    path = context.tmp_path / "user_invites.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _accept(token):
    return main.accept_user_invite(
        main.UserInviteAcceptModel(
            token=token,
            display_name="Household Guest",
            password="long-test-password-123",
        )
    )


def test_create_persists_pending_invitation_and_reports_email_accepted(invitation_context):
    context = invitation_context

    result = _create(context)

    stored = _stored_invites(context)[0]
    assert result["status"] == "complete"
    assert result["invitation_status"] == "pending"
    assert result["email_status"] == "sent"
    assert stored["status"] == "pending"
    assert stored["email_status"] == "sent"
    assert context.service.messages[0]["type"] == "invitation"
    assert context.service.messages[0]["to"] == "guest@example.test"
    assert f"token={stored['token']}" in context.service.messages[0]["text"]
    assert "token" not in result["invite"]


def test_preview_backend_is_explicitly_not_delivered(tmp_path, monkeypatch):
    service_settings = SimpleNamespace(
        email_backend="preview",
        email_preview_dir=str(tmp_path / "email-preview"),
        email_from="noreply@example.test",
    )
    monkeypatch.setattr(email_service, "settings", service_settings)
    monkeypatch.setattr(main, "USER_INVITES_FILE", tmp_path / "user_invites.json")
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://portal.example.test")
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "owner-1", "role": "administrator"})
    monkeypatch.setattr(main, "record_audit", lambda *args, **kwargs: None)
    request = SimpleNamespace(headers={}, cookies={}, base_url="https://portal.example.test/")

    result = main.create_user_invite(
        request,
        main.UserInviteCreateModel(email="preview@example.test", display_name="Preview Guest", role="view-only"),
    )

    assert result["invitation_status"] == "pending"
    assert result["email_status"] == "preview"
    assert "not delivered" in result["message"]
    preview_files = list((tmp_path / "email-preview").glob("*.json"))
    assert len(preview_files) == 1


def test_missing_smtp_configuration_is_reported_as_failed_not_sent(tmp_path, monkeypatch):
    service_settings = SimpleNamespace(
        email_backend="smtp",
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        email_from="noreply@example.test",
    )
    monkeypatch.setattr(email_service, "settings", service_settings)
    monkeypatch.setattr(main, "USER_INVITES_FILE", tmp_path / "user_invites.json")
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://portal.example.test")
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "owner-1", "role": "administrator"})
    monkeypatch.setattr(main, "record_audit", lambda *args, **kwargs: None)
    request = SimpleNamespace(headers={}, cookies={}, base_url="https://portal.example.test/")

    result = main.create_user_invite(
        request,
        main.UserInviteCreateModel(email="missing@example.test", display_name="Guest", role="view-only"),
    )

    assert result["status"] == "complete"  # the invitation record was persisted
    assert result["email_status"] == "failed"
    assert "delivery failed" in result["message"]
    assert _stored_invites(SimpleNamespace(tmp_path=tmp_path))[0]["status"] == "pending"


def test_provider_failure_keeps_invitation_and_records_failed_email(invitation_context):
    context = invitation_context
    context.service.error = OSError("provider unavailable")

    result = _create(context)

    stored = _stored_invites(context)[0]
    assert result["invitation_status"] == "pending"
    assert result["email_status"] == "failed"
    assert stored["email_last_attempt_status"] == "failed"
    assert "provider unavailable" not in result["message"]


def test_successful_resend_rotates_token_and_old_link_is_rejected(invitation_context, monkeypatch):
    context = invitation_context
    monkeypatch.setattr(main, "hash_password", lambda password: "test-password-hash")
    _create(context)
    invite = _stored_invites(context)[0]
    old_token = invite["token"]
    old_expiry = invite["expires_at"]

    result = main.resend_user_invite(context.request, invite["id"])

    updated = _stored_invites(context)[0]
    assert result["email_status"] == "sent"
    assert updated["token"] != old_token
    assert updated["expires_at"] != old_expiry
    assert _accept(old_token)["status"] == "error"
    assert _accept(updated["token"])["status"] == "complete"
    assert _accept(updated["token"])["status"] == "error"


def test_failed_resend_preserves_existing_token_expiry_and_delivery_state(invitation_context, monkeypatch):
    context = invitation_context
    monkeypatch.setattr(main, "hash_password", lambda password: "test-password-hash")
    _create(context)
    original = _stored_invites(context)[0]
    context.service.error = OSError("provider unavailable")

    result = main.resend_user_invite(context.request, original["id"])

    updated = _stored_invites(context)[0]
    assert result["status"] == "complete"
    assert result["email_last_attempt_status"] == "failed"
    assert updated["token"] == original["token"]
    assert updated["expires_at"] == original["expires_at"]
    assert updated["email_status"] == "sent"
    assert updated["email_last_attempt_status"] == "failed"
    assert _accept(original["token"])["status"] == "complete"


def test_preview_only_resend_keeps_the_current_active_token(invitation_context):
    context = invitation_context
    _create(context)
    original = _stored_invites(context)[0]
    context.service.status = "preview"

    result = main.resend_user_invite(context.request, original["id"])

    updated = _stored_invites(context)[0]
    assert "not delivered" in result["message"]
    assert updated["token"] == original["token"]
    assert updated["expires_at"] == original["expires_at"]
    assert updated["email_last_attempt_status"] == "preview"


@pytest.mark.parametrize("state", ["accepted", "cancelled"])
def test_resend_rejects_accepted_and_cancelled_invitations(invitation_context, state):
    context = invitation_context
    _create(context)
    invite = _stored_invites(context)[0]
    invite["status"] = state
    main.save_user_invites([invite])

    result = main.resend_user_invite(context.request, invite["id"])

    assert result["status"] == "error"
    assert _stored_invites(context)[0]["status"] == state


def test_expired_invitation_cannot_be_accepted_or_resent(invitation_context):
    context = invitation_context
    _create(context)
    invite = _stored_invites(context)[0]
    invite["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
    main.save_user_invites([invite])

    assert _accept(invite["token"])["status"] == "error"
    assert _stored_invites(context)[0]["status"] == "expired"
    assert main.resend_user_invite(context.request, invite["id"])["status"] == "error"


def test_invite_acceptance_requires_manager_and_list_never_returns_token(invitation_context, monkeypatch):
    context = invitation_context
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "viewer-1", "role": "view-only"})

    denied = _create(context)

    assert denied["status"] == "error"
    assert _stored_invites(context) == []

    monkeypatch.setattr(main, "current_user", lambda request: {"id": "owner-1", "role": "administrator"})
    _create(context)
    listed = main.user_invites_api(context.request)
    assert listed["status"] == "complete"
    assert "token" not in listed["invites"][0]
