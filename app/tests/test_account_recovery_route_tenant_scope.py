"""Route-level tenant-scope tests for the customer-account-unlock API
(POST /api/partner/customers/{customer_id}/accounts/{user_id}/unlock),
added per ANYAICAM_ACCOUNT_RECOVERY_CLAUDE_HANDOFF.md's own "Known limits
and required Claude work" list -- Codex's unit tests in
test_account_recovery.py exercise cloud_security.py's lockout/reset
primitives directly but never call the route itself, so same-tenant
success, cross-tenant denial, unauthenticated denial, password
preservation, and absence of unrelated-customer mutation were all
unverified before this file.

Same established pattern as
test_partner_administrator_tenant_scope_followup.py: pull the real route
function off main.app.routes and call it directly, monkeypatching
require_partner_access on the module it was imported into
(cloud_features, not partner_portal -- cloud_features does
`from partner_portal import ... require_partner_access`, so the name
lives in cloud_features' own namespace)."""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import cloud_features
import main
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash, row


def _route(path, method="POST"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request():
    from types import SimpleNamespace
    return SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"))


def _technician_identity(email="tech-a@example.test", partner_id="partner-a"):
    """A partner-scoped technician: holds customer.edit (per
    partner_db.ROLE_PERMISSIONS) but, per tenant_owns_partner()'s own
    contract, no reach beyond its own partner_id absent a live global
    administrator grant."""
    return {"role": "technician", "partner_id": partner_id, "email": email}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_account_recovery_route_tenant_scope.db"


def _seed(db_path):
    now = datetime.now().isoformat()
    locked = (datetime.now() + timedelta(minutes=15)).isoformat()
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
            db.execute("INSERT INTO account_lockouts(email,attempts,locked_until,last_attempt_at) VALUES('user-a@example.test',5,?,?)", (locked, now))
            db.execute("INSERT INTO account_lockouts(email,attempts,locked_until,last_attempt_at) VALUES('user-b@example.test',5,?,?)", (locked, now))


def test_unlock_same_tenant_authorized_succeeds(monkeypatch, db_path):
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        result = unlock(_fake_request(), "customer-a", "user-a")
        lockout = row("SELECT * FROM account_lockouts WHERE email=?", ("user-a@example.test",))
        account = row("SELECT password_hash,customer_id FROM partner_users WHERE id=?", ("user-a",))
    assert result == {"message": "Customer account lockout cleared. The password and customer access were not changed.", "lockout_cleared": True}
    assert lockout is None, "the unlocked account's lockout row must be gone"
    assert account["customer_id"] == "customer-a", "unlock must never move the account to another customer"


def test_unlock_same_tenant_never_changes_password(monkeypatch, db_path):
    from partner_db import verify_password
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        unlock(_fake_request(), "customer-a", "user-a")
        account = row("SELECT password_hash FROM partner_users WHERE id=?", ("user-a",))
    assert verify_password("user-a-password-123", account["password_hash"]) is True, "unlock must never touch the password"


def test_unlock_cross_tenant_denied_with_zero_mutation(monkeypatch, db_path):
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "customer-b", "user-b")
        lockout = row("SELECT * FROM account_lockouts WHERE email=?", ("user-b@example.test",))
    assert excinfo.value.status_code == 404
    assert lockout is not None and lockout["attempts"] == 5, "a denied cross-tenant unlock must leave the foreign customer's lockout untouched"


def test_unlock_cross_tenant_denial_matches_unknown_customer_404(monkeypatch, db_path):
    """No cross-tenant existence oracle: denial for a real foreign
    customer must be indistinguishable from an unknown customer_id."""
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        with pytest.raises(HTTPException) as foreign:
            unlock(_fake_request(), "customer-b", "user-b")
        with pytest.raises(HTTPException) as unknown:
            unlock(_fake_request(), "customer-does-not-exist", "user-nope")
    assert (foreign.value.status_code, foreign.value.detail) == (unknown.value.status_code, unknown.value.detail)


def test_unlock_wrong_customer_id_for_real_user_denied(monkeypatch, db_path):
    """user-a genuinely belongs to customer-a; naming a different real
    customer_id must not unlock it (the route joins on both customer_id
    and user_id, not user_id alone)."""
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        # A global-reach identity so this exercises the customer_id/user_id
        # join itself, not tenant_owns_partner()'s partner_id gate.
        from appliance_identity import has_global_administrator_grant
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: {"role": "administrator", "partner_id": None, "email": "global-admin@example.test"})
        with connection() as db:
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('global-admin','partner-a','global-admin@example.test','Global Admin','administrator','x',1,?)", (datetime.now().isoformat(),))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) VALUES('grant-global-admin','global-admin','administrator','global',NULL,?,'system:test',NULL)", (datetime.now().isoformat(),))
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "customer-b", "user-a")
        lockout = row("SELECT * FROM account_lockouts WHERE email=?", ("user-a@example.test",))
    assert excinfo.value.status_code == 404
    assert lockout is not None and lockout["attempts"] == 5


def test_unlock_unauthenticated_denied_with_zero_mutation(monkeypatch, db_path):
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        def _deny(request):
            raise HTTPException(status_code=403, detail="Partner authorization required.")
        monkeypatch.setattr(cloud_features, "require_partner_access", _deny)
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "customer-a", "user-a")
        lockout = row("SELECT * FROM account_lockouts WHERE email=?", ("user-a@example.test",))
    assert excinfo.value.status_code == 403
    assert lockout is not None and lockout["attempts"] == 5, "an unauthenticated request must leave state untouched"


def test_unlock_role_without_customer_edit_permission_denied(monkeypatch, db_path):
    """customer_viewer holds only customer.self.view (partner_db.
    ROLE_PERMISSIONS) -- require_permission(...,'customer.edit') must
    reject it even if it somehow reached this route."""
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: {"role": "customer_viewer", "partner_id": "partner-a", "customer_id": "customer-a", "email": "viewer-a@example.test"})
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "customer-a", "user-a")
        lockout = row("SELECT * FROM account_lockouts WHERE email=?", ("user-a@example.test",))
    assert excinfo.value.status_code == 403
    assert lockout is not None and lockout["attempts"] == 5


def test_unlock_does_not_mutate_unrelated_customer_fields(monkeypatch, db_path):
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        before = row("SELECT * FROM customers WHERE id=?", ("customer-a",))
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        unlock(_fake_request(), "customer-a", "user-a")
        after = row("SELECT * FROM customers WHERE id=?", ("customer-a",))
    assert before == after, "unlock must never mutate the customer row itself (plan, status, cameras, sites, etc.)"


def test_unlock_writes_an_audit_record_without_password_or_token(monkeypatch, db_path):
    unlock = _route("/api/partner/customers/{customer_id}/accounts/{user_id}/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(cloud_features, "require_partner_access", lambda request: _technician_identity())
        unlock(_fake_request(), "customer-a", "user-a")
        entry = row("SELECT * FROM audit_logs WHERE action='customer_account.unlocked' AND entity_id='user-a'")
    assert entry is not None
    assert entry["actor_email"] == "tech-a@example.test"
    assert "password" not in entry["details_json"].lower()
    assert "token" not in entry["details_json"].lower()
