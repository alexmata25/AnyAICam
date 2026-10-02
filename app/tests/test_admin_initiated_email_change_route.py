"""Route-level tests for the admin-initiated customer email-change
route (POST /api/partner/customers/{customer_id}/accounts/{user_id}/
change-email) -- the second and final piece the Password Recovery +
Account Controls audit found missing (see
test_admin_initiated_password_reset_route.py's own docstring for the
first): unlock and admin-initiated password reset were the only two
existing admin-facing customer-account controls; there was no way for a
partner/admin to correct a customer's login email on their behalf.

Same established pattern as its two siblings: pull the real route
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
    return tmp_path / "test_admin_initiated_email_change.db"


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


def test_email_change_updates_the_real_row_and_notifies_the_old_address(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    capturing = _CapturingEmailService()
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
        result = change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
        updated = row("SELECT email FROM partner_users WHERE id='user-a'")
    assert result["old_email"] == "user-a@example.test"
    assert result["new_email"] == "new-address@example.test"
    assert updated["email"] == "new-address@example.test"
    assert len(capturing.sent) == 1
    assert capturing.sent[0]["to"] == "user-a@example.test", "the security notice must go to the OLD address, not the new one"
    assert capturing.sent[0]["type"] == "account_email_changed"


def test_email_change_never_touches_the_password(monkeypatch, db_path):
    from partner_db import verify_password
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
        account = row("SELECT password_hash FROM partner_users WHERE id='user-a'")
    assert verify_password("user-a-password-123", account["password_hash"]) is True


def test_email_change_rejects_an_invalid_email_with_zero_mutation(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        with pytest.raises(HTTPException) as excinfo:
            change(_fake_request(), "customer-a", "user-a", {"new_email": "not-an-email"})
        unchanged = row("SELECT email FROM partner_users WHERE id='user-a'")
    assert excinfo.value.status_code == 400
    assert unchanged["email"] == "user-a@example.test"


def test_email_change_rejects_the_same_email_as_a_no_op(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        with pytest.raises(HTTPException) as excinfo:
            change(_fake_request(), "customer-a", "user-a", {"new_email": "USER-A@example.test"})
    assert excinfo.value.status_code == 400


def test_email_change_rejects_a_duplicate_email_already_in_use(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        with pytest.raises(HTTPException) as excinfo:
            change(_fake_request(), "customer-a", "user-a", {"new_email": "user-b@example.test"})
        unchanged = row("SELECT email FROM partner_users WHERE id='user-a'")
    assert excinfo.value.status_code == 409
    assert unchanged["email"] == "user-a@example.test"


def test_email_change_cross_tenant_denied_with_zero_mutation(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        with pytest.raises(HTTPException) as excinfo:
            change(_fake_request(), "customer-b", "user-b", {"new_email": "new-address@example.test"})
        unchanged = row("SELECT email FROM partner_users WHERE id='user-b'")
    assert excinfo.value.status_code == 404
    assert unchanged["email"] == "user-b@example.test"


def test_email_change_unauthenticated_denied_with_zero_mutation(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        def _deny(request):
            raise HTTPException(status_code=403, detail="Partner authorization required.")
        monkeypatch.setattr(cloud_features, "require_partner_access", _deny)
        with pytest.raises(HTTPException) as excinfo:
            change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
        unchanged = row("SELECT email FROM partner_users WHERE id='user-a'")
    assert excinfo.value.status_code == 403
    assert unchanged["email"] == "user-a@example.test"


def test_email_change_role_without_customer_edit_permission_denied(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: {"role": "customer_viewer", "partner_id": "partner-a", "customer_id": "customer-a", "email": "viewer-a@example.test"})
        with pytest.raises(HTTPException) as excinfo:
            change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
    assert excinfo.value.status_code == 403


def test_email_change_writes_an_audit_record_with_both_old_and_new_email(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
        entry = row("SELECT * FROM audit_logs WHERE action='customer_account.email_changed' AND entity_id='user-a'")
    assert entry is not None
    assert entry["actor_email"] == "tech-a@example.test"
    assert "user-a@example.test" in entry["details_json"]
    assert "new-address@example.test" in entry["details_json"]


def test_email_change_does_not_mutate_unrelated_customer_fields(monkeypatch, db_path):
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        before = row("SELECT * FROM customers WHERE id='customer-a'")
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _CapturingEmailService())
        change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
        after = row("SELECT * FROM customers WHERE id='customer-a'")
    assert before == after


def test_email_change_survives_a_real_smtp_failure_gracefully(monkeypatch, db_path):
    """Regression guard for the exact bug the sibling password-reset
    route hit live on staging: the email UPDATE must already be
    committed and the route must still return 200 even if the security-
    notice send() itself reports a failure (email_service.py's own
    fixed SMTPEmail now returns status='failed' instead of raising, but
    this proves the route's own logic doesn't additionally assume the
    email always succeeds)."""
    change = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/change-email")
    _seed(db_path)

    class _FailingEmailService:
        def send(self, message_type, to, subject, text, html=None, metadata=None):
            return {"type": message_type, "to": to, "status": "failed", "error": "real SMTP auth failure"}

    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        monkeypatch.setattr(cloud_features, "get_email_service", lambda: _FailingEmailService())
        result = change(_fake_request(), "customer-a", "user-a", {"new_email": "new-address@example.test"})
        updated = row("SELECT email FROM partner_users WHERE id='user-a'")
    assert result["new_email"] == "new-address@example.test"
    assert updated["email"] == "new-address@example.test"
