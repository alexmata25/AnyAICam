"""Route-level tests for the admin-initiated customer password reset
(POST /api/partner/customers/{customer_id}/accounts/{user_id}/reset-
password) -- the one genuinely missing piece the Password Recovery +
Account Controls audit found: unlock_customer_account() was the only
existing admin-facing customer-account control; there was no way for a
partner/admin to start a password reset on a customer's behalf.

Same established pattern as this suite's sibling,
test_account_recovery_route_tenant_scope.py: pull the real route
function off main.app.routes and call it directly, monkeypatching
require_partner_access/get_email_service on the module they were
imported into (cloud_features)."""
from datetime import datetime

import pytest
from fastapi import HTTPException

import cloud_features
import main
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash, row


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append({"type": message_type, "to": to, "subject": subject, "text": text, "metadata": metadata})
        return {"id": "test-message-id", "status": "preview"}


def _route(path, method="POST"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request():
    from types import SimpleNamespace
    return SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"), url=SimpleNamespace(scheme="https", netloc="portal.example.test"))


def _technician_identity(email="tech-a@example.test", partner_id="partner-a"):
    return {"role": "technician", "partner_id": partner_id, "email": email}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_admin_initiated_password_reset.db"


def _seed(db_path):
    now = datetime.now().isoformat()
    with override_target(sqlite_path=db_path):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','Partner A','approved','real',?)", (now,))
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-b','Partner B','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-a','partner-a','Customer A','customer-a@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-b','partner-b','Customer B','customer-b@example.test','active','real',?)", (now,))
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES('user-a','partner-a','user-a@example.test','User A','customer_owner',?,1,'customer-a',?,'active')",
                (password_hash("user-a-password-123"), now),
            )
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES('user-b','partner-b','user-b@example.test','User B','customer_owner',?,1,'customer-b',?,'active')",
                (password_hash("user-b-password-456"), now),
            )


def test_admin_initiated_reset_same_tenant_creates_a_real_token_and_sends_email(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    capturing = _CapturingEmailService()
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
        result = reset(_fake_request(), "customer-a", "user-a")
        token_row = row("SELECT * FROM password_reset_tokens WHERE user_id='user-a' AND used_at IS NULL")
    assert "message" in result
    assert token_row is not None
    assert len(capturing.sent) == 1
    assert capturing.sent[0]["to"] == "user-a@example.test"
    assert capturing.sent[0]["type"] == "password_reset"


def test_admin_initiated_reset_never_changes_the_password_itself(monkeypatch, db_path):
    """Starting a reset must not BE a reset -- the customer's current
    password must still work until they actually complete the link."""
    from partner_db import verify_password
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        reset(_fake_request(), "customer-a", "user-a")
        account = row("SELECT password_hash FROM partner_users WHERE id='user-a'")
    assert verify_password("user-a-password-123", account["password_hash"]) is True


def test_admin_initiated_reset_token_is_a_real_usable_reset_token(monkeypatch, db_path):
    """The admin-initiated token isn't a parallel mechanism -- it must
    complete through the exact same consume_password_reset() the self-
    service /forgot-password flow already uses."""
    from cloud_security import consume_password_reset
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    captured_link = {}

    class _CapturingLink:
        def send(self, message_type, to, subject, text, html=None, metadata=None):
            captured_link["token"] = text.split("token=")[1].strip()
            return {"id": "test-message-id", "status": "preview"}

    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingLink())
        reset(_fake_request(), "customer-a", "user-a")
        role = consume_password_reset(captured_link["token"], "brand-new-password-789")
    assert role == "customer_owner"


def test_admin_initiated_reset_cross_tenant_denied_with_zero_mutation(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        with pytest.raises(HTTPException) as excinfo:
            reset(_fake_request(), "customer-b", "user-b")
        token_row = row("SELECT * FROM password_reset_tokens WHERE user_id='user-b'")
    assert excinfo.value.status_code == 404
    assert token_row is None, "a denied cross-tenant reset must never create a real token for the foreign account"


def test_admin_initiated_reset_cross_tenant_denial_matches_unknown_customer_404(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        with pytest.raises(HTTPException) as foreign:
            reset(_fake_request(), "customer-b", "user-b")
        with pytest.raises(HTTPException) as unknown:
            reset(_fake_request(), "customer-does-not-exist", "user-nope")
    assert (foreign.value.status_code, foreign.value.detail) == (unknown.value.status_code, unknown.value.detail)


def test_admin_initiated_reset_unauthenticated_denied_with_zero_mutation(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        def _deny(request):
            raise HTTPException(status_code=403, detail="Partner authorization required.")
        monkeypatch.setattr(cloud_features, "require_partner_access", _deny)
        with pytest.raises(HTTPException) as excinfo:
            reset(_fake_request(), "customer-a", "user-a")
        token_row = row("SELECT * FROM password_reset_tokens WHERE user_id='user-a'")
    assert excinfo.value.status_code == 403
    assert token_row is None


def test_admin_initiated_reset_role_without_customer_edit_permission_denied(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: {"role": "customer_viewer", "partner_id": "partner-a", "customer_id": "customer-a", "email": "viewer-a@example.test"})
        with pytest.raises(HTTPException) as excinfo:
            reset(_fake_request(), "customer-a", "user-a")
        token_row = row("SELECT * FROM password_reset_tokens WHERE user_id='user-a'")
    assert excinfo.value.status_code == 403
    assert token_row is None


def test_admin_initiated_reset_writes_an_audit_record_without_password_or_token(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        reset(_fake_request(), "customer-a", "user-a")
        entry = row("SELECT * FROM audit_logs WHERE action='customer_account.password_reset_initiated' AND entity_id='user-a'")
    assert entry is not None
    assert entry["actor_email"] == "tech-a@example.test"
    assert "password" not in entry["details_json"].lower()
    assert "token" not in entry["details_json"].lower()


def test_admin_initiated_reset_does_not_mutate_unrelated_customer_fields(monkeypatch, db_path):
    reset = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        before = row("SELECT * FROM customers WHERE id='customer-a'")
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        reset(_fake_request(), "customer-a", "user-a")
        after = row("SELECT * FROM customers WHERE id='customer-a'")
    assert before == after
