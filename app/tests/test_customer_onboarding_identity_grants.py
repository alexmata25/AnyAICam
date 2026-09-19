"""Regression coverage for the confirmed-live "manual Samsung insert"
blocker: an appliance's cloud-delegated login (appliance_identity.py's
authenticate_operator()) requires a LIVE identity_grants row before it
will ever recognize a user, but neither normal customer onboarding
(partner_workspace.py's onboard_customer()) nor inviting an additional
customer user (invite_portal_user()) ever created one -- the only way a
brand-new customer_owner/customer_viewer ever worked was an operator
manually granting it via POST /api/operations/identity-grants (see
docs/blockers-before-universal-release.md).

Both routes now call appliance_identity.create_grant() themselves,
immediately after inserting the partner_users row, in the same DB
transaction -- scope_type='customer', scope_id=<that customer_id>,
role preserved exactly as the invited/onboarded role. Deliberately NOT
extended to partner_owner/salesperson/technician invites in this pass
(their scope mapping is a separate, unresolved question) -- the last
test here proves that restraint holds.

Route functions are pulled directly off the registered FastAPI route
table and called as plain functions -- same established pattern as
test_camera_discovery_provisioning.py.
"""
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

import main
import partner_workspace
from database_backend import override_target
from partner_db import initialize_database


def _route(path, method="POST"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request():
    return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


def _partner_owner_identity():
    return {"role": "partner_owner", "partner_id": "anyaicam-primary", "email": "operator@example.test"}


def _seed_customer(conn, customer_id="cust-1"):
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('anyaicam-primary','AnyAiCam','2026-01-01')")
    conn.execute(
        "INSERT INTO customers(id,partner_id,name,company,email,status,created_at) VALUES(?,?,?,?,?,?,?)",
        (customer_id, "anyaicam-primary", "Existing Co", "Existing Co", f"{customer_id}@example.test", "active", now),
    )
    conn.commit()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_onboarding_identity_grants.db"


ONBOARDING_PAYLOAD = {
    "name": "New Customer", "company": "New Co", "email": "new-owner@example.test", "phone": "555-0100",
    "status": "trial", "sites": [{"name": "HQ"}],
    "appliance_type": "AnyAiCam mini PC", "deployment_mode": "local",
    "pricing": {"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 5, "addons": []},
}


def test_onboarding_automatically_grants_the_new_customer_owner(db_path, monkeypatch):
    onboard_customer = _route("/api/partner/customers/onboard")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('anyaicam-primary','AnyAiCam','2026-01-01')")
        conn.commit()
        # onboard_customer -> _dual_mode_identity() resolves identity via
        # partner_workspace's own `partner_identity` name (imported from
        # partner_portal), so overriding that binding is enough here --
        # unlike invite_portal_user below, which calls the imported
        # require_partner_access() function directly (see that test's
        # own comment for why it needs a different monkeypatch target).
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_owner_identity())
        # calculate_partner_quote() needs a partner pricing_mode with
        # actual configured prices for this resolution/recording/
        # retention combo -- same fixture-config swap
        # test_samsung_mock_e2e.py's env fixture already uses.
        import pricing_config
        percentage_config = pricing_config.load_pricing()
        percentage_config["partner"]["pricing_mode"] = "percentage"
        percentage_config["partner"]["percentage_discount"] = 20
        monkeypatch.setattr(pricing_config, "load_pricing", lambda: percentage_config)
        result = onboard_customer(_fake_request(), dict(ONBOARDING_PAYLOAD))
        customer_id = result["customer"]["id"]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT id FROM partner_users WHERE lower(email)=?", (ONBOARDING_PAYLOAD["email"],)).fetchone()
        grant = conn.execute(
            "SELECT role,scope_type,scope_id,revoked_at FROM identity_grants WHERE user_id=?", (user["id"],)
        ).fetchone()
    assert grant is not None, "onboarding must create a live identity_grants row for the new customer_owner -- no manual insert should be required"
    assert grant["role"] == "customer_owner"
    assert grant["scope_type"] == "customer"
    assert grant["scope_id"] == customer_id
    assert grant["revoked_at"] is None


@pytest.mark.parametrize("invited_role", ["customer_owner", "customer_viewer"])
def test_invite_portal_user_automatically_grants_customer_scoped_roles(db_path, monkeypatch, invited_role):
    invite_portal_user = _route("/api/partner/users/invite")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn, customer_id="cust-1")
        # invite_portal_user calls require_partner_access(request) --
        # imported directly from partner_portal, not routed through
        # partner_workspace's own `partner_identity` name -- so that
        # binding, not partner_identity, is what must be overridden here.
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _partner_owner_identity())
        invite_portal_user(_fake_request(), {"email": f"{invited_role}@example.test", "role": invited_role, "customer_id": "cust-1"})
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        user = conn2.execute("SELECT id FROM partner_users WHERE lower(email)=?", (f"{invited_role}@example.test",)).fetchone()
        grant = conn2.execute(
            "SELECT role,scope_type,scope_id,revoked_at FROM identity_grants WHERE user_id=?", (user["id"],)
        ).fetchone()
    assert grant is not None, f"inviting a {invited_role} must create a live identity_grants row automatically"
    assert grant["role"] == invited_role  # the invited role is preserved exactly, not collapsed to a default
    assert grant["scope_type"] == "customer"
    assert grant["scope_id"] == "cust-1"
    assert grant["revoked_at"] is None


@pytest.mark.parametrize("invited_role", ["partner_owner", "salesperson", "technician"])
def test_invite_portal_user_does_not_grant_partner_level_roles_in_this_pass(db_path, monkeypatch, invited_role):
    # Deliberate restraint: partner-level roles have no established
    # scope mapping in this pass (see this file's module docstring) --
    # this must stay a no-op for them, not silently invent one.
    invite_portal_user = _route("/api/partner/users/invite")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn, customer_id="cust-1")
        # invite_portal_user calls require_partner_access(request) --
        # imported directly from partner_portal, not routed through
        # partner_workspace's own `partner_identity` name -- so that
        # binding, not partner_identity, is what must be overridden here.
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _partner_owner_identity())
        invite_portal_user(_fake_request(), {"email": f"{invited_role}@example.test", "role": invited_role})
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        user = conn2.execute("SELECT id FROM partner_users WHERE lower(email)=?", (f"{invited_role}@example.test",)).fetchone()
        grant = conn2.execute("SELECT id FROM identity_grants WHERE user_id=?", (user["id"],)).fetchone()
    assert grant is None
