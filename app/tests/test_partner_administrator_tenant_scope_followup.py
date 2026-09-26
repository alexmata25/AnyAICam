"""Partner-scoped-administrator tenant-confinement follow-up (2026-09-14
security remediation checkpoint fdb3764 -> this pass) -- Codex re-audit.

The 2026-09-14 multi-tenant security remediation (c4d5f24) fixed four
confirmed cross-tenant *mutation* vulnerabilities, but its own commit
message explicitly left three routes' pre-existing naive
`identity.get('role')=='administrator'` shortcuts untouched as
"architectural fragility... documented as follow-up security work":
customer_detail(), add_customer_note(), and onboard_customer() (all
partner_workspace.py). A live-verified GLOBAL administrator grant
(appliance_identity.has_global_administrator_grant()) and a company-
scoped 'administrator' (partner_users.role='administrator' with no such
grant, or a revoked one) carry the exact same role-name claim -- see
partner_db.tenant_owns_partner()'s own docstring -- so a bare role check
let a partner-scoped administrator reach every other partner's real
customer data through these three routes, and, discovered in this
pass's required sibling audit, through customer *listing*
(partner_workspace.render_partner_workspace()) and two directly-
equivalent appliance-listing routes using the identical pattern
(appliance_cloud.appliance_dashboard(), main.operations_rdm_page()).

All six are now routed through the same primitives the c4d5f24 pass
already established: authorize_customer_tenant() (customer_detail,
add_customer_note) and has_global_administrator_grant() directly
(onboard_customer's partner_id steering, and every listing route, none
of which resolve a single customer_id/appliance_id to authorize --
they decide whether to drop a WHERE partner_id=? clause entirely).

Fixtures: two complete, isolated tenant chains (Partner A/Customer A/
Appliance A, Partner B/Customer B/Appliance B), a partner-scoped
administrator confined to Partner A, a genuine global administrator
(live identity_grants row, scope_type='global'), and an administrator
whose global grant has been revoked -- proving revocation, not the
role claim, is what actually governs reach.

Route functions are pulled directly off the real, fully-wired
main.app route table and called as plain functions, or (for
operations_rdm_page) called directly by name -- the same established
pattern as test_customer_onboarding_identity_grants.py and
test_admin_partner_bridge_integration.py.
"""
import sqlite3
from types import SimpleNamespace

import pytest

import appliance_cloud
import main
import partner_portal
import partner_workspace
from database_backend import override_target
from partner_db import initialize_database


def _route(path, method="GET"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request():
    return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


def _admin_portal_user():
    return {"id": "admin-1", "role": "administrator", "enabled": True, "email": "admin@example.test", "camera_ids": []}


def _partner_admin_identity(email="padmin-a@example.test", partner_id="partner-a"):
    """A company-scoped administrator: partner_users.role='administrator'
    but with no live scope_type='global' identity_grants row -- exactly
    the case has_global_administrator_grant() must reject."""
    return {"role": "administrator", "partner_id": partner_id, "email": email}


def _global_admin_identity(email="global-admin@example.test"):
    return {"role": "administrator", "partner_id": None, "email": email}


ONBOARDING_PAYLOAD = {
    "name": "New Customer", "company": "New Co", "email": "new-owner@example.test", "phone": "555-0100",
    "status": "trial", "sites": [{"name": "HQ"}],
    "appliance_type": "AnyAiCam mini PC", "deployment_mode": "local",
    "pricing": {"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 5, "addons": []},
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_partner_admin_tenant_scope.db"


@pytest.fixture()
def percentage_pricing(monkeypatch):
    """calculate_partner_quote() needs a partner pricing_mode with actual
    configured prices for the onboarding payload's resolution/recording/
    retention combo -- same fixture-config swap test_customer_onboarding_
    identity_grants.py and test_samsung_mock_e2e.py already use."""
    import pricing_config
    config = pricing_config.load_pricing()
    config["partner"]["pricing_mode"] = "percentage"
    config["partner"]["percentage_discount"] = 20
    monkeypatch.setattr(pricing_config, "load_pricing", lambda: config)


def _seed_tenant(conn, *, partner_id, customer_id, appliance_id, site_id, cloud_id):
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, f"Partner {partner_id}", "approved", "real", now))
    conn.execute(
        "INSERT INTO customers(id,partner_id,name,company,email,status,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"Company {customer_id}", f"{customer_id}@example.test", "active", "real", now),
    )
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main", now))
    # partner_id IS set on the appliance row here (unlike c4d5f24's own
    # fixtures, which deliberately leave it NULL to prove the activation-
    # time-backfill case) -- appliance_dashboard()/operations_rdm_page()
    # filter directly on appliances.partner_id, the real post-activation
    # state this pass's listing fixes need to exercise.
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,state,created_at) VALUES(?,?,?,?,?,?,?)",
        (appliance_id, customer_id, site_id, cloud_id, partner_id, "online", now),
    )
    conn.commit()


def _seed_partner_scoped_administrator(conn, *, user_id, email, partner_id):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "Partner Admin", "administrator", "x", 1, "2026-01-01"),
    )
    conn.commit()


def _seed_global_administrator(conn, *, user_id, email):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, "partner-a", email, "Global Admin", "administrator", "x", 1, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
        (f"grant-{user_id}", user_id, "administrator", "global", None, "2026-01-01", "system:test"),
    )
    conn.commit()


def _seed_revoked_global_administrator(conn, *, user_id, email, partner_id):
    """An administrator whose global grant existed but was revoked --
    must behave exactly like a partner-scoped administrator, never like
    a live global one, proving revocation (not the role claim) governs
    reach."""
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "Ex Global Admin", "administrator", "x", 1, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) VALUES(?,?,?,?,?,?,?,?)",
        (f"grant-{user_id}", user_id, "administrator", "global", None, "2026-01-01", "system:test", "2026-02-01T00:00:00"),
    )
    conn.commit()


def _seed_two_tenants(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('anyaicam-primary','AnyAiCam','approved','real','2026-01-01')")
    _seed_tenant(conn, partner_id="partner-a", customer_id="cust-a", appliance_id="appl-a", site_id="site-a", cloud_id="AIC-A")
    _seed_tenant(conn, partner_id="partner-b", customer_id="cust-b", appliance_id="appl-b", site_id="site-b", cloud_id="AIC-B")
    _seed_partner_scoped_administrator(conn, user_id="padmin-a", email="padmin-a@example.test", partner_id="partner-a")
    _seed_global_administrator(conn, user_id="global-admin", email="global-admin@example.test")
    _seed_revoked_global_administrator(conn, user_id="exadmin-a", email="exadmin-a@example.test", partner_id="partner-a")


# =============================================================== unit: administrator role alone never implies global reach


def test_partner_scoped_administrator_role_fails_the_live_global_grant_check(db_path):
    from appliance_identity import has_global_administrator_grant
    from partner_db import connection
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        with connection() as db:
            assert has_global_administrator_grant(db, email="padmin-a@example.test") is False
            assert has_global_administrator_grant(db, email="exadmin-a@example.test") is False, "a revoked global grant must not still count"
            assert has_global_administrator_grant(db, email="global-admin@example.test") is True


# =============================================================== customer detail


def test_customer_detail_partner_admin_own_customer_succeeds(monkeypatch, db_path):
    customer_detail = _route("/partner/customers/{customer_id}")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_admin_identity())
        result = customer_detail(_fake_request(), "cust-a")
    assert "Customer cust-a" in result


def test_customer_detail_partner_admin_foreign_customer_denied(monkeypatch, db_path):
    customer_detail = _route("/partner/customers/{customer_id}")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_admin_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            customer_detail(_fake_request(), "cust-b")
    assert excinfo.value.status_code == 404


def test_customer_detail_revoked_grant_administrator_foreign_customer_denied(monkeypatch, db_path):
    customer_detail = _route("/partner/customers/{customer_id}")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {"role": "administrator", "partner_id": "partner-a", "email": "exadmin-a@example.test"})
        with pytest.raises(main.HTTPException) as excinfo:
            customer_detail(_fake_request(), "cust-b")
    assert excinfo.value.status_code == 404


def test_customer_detail_global_admin_foreign_customer_succeeds(monkeypatch, db_path):
    customer_detail = _route("/partner/customers/{customer_id}")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _global_admin_identity())
        result = customer_detail(_fake_request(), "cust-b")
    assert "Customer cust-b" in result


def test_customer_detail_unknown_and_foreign_ids_produce_identical_404(monkeypatch, db_path):
    """No cross-tenant existence oracle: a caller who isn't authorized to
    see cust-b gets the exact same response as one naming a customer_id
    that has never existed."""
    customer_detail = _route("/partner/customers/{customer_id}")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_admin_identity())
        with pytest.raises(main.HTTPException) as foreign:
            customer_detail(_fake_request(), "cust-b")
        with pytest.raises(main.HTTPException) as unknown:
            customer_detail(_fake_request(), "cust-does-not-exist")
    assert (foreign.value.status_code, foreign.value.detail) == (unknown.value.status_code, unknown.value.detail)


# =============================================================== customer listing (render_partner_workspace)


def test_customer_listing_partner_admin_sees_only_own_tenant(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_admin_identity())
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _partner_admin_identity())
        result = main.partner_portal(_fake_request())
    assert "Customer cust-a" in result
    assert "Customer cust-b" not in result, "a partner-scoped administrator must never receive a foreign partner's customer records"
    assert "AIC-A" in result
    assert "AIC-B" not in result, "the same listing's appliance section must be equally tenant-scoped"


def test_customer_listing_revoked_grant_administrator_sees_only_own_tenant(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        revoked_identity = {"role": "administrator", "partner_id": "partner-a", "email": "exadmin-a@example.test"}
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: revoked_identity)
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: revoked_identity)
        result = main.partner_portal(_fake_request())
    assert "Customer cust-a" in result
    assert "Customer cust-b" not in result


def test_customer_listing_global_admin_sees_both_tenants(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _global_admin_identity())
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _global_admin_identity())
        result = main.partner_portal(_fake_request())
    assert "Customer cust-a" in result
    assert "Customer cust-b" in result


# =============================================================== customer notes


def test_add_note_partner_admin_own_tenant_succeeds(monkeypatch, db_path):
    add_note = _route("/api/partner/customers/{customer_id}/notes", method="POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _partner_admin_identity())
        result = add_note(_fake_request(), "cust-a", {"note": "Installed camera 3."})
        note = sqlite3.connect(db_path).execute("SELECT note FROM customer_notes WHERE customer_id='cust-a'").fetchone()
    assert result["message"] == "Customer note saved."
    assert note == ("Installed camera 3.",)


def test_add_note_partner_admin_foreign_denied_with_zero_mutation(monkeypatch, db_path):
    add_note = _route("/api/partner/customers/{customer_id}/notes", method="POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _partner_admin_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            add_note(_fake_request(), "cust-b", {"note": "attacker note"})
        note_count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM customer_notes WHERE customer_id='cust-b'").fetchone()[0]
    assert excinfo.value.status_code == 404
    assert note_count == 0, "a denied cross-tenant note request must create zero rows"


def test_add_note_global_admin_foreign_succeeds(monkeypatch, db_path):
    add_note = _route("/api/partner/customers/{customer_id}/notes", method="POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "require_partner_access", lambda request: _global_admin_identity())
        result = add_note(_fake_request(), "cust-b", {"note": "support note"})
        note = sqlite3.connect(db_path).execute("SELECT note FROM customer_notes WHERE customer_id='cust-b'").fetchone()
    assert result["message"] == "Customer note saved."
    assert note == ("support note",)


# =============================================================== customer onboarding


def test_onboarding_partner_admin_confined_to_own_partner_even_when_payload_requests_foreign(monkeypatch, db_path, percentage_pricing):
    onboard_customer = _route("/api/partner/customers/onboard", method="POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _partner_admin_identity())
        payload = dict(ONBOARDING_PAYLOAD, partner_id="partner-b")  # attacker-supplied foreign partner_id
        result = onboard_customer(_fake_request(), payload)
        row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE id=?", (result["customer"]["id"],)).fetchone()
        foreign_count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM customers WHERE partner_id='partner-b'").fetchone()[0]
    assert row[0] == "partner-a", "a partner-scoped administrator must be confined to its own tenant even when the payload names a foreign partner_id"
    assert foreign_count == 1, "only the pre-seeded cust-b may exist under partner-b -- zero new rows there"


def test_onboarding_global_admin_can_steer_partner_id(monkeypatch, db_path, percentage_pricing):
    onboard_customer = _route("/api/partner/customers/onboard", method="POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _global_admin_identity())
        payload = dict(ONBOARDING_PAYLOAD, partner_id="partner-b")
        result = onboard_customer(_fake_request(), payload)
        row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE id=?", (result["customer"]["id"],)).fetchone()
    assert row[0] == "partner-b"


def test_onboarding_revoked_grant_administrator_confined_to_own_partner(monkeypatch, db_path, percentage_pricing):
    onboard_customer = _route("/api/partner/customers/onboard", method="POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {"role": "administrator", "partner_id": "partner-a", "email": "exadmin-a@example.test"})
        payload = dict(ONBOARDING_PAYLOAD, partner_id="partner-b")
        result = onboard_customer(_fake_request(), payload)
        row = sqlite3.connect(db_path).execute("SELECT partner_id FROM customers WHERE id=?", (result["customer"]["id"],)).fetchone()
    assert row[0] == "partner-a"


# =============================================================== sibling-audit finding #1: appliance_dashboard() (appliance_cloud.py)


def test_appliance_dashboard_partner_admin_sees_only_own_tenant(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _partner_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _partner_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        result = appliance_dashboard(_fake_request())
    assert "AIC-A" in result
    assert "AIC-B" not in result


def test_appliance_dashboard_global_admin_sees_both_tenants(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _global_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _global_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        result = appliance_dashboard(_fake_request())
    assert "AIC-A" in result
    assert "AIC-B" in result


# =============================================================== sibling-audit finding #2: operations_rdm_page() (main.py)


def test_operations_rdm_partner_admin_sees_only_own_tenant(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _partner_admin_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        result = main.operations_rdm_page(_fake_request())
    assert "AIC-A" in result
    assert "AIC-B" not in result


def test_operations_rdm_global_admin_sees_both_tenants(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _global_admin_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        result = main.operations_rdm_page(_fake_request())
    assert "AIC-A" in result
    assert "AIC-B" in result
