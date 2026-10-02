"""Multi-tenant security remediation (2026-09-14) -- Codex tenant-
isolation audit (outputs/production-security-tenant-isolation-audit.md).

Fixes four confirmed cross-tenant authorization vulnerabilities, all
sharing the same root cause: a partner-management route accepted a
caller-supplied customer_id/appliance_id and performed a real database
mutation without ever verifying the resource belonged to the caller's
own tenant.

  1. CRITICAL -- invite_portal_user() (partner_workspace.py): any user
     holding user.invite (partner_owner OR customer_owner) could mint a
     real customer_owner/customer_viewer identity grant for an
     arbitrary customer_id, including one owned by a different
     partner -- cross-tenant customer-account takeover.
  2. HIGH -- queue_command() (appliance_cloud.py, the RDM command
     queue): any partner_owner/technician could queue a real remote
     command for another tenant's appliance.
  3. HIGH -- regenerate_activation_token() (partner_workspace.py): any
     partner_owner/technician could revoke a foreign appliance's real
     activation tokens and receive a usable replacement.
  4. HIGH -- change_customer_plan() (partner_workspace.py): any
     partner_owner/salesperson could create a plan/commercial record
     for an arbitrary customer_id.

Fixed with one reusable, well-tested tenant/resource-ownership
primitive (partner_db.py): tenant_owns_partner() (the core check --
never a bare identity['role']=='administrator' shortcut, always a
live-verified global grant or an exact partner_id match) plus
authorize_customer_tenant()/authorize_appliance_tenant() (thin,
resource-specific wrappers). Applied to all four confirmed routes plus
two directly-equivalent findings discovered during the required audit
of other partner-management routes accepting a customer/appliance id:
appliance_action() (a placeholder route, same missing check) and
deliver_quote() (cloud_features.py, same missing check, reused the
core primitive directly since quotes.partner_id is always populated).

Fixtures represent two complete, isolated tenant chains as required:
Partner A / Customer A / Appliance A and Partner B / Customer B /
Appliance B, plus a genuine global administrator (a real,
live-verified identity_grants row with scope_type='global' --
never merely a partner_users.role=='administrator' claim, since that
claim is byte-identical for a true platform administrator and a
company-scoped one; see tenant_owns_partner()'s own docstring).

Real TestClient(FastAPI()) with all three affected route modules
registered, real signed session cookies via partner_portal._token(),
matching the established pattern in test_appliance_activation_token_
regeneration.py and test_admin_partner_bridge_integration.py.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_multi_tenant_security.db"):
    import appliance_cloud
    import cloud_features
    import partner_portal
    import partner_workspace
    from partner_db import authorize_appliance_tenant, authorize_customer_tenant, connection, password_hash, tenant_owns_partner


# --------------------------------------------------------- fixtures: two complete, isolated tenant chains


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_multi_tenant_security.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        partner_workspace.register_partner_workspace_routes(app, shell=lambda *a, **k: "")
        cloud_features.register_cloud_feature_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _db(db_path):
    return override_target(sqlite_path=str(db_path))


def _seed_tenant(conn, *, partner_id, customer_id, appliance_id, site_id, cloud_id):
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, f"Partner {partner_id}", "approved", "real", now))
    conn.execute(
        "INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "real", now),
    )
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main", now))
    # partner_id deliberately NOT set on the appliance row itself here --
    # matching real production behavior, where appliances.partner_id is
    # only ever backfilled at activation time (see appliance_cloud.py's
    # own COALESCE backfill). Ownership must resolve correctly through
    # the customer join regardless, which is exactly what these tests
    # prove.
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, customer_id, site_id, cloud_id, now),
    )
    conn.execute(
        "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
        (f"tok-{appliance_id}", appliance_id, password_hash("original-token"), (datetime.now() + timedelta(hours=24)).isoformat(), now),
    )
    conn.commit()


def _seed_partner_owner(conn, *, user_id, email, partner_id):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "Owner", "partner_owner", "x", 1, "2026-01-01"),
    )
    conn.commit()


def _seed_technician(conn, *, user_id, email, partner_id):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "Tech", "technician", "x", 1, "2026-01-01"),
    )
    conn.commit()


def _seed_customer_owner(conn, *, user_id, email, partner_id, customer_id):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "Cust Owner", "customer_owner", "x", 1, customer_id, "2026-01-01"),
    )
    conn.commit()


def _seed_global_administrator(conn, *, user_id, email):
    """The one, correct, verifiable global-authority mechanism -- see
    partner_db.tenant_owns_partner()'s own docstring for why this is
    required instead of a bare role=='administrator' claim."""
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, "partner-a", email, "Global Admin", "administrator", "x", 1, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
        (f"grant-{user_id}", user_id, "administrator", "global", None, "2026-01-01", "system:test"),
    )
    conn.commit()


def _cookie(email, role, partner_id=None, customer_id=None):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, partner_id, customer_id, None)}


def _seed_two_tenants(conn):
    # 'anyaicam-primary' is this codebase's own established single-
    # tenant fallback partner_id (identity.get('partner_id') or
    # 'anyaicam-primary', used throughout partner_workspace.py) -- a
    # real partners row is required for it here because partner_users.
    # partner_id carries a real FOREIGN KEY REFERENCES partners(id),
    # and a global administrator's own session legitimately carries no
    # partner_id at all.
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('anyaicam-primary','AnyAiCam','approved','real','2026-01-01')")
    _seed_tenant(conn, partner_id="partner-a", customer_id="cust-a", appliance_id="appl-a", site_id="site-a", cloud_id="AIC-A")
    _seed_tenant(conn, partner_id="partner-b", customer_id="cust-b", appliance_id="appl-b", site_id="site-b", cloud_id="AIC-B")
    _seed_partner_owner(conn, user_id="owner-a", email="owner-a@example.test", partner_id="partner-a")
    _seed_technician(conn, user_id="tech-a", email="tech-a@example.test", partner_id="partner-a")
    _seed_customer_owner(conn, user_id="custowner-a", email="custowner-a@example.test", partner_id="partner-a", customer_id="cust-a")
    _seed_partner_owner(conn, user_id="owner-b", email="owner-b@example.test", partner_id="partner-b")
    _seed_global_administrator(conn, user_id="global-admin", email="global-admin@example.test")


# =============================================================== 1. invite_portal_user -- CRITICAL


def test_invite_same_tenant_customer_owner_succeeds(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/users/invite",
        json={"role": "customer_owner", "email": "new-owner@example.test", "customer_id": "cust-a"},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    assert response.status_code == 200, response.text
    with _db(db_path):
        with connection() as db:
            user = db.execute("SELECT customer_id FROM partner_users WHERE email='new-owner@example.test'").fetchone()
            grant = db.execute("SELECT scope_id FROM identity_grants WHERE role='customer_owner' AND scope_type='customer'").fetchone()
    assert user["customer_id"] == "cust-a"
    assert grant["scope_id"] == "cust-a"


def test_invite_cross_tenant_customer_owner_is_denied_with_zero_mutation(client, db_path):
    """The CRITICAL finding's exact reproduction: partner A's owner
    tries to invite a customer_owner for partner B's own customer."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/users/invite",
        json={"role": "customer_owner", "email": "attacker@example.test", "customer_id": "cust-b"},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    assert response.status_code == 404
    with _db(db_path):
        with connection() as db:
            user = db.execute("SELECT id FROM partner_users WHERE email='attacker@example.test'").fetchone()
            grant = db.execute("SELECT id FROM identity_grants WHERE scope_id='cust-b' AND role='customer_owner'").fetchone()
            invitation = db.execute("SELECT id FROM invitations WHERE email='attacker@example.test'").fetchone()
    assert user is None, "no user row may be created on a denied cross-tenant invitation"
    assert grant is None, "no identity grant may be created on a denied cross-tenant invitation"
    assert invitation is None, "no invitation row may be created on a denied cross-tenant invitation"


def test_invite_peer_partner_owner_is_denied(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/users/invite",
        json={"role": "customer_owner", "email": "attacker2@example.test", "customer_id": "cust-a"},
        cookies=_cookie("owner-b@example.test", "partner_owner", "partner-b"),
    )
    assert response.status_code == 404


def test_invite_customer_owner_session_is_rejected_at_the_outer_partner_role_gate(client, db_path):
    """require_partner_access(request)'s own default roles set
    (PARTNER_ROLES) does not include customer_owner/customer_viewer at
    all -- ROLE_PERMISSIONS['customer_owner'] listing 'user.invite' is
    reachable only by a caller who already cleared that outer gate
    (partner_owner/salesperson/technician/administrator), never by a
    customer_owner session itself calling this specific route. This is
    existing, pre-remediation permission design, confirmed here as a
    boundary this fix must not change -- not a finding of this pass."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/users/invite",
        json={"role": "customer_viewer", "email": "viewer@example.test", "customer_id": "cust-a"},
        cookies=_cookie("custowner-a@example.test", "customer_owner", "partner-a", "cust-a"),
    )
    assert response.status_code == 403
    with _db(db_path):
        with connection() as db:
            assert db.execute("SELECT id FROM partner_users WHERE email='viewer@example.test'").fetchone() is None


def test_invite_global_administrator_can_invite_across_tenants(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/users/invite",
        json={"role": "customer_owner", "email": "support-created@example.test", "customer_id": "cust-b"},
        cookies=_cookie("global-admin@example.test", "administrator", None),
    )
    assert response.status_code == 200, response.text


def test_invite_partner_side_role_ignores_customer_id_entirely_and_stays_safe(client, db_path):
    """partner_owner/salesperson/technician invitations were never the
    vulnerable branch (their partner_id always comes from the caller's
    own identity, never the payload) -- confirm this remains true."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/users/invite",
        json={"role": "technician", "email": "new-tech@example.test"},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    assert response.status_code == 200, response.text
    with _db(db_path):
        with connection() as db:
            user = db.execute("SELECT partner_id FROM partner_users WHERE email='new-tech@example.test'").fetchone()
    assert user["partner_id"] == "partner-a"


# =============================================================== 2. RDM appliance command queue -- HIGH


def test_queue_command_same_tenant_succeeds(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/appliances/appl-a/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"


def test_queue_command_cross_tenant_is_denied_with_zero_mutation(client, db_path):
    """The HIGH finding's exact reproduction."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/appliances/appl-b/commands",
        json={"command": "reboot_appliance", "confirmed": True},
        cookies=_cookie("tech-a@example.test", "technician", "partner-a"),
    )
    assert response.status_code == 404
    with _db(db_path):
        with connection() as db:
            command = db.execute("SELECT id FROM appliance_commands WHERE appliance_id='appl-b'").fetchone()
    assert command is None, "no command row may be queued for a foreign appliance on denial"


def test_queue_command_peer_partner_technician_is_denied(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            _seed_technician(db, user_id="tech-b", email="tech-b@example.test", partner_id="partner-b")

    response = client.post(
        "/api/partner/appliances/appl-a/commands",
        json={"command": "restart_service", "confirmed": True},
        cookies=_cookie("tech-b@example.test", "technician", "partner-b"),
    )
    assert response.status_code == 404


def test_queue_command_global_administrator_succeeds_across_tenants(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post(
        "/api/partner/appliances/appl-b/commands",
        json={"command": "run_diagnostics", "confirmed": True},
        cookies=_cookie("global-admin@example.test", "administrator", None),
    )
    assert response.status_code == 200, response.text


def test_queue_command_cross_tenant_denial_does_not_leak_an_existing_pending_commands_id(client, db_path):
    """A cross-tenant caller must not learn anything about a foreign
    appliance's own already-queued commands via the de-dup response --
    the ownership check runs before the de-dup lookup for exactly this
    reason."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            db.execute(
                "INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                ("real-cmd-1", "appl-b", "restart_vms", "{}", "pending", "2026-01-01T00:00:00", "2026-01-02T00:00:00", "owner-b@example.test"),
            )
            db.commit()

    response = client.post(
        "/api/partner/appliances/appl-b/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    assert response.status_code == 404
    assert "real-cmd-1" not in response.text


def test_queue_command_unknown_appliance_id_gets_the_same_404_as_a_real_foreign_one(client, db_path):
    """No cross-tenant existence oracle: a nonexistent id and a real,
    foreign-tenant id must be indistinguishable to the caller."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    unknown = client.post(
        "/api/partner/appliances/does-not-exist/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    foreign = client.post(
        "/api/partner/appliances/appl-b/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"),
    )
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()


# =============================================================== 3. Appliance activation-token generation -- HIGH


def test_activation_token_same_tenant_succeeds(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post("/api/partner/appliances/appl-a/activation-token", cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert response.status_code == 200, response.text
    assert "activation_token" in response.json()


def test_activation_token_cross_tenant_is_denied_and_foreign_tokens_are_unchanged(client, db_path):
    """The HIGH finding's exact reproduction: revocation/regeneration
    must not touch the foreign appliance's real, existing token."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            before = dict(db.execute("SELECT token_hash,revoked_at FROM appliance_activation_tokens WHERE appliance_id='appl-b'").fetchone())

    response = client.post("/api/partner/appliances/appl-b/activation-token", cookies=_cookie("tech-a@example.test", "technician", "partner-a"))
    assert response.status_code == 404

    with _db(db_path):
        with connection() as db:
            after = dict(db.execute("SELECT token_hash,revoked_at FROM appliance_activation_tokens WHERE appliance_id='appl-b'").fetchone())
            token_count = db.execute("SELECT COUNT(*) AS n FROM appliance_activation_tokens WHERE appliance_id='appl-b'").fetchone()["n"]
    assert after == before, "the foreign appliance's existing token row must be completely unchanged"
    assert token_count == 1, "no replacement token may be inserted for a foreign appliance on denial"


def test_activation_token_peer_partner_is_denied(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post("/api/partner/appliances/appl-a/activation-token", cookies=_cookie("owner-b@example.test", "partner_owner", "partner-b"))
    assert response.status_code == 404


def test_activation_token_global_administrator_succeeds_across_tenants(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post("/api/partner/appliances/appl-b/activation-token", cookies=_cookie("global-admin@example.test", "administrator", None))
    assert response.status_code == 200, response.text


def test_activation_token_unknown_and_foreign_appliance_ids_are_indistinguishable(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    unknown = client.post("/api/partner/appliances/does-not-exist/activation-token", cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    foreign = client.post("/api/partner/appliances/appl-b/activation-token", cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()


# =============================================================== 4. Customer-plan integrity -- HIGH


def _quote_payload():
    # Same resolution/recording/retention combination test_admin_
    # customer_management.py's own change_customer_plan() coverage
    # already uses -- calculate_partner_quote() only accepts
    # combinations actually present in the real pricing config.
    return {"resolution": "2mp", "recording": "motion", "retention": 2, "quantity": 4}


def _use_percentage_pricing_mode(monkeypatch):
    # The DEFAULT pricing config's partner.pricing_mode is 'fixed',
    # which requires a real, individually-configured partner_monthly_
    # price for every single plan_terms combination -- not populated
    # for most combinations in the shipped default config. Same
    # fixture-config swap test_customer_onboarding_identity_grants.py's
    # own onboarding-quote coverage already uses to get a real,
    # computable quote regardless of exactly which combination is
    # requested; unrelated to (and does not weaken) this phase's own
    # tenant-ownership fix.
    import pricing_config
    percentage_config = pricing_config.load_pricing()
    percentage_config["partner"]["pricing_mode"] = "percentage"
    percentage_config["partner"]["percentage_discount"] = 20
    monkeypatch.setattr(pricing_config, "load_pricing", lambda: percentage_config)


def test_change_plan_same_tenant_succeeds(client, db_path, monkeypatch):
    _use_percentage_pricing_mode(monkeypatch)
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.put("/api/partner/customers/cust-a/plan", json=_quote_payload(), cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert response.status_code == 200, response.text
    with _db(db_path):
        with connection() as db:
            plan = db.execute("SELECT id FROM plans WHERE customer_id='cust-a'").fetchone()
    assert plan is not None


def test_change_plan_cross_tenant_is_denied_with_zero_mutation(client, db_path, monkeypatch):
    """The HIGH finding's exact reproduction."""
    _use_percentage_pricing_mode(monkeypatch)
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.put("/api/partner/customers/cust-b/plan", json=_quote_payload(), cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert response.status_code == 404
    with _db(db_path):
        with connection() as db:
            plan = db.execute("SELECT id FROM plans WHERE customer_id='cust-b'").fetchone()
    assert plan is None, "no plan row may be created for a foreign customer on denial"


def test_change_plan_peer_partner_is_denied(client, db_path, monkeypatch):
    _use_percentage_pricing_mode(monkeypatch)
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.put("/api/partner/customers/cust-a/plan", json=_quote_payload(), cookies=_cookie("owner-b@example.test", "partner_owner", "partner-b"))
    assert response.status_code == 404


def test_change_plan_global_administrator_succeeds_across_tenants(client, db_path, monkeypatch):
    _use_percentage_pricing_mode(monkeypatch)
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.put("/api/partner/customers/cust-b/plan", json=_quote_payload(), cookies=_cookie("global-admin@example.test", "administrator", None))
    assert response.status_code == 200, response.text


# =============================================================== directly-equivalent findings: appliance_action, deliver_quote


def test_appliance_action_placeholder_same_tenant_succeeds(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post("/api/partner/appliances/appl-a/restart", cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert response.status_code == 200, response.text


def test_appliance_action_placeholder_cross_tenant_is_denied(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)

    response = client.post("/api/partner/appliances/appl-b/restart", cookies=_cookie("tech-a@example.test", "technician", "partner-a"))
    assert response.status_code == 404


def test_deliver_quote_same_tenant_succeeds(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            db.execute("INSERT INTO quotes(id,customer_id,partner_id,status,selection_json,totals_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                       ("quote-a", "cust-a", "partner-a", "estimate", "{}", "{}", "2026-01-01T00:00:00", "owner-a@example.test"))
            db.commit()

    response = client.post("/api/partner/quotes/quote-a/deliver", json={"email": "customer-a@example.test"}, cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert response.status_code == 200, response.text


def test_deliver_quote_cross_tenant_is_denied(client, db_path):
    """Confirmed cross-tenant existence-oracle finding from the required
    audit: a foreign quote_id must not resolve at all for this caller."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            db.execute("INSERT INTO quotes(id,customer_id,partner_id,status,selection_json,totals_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                       ("quote-b", "cust-b", "partner-b", "estimate", "{}", "{}", "2026-01-01T00:00:00", "owner-b@example.test"))
            db.commit()

    response = client.post("/api/partner/quotes/quote-b/deliver", json={"email": "attacker@example.test"}, cookies=_cookie("owner-a@example.test", "partner_owner", "partner-a"))
    assert response.status_code == 404


# =============================================================== the reusable primitive itself


def test_tenant_owns_partner_true_for_same_partner(db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            assert tenant_owns_partner(db, {"email": "owner-a@example.test", "partner_id": "partner-a"}, "partner-a") is True


def test_tenant_owns_partner_false_for_different_partner(db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            assert tenant_owns_partner(db, {"email": "owner-a@example.test", "partner_id": "partner-a"}, "partner-b") is False


def test_tenant_owns_partner_true_for_a_genuine_global_grant_regardless_of_partner_id(db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            assert tenant_owns_partner(db, {"email": "global-admin@example.test", "partner_id": None}, "partner-b") is True


def test_tenant_owns_partner_false_for_a_role_name_alone_with_no_real_grant(db_path):
    """The exact shortcut this remediation closes: a session merely
    CLAIMING role='administrator' (e.g. a forged/stale token, or a
    company-scoped administrator who is not this partner) must never
    be trusted without a live, matching identity_grants row."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            # No identity_grants row exists for this email at all.
            assert tenant_owns_partner(db, {"email": "nobody@example.test", "role": "administrator", "partner_id": None}, "partner-b") is False


def test_tenant_owns_partner_false_when_resource_partner_id_is_none(db_path):
    """An appliance not yet backfilled with its own partner_id column
    (pre-activation) must never be treated as "owned" by string
    coincidence with a falsy value -- callers must resolve ownership
    through the customer join instead (see authorize_appliance_tenant()),
    never pass the appliance's own possibly-null column here directly."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            assert tenant_owns_partner(db, {"email": "owner-a@example.test", "partner_id": "partner-a"}, None) is False


def test_authorize_customer_tenant_returns_none_for_unknown_customer(db_path):
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            assert authorize_customer_tenant(db, {"email": "owner-a@example.test", "role": "partner_owner", "partner_id": "partner-a"}, "does-not-exist") is None


def test_authorize_appliance_tenant_resolves_through_the_customer_join_not_the_possibly_null_column(db_path):
    """appl-a's own appliances.partner_id column is NULL in this fixture
    (see _seed_tenant()'s own comment) -- ownership must still resolve
    correctly through customer_id -> customers.partner_id."""
    with _db(db_path):
        with connection() as db:
            _seed_two_tenants(db)
            null_column = db.execute("SELECT partner_id FROM appliances WHERE id='appl-a'").fetchone()["partner_id"]
            assert null_column is None
            appliance = authorize_appliance_tenant(db, {"email": "owner-a@example.test", "role": "partner_owner", "partner_id": "partner-a"}, "appl-a")
            assert appliance is not None
            assert appliance["owning_customer_id"] == "cust-a"
