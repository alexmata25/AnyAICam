"""Final tenant-isolation re-audit follow-up (2026-09-14 security
remediation, checkpoint fdebc42 -> this pass) -- Codex independent
re-audit of source commit 73c09e1.

73c09e1 (test_partner_administrator_tenant_scope_followup.py, 19 tests)
closed six partner-scoped-administrator bypasses. Codex's independent
re-read of that same commit confirmed those six paths and found three
more in the same administrator-scope family, none touched by 73c09e1:

1. Partner Pricing Administration (partner_portal.py's GET
   /partner-pricing-admin and PUT /api/admin/partner-pricing) granted
   global pricing read/write from `identity['role']=='administrator'`
   alone -- central retail/partner pricing, margins, MAP, and
   commercial settings, reachable by any partner-scoped administrator.
2. appliance_cloud.py's appliance_dashboard() scoped its appliance
   cards correctly (73c09e1) but joined appliance_commands with no
   tenant predicate (a foreign command-history leak) and ran its
   stale-appliance housekeeping UPDATE unconditionally across every
   partner on every page load (a cross-tenant state mutation any
   authenticated partner could trigger).
3. main.py's operations_rdm_page() scoped its appliance cards correctly
   (73c09e1) but its restart/reboot command-history query had the same
   missing tenant predicate as finding 2.

All three are now routed through the same primitive as 73c09e1:
appliance_identity.has_global_administrator_grant(), never a bare
`identity['role']=='administrator'` shortcut. See that function's own
docstring, and partner_db.tenant_owns_partner()'s, for why: a company-
scoped administrator (identity_grants scope_type='partner') and a true
platform-global one (scope_type='global') carry the byte-identical role
claim, and only a live, unrevoked global grant may tell them apart.

Fixtures and identity helpers are reused from
test_partner_administrator_tenant_scope_followup.py rather than
duplicated -- same two isolated tenant chains (Partner A/Customer A/
Appliance A, Partner B/Customer B/Appliance B), same partner-scoped
administrator, same genuine global administrator, same revoked-global-
grant administrator.
"""
import sqlite3

import pytest

import appliance_cloud
import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database

from test_partner_administrator_tenant_scope_followup import (
    _admin_portal_user,
    _fake_request,
    _global_admin_identity,
    _partner_admin_identity,
    _route,
    _seed_two_tenants,
    db_path,  # noqa: F401 -- reused fixture
)


def _revoked_admin_identity(email="exadmin-a@example.test", partner_id="partner-a"):
    return {"role": "administrator", "partner_id": partner_id, "email": email}


def _seed_appliance_command(conn, *, command_id, appliance_id, command, status="pending"):
    conn.execute(
        "INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (command_id, appliance_id, command, "{}", status, "2026-01-01T00:00:00", "2026-01-01T01:00:00"),
    )
    conn.commit()


# =============================================================== finding 1: Partner Pricing Administration


def test_pricing_admin_page_partner_scoped_administrator_denied(monkeypatch, db_path):
    load_spy = []
    monkeypatch.setattr(partner_portal, "load_pricing", lambda: (load_spy.append(1), {})[1])
    partner_admin_page = _route("/partner-pricing-admin")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_portal, "_identity", lambda request: _partner_admin_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            partner_admin_page(_fake_request())
    assert excinfo.value.status_code == 403
    assert load_spy == [], "pricing data must not be read before the global-grant check passes"


def test_pricing_admin_page_revoked_global_admin_denied(monkeypatch, db_path):
    partner_admin_page = _route("/partner-pricing-admin")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_portal, "_identity", lambda request: _revoked_admin_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            partner_admin_page(_fake_request())
    assert excinfo.value.status_code == 403


def test_pricing_admin_page_genuine_global_admin_succeeds(monkeypatch, db_path):
    partner_admin_page = _route("/partner-pricing-admin")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_portal, "_identity", lambda request: _global_admin_identity())
        result = partner_admin_page(_fake_request())
    assert "pricing" in result.lower()


def test_pricing_write_partner_scoped_administrator_denied_zero_mutation(monkeypatch, db_path):
    save_spy = []
    monkeypatch.setattr(partner_portal, "save_pricing", lambda config: save_spy.append(config))
    admin_update = _route("/api/admin/partner-pricing", method="PUT")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_portal, "_require", lambda request, roles=None: _partner_admin_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            admin_update(_fake_request(), {"trial_days": 45})
    assert excinfo.value.status_code == 403
    assert save_spy == [], "a denied global-pricing write must persist zero changes"


def test_pricing_write_revoked_global_admin_denied_zero_mutation(monkeypatch, db_path):
    save_spy = []
    monkeypatch.setattr(partner_portal, "save_pricing", lambda config: save_spy.append(config))
    admin_update = _route("/api/admin/partner-pricing", method="PUT")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_portal, "_require", lambda request, roles=None: _revoked_admin_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            admin_update(_fake_request(), {"trial_days": 45})
    assert excinfo.value.status_code == 403
    assert save_spy == []


def test_pricing_write_genuine_global_admin_succeeds(monkeypatch, db_path):
    """Confirms the write path is genuinely reachable for a live global
    grant (not merely blocked for everyone), and that the value the
    caller sent is what would be persisted -- without touching the real
    pricing.json file on disk."""
    import pricing_config
    baseline = pricing_config.load_pricing()
    save_spy = []
    monkeypatch.setattr(partner_portal, "load_pricing", lambda: baseline)
    monkeypatch.setattr(partner_portal, "save_pricing", lambda config: save_spy.append(config))
    admin_update = _route("/api/admin/partner-pricing", method="PUT")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(partner_portal, "_require", lambda request, roles=None: _global_admin_identity())
        result = admin_update(_fake_request(), {"trial_days": 45})
    assert result["status"] == "complete"
    assert len(save_spy) == 1
    assert save_spy[0]["trial_days"] == 45


# =============================================================== finding 2: appliance_cloud.appliance_dashboard() command history + stale-state mutation


def test_appliance_dashboard_command_history_partner_admin_sees_only_own_tenant(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        _seed_appliance_command(conn, command_id="cmd-a", appliance_id="appl-a", command="restart_service")
        _seed_appliance_command(conn, command_id="cmd-b", appliance_id="appl-b", command="run_diagnostics")
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _partner_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _partner_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        result = appliance_dashboard(_fake_request())
    assert "restart service" in result.lower()
    assert "run diagnostics" not in result.lower(), "a partner-scoped administrator must not see a foreign tenant's command history"
    assert "AIC-B" not in result


def test_appliance_dashboard_command_history_revoked_global_admin_sees_only_own_tenant(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        _seed_appliance_command(conn, command_id="cmd-a", appliance_id="appl-a", command="restart_service")
        _seed_appliance_command(conn, command_id="cmd-b", appliance_id="appl-b", command="run_diagnostics")
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _revoked_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _revoked_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        result = appliance_dashboard(_fake_request())
    assert "run diagnostics" not in result.lower()


def test_appliance_dashboard_command_history_global_admin_sees_both_tenants(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        _seed_appliance_command(conn, command_id="cmd-a", appliance_id="appl-a", command="restart_service")
        _seed_appliance_command(conn, command_id="cmd-b", appliance_id="appl-b", command="run_diagnostics")
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _global_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _global_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        result = appliance_dashboard(_fake_request())
    assert "restart service" in result.lower()
    assert "run diagnostics" in result.lower()


def test_appliance_dashboard_stale_state_mutation_confined_to_own_tenant(monkeypatch, db_path):
    """Both appl-a and appl-b are seeded state='online' with a NULL
    last_check_in (already stale by the 3-minute housekeeping rule). A
    partner-scoped administrator loading the dashboard must flip only
    its own appliance offline; the foreign appliance must not move."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _partner_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _partner_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        appliance_dashboard(_fake_request())
        states = dict(sqlite3.connect(db_path).execute("SELECT id,state FROM appliances WHERE id IN ('appl-a','appl-b')").fetchall())
    assert states["appl-a"] == "offline", "the caller's own stale appliance must still be swept"
    assert states["appl-b"] == "online", "a partner-scoped administrator's page load must not mutate a foreign tenant's appliance state"


def test_appliance_dashboard_stale_state_mutation_global_admin_sweeps_both_tenants(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        monkeypatch.setattr(appliance_cloud, "partner_identity", lambda request: _global_admin_identity())
        monkeypatch.setattr(appliance_cloud, "require_partner_access", lambda request: _global_admin_identity())
        appliance_dashboard = _route("/partner/appliance-dashboard")
        appliance_dashboard(_fake_request())
        states = dict(sqlite3.connect(db_path).execute("SELECT id,state FROM appliances WHERE id IN ('appl-a','appl-b')").fetchall())
    assert states["appl-a"] == "offline"
    assert states["appl-b"] == "offline", "a genuine global administrator retains its intended platform-wide housekeeping reach"


# =============================================================== finding 3: main.operations_rdm_page() command history


def test_operations_rdm_command_history_partner_admin_sees_only_own_tenant(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _partner_admin_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        _seed_appliance_command(conn, command_id="cmd-a", appliance_id="appl-a", command="restart_vms")
        _seed_appliance_command(conn, command_id="cmd-b", appliance_id="appl-b", command="reboot_appliance")
        result = main.operations_rdm_page(_fake_request())
    assert "restart vms" in result.lower()
    assert "AIC-B" not in result
    # cmd-b's own row (appliance AIC-B, "reboot appliance") must not
    # appear; "reboot appliance" alone is also a disruptive-command
    # label baked into every page's JS, so the tenant-scoping proof
    # relies on the accompanying foreign cloud_id, not the command name.


def test_operations_rdm_command_history_revoked_global_admin_sees_only_own_tenant(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _revoked_admin_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        _seed_appliance_command(conn, command_id="cmd-a", appliance_id="appl-a", command="restart_vms")
        _seed_appliance_command(conn, command_id="cmd-b", appliance_id="appl-b", command="reboot_appliance")
        result = main.operations_rdm_page(_fake_request())
    assert "AIC-B" not in result


def test_operations_rdm_command_history_global_admin_sees_both_tenants(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _global_admin_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_tenants(conn)
        _seed_appliance_command(conn, command_id="cmd-a", appliance_id="appl-a", command="restart_vms")
        _seed_appliance_command(conn, command_id="cmd-b", appliance_id="appl-b", command="reboot_appliance")
        result = main.operations_rdm_page(_fake_request())
    assert "AIC-A" in result
    assert "AIC-B" in result
