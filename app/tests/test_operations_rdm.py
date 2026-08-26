"""Regression coverage for the Admin Portal RDM page (/operations/rdm),
which wires only to the already-implemented RDM4 backend:
appliance_protocol.ALLOWED_COMMANDS and appliance_cloud.py's existing
POST /api/partner/appliances/{appliance_id}/commands (already used today
by /partner/appliance-dashboard) -- no new backend route, table, or
command was added for this page.

Same import/isolation constraints as the other integration test files in
this directory: imports `main` (Windows-native Python only), and every
test redirects to a throwaway sqlite file via override_target() before
seeding or querying anything.
"""

import sqlite3

import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


class _StubRequest:
    headers: dict = {}


def _stub_request():
    return _StubRequest()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_operations_rdm.db"


def _admin_portal_user():
    """A legacy Admin Portal identity (current_user()) with manage_settings
    -- the gate every /operations page already requires."""
    return {"id": "admin-1", "role": "administrator", "enabled": True, "camera_ids": []}


def _unprivileged_admin_portal_user():
    return {"id": "viewer-1", "role": "viewer", "enabled": True, "camera_ids": []}


def _seed_appliance(conn, appliance_id, partner_id, customer_id, cloud_id, *, cpu=42.0, memory=55.0, disk=30.0, last_check_in="2026-08-26T12:00:00", state="online"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, f"Partner {partner_id}", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
        (f"site-{appliance_id}", customer_id, "Main Site", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,software_version,last_check_in,online_status,ip_address,cpu,memory,disk,partner_id,state,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (appliance_id, customer_id, f"site-{appliance_id}", cloud_id, "1.2.3", last_check_in, "online", "10.0.0.5", cpu, memory, disk, partner_id, state, "2026-01-01"),
    )
    conn.commit()


def _seed_health_history(conn, appliance_id, status="online", cpu=42.0, memory=55.0, created_at="2026-08-26T12:00:00"):
    conn.execute(
        "INSERT INTO appliance_health_history(appliance_id,status,cpu,memory,disk_capacity,disk_used,recording_used,uptime_seconds,camera_count,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (appliance_id, status, cpu, memory, 500.0, 150.0, 100.0, 3600, 5, created_at),
    )
    conn.commit()


def _seed_camera_status(conn, appliance_id, camera_id, *, online=1, recording=1):
    conn.execute(
        "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,updated_at) VALUES(?,?,?,?,?,?,?)",
        (appliance_id, camera_id, camera_id, online, recording, 0, "2026-08-26T12:00:00"),
    )
    conn.commit()


def _administrator_identity():
    return {"role": "administrator", "email": "admin@example.test", "partner_id": None, "customer_id": None}


def _technician_identity(partner_id):
    return {"role": "technician", "email": "tech@example.test", "partner_id": partner_id, "customer_id": None}


def _salesperson_identity(partner_id):
    return {"role": "salesperson", "email": "sales@example.test", "partner_id": partner_id, "customer_id": None}


# =============================================================== gating: Admin Portal identity


def test_denies_admin_portal_role_without_manage_settings(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _unprivileged_admin_portal_user())
    result = main.operations_rdm_page(_stub_request())
    assert "Access control" in result
    assert "does not include" in result
    assert 'class="filter queue-command"' not in result


def test_shows_partner_login_prompt_when_no_partner_session(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    result = main.operations_rdm_page(_stub_request())
    assert 'href="/partner-login"' in result
    assert 'class="filter queue-command"' not in result
    # Never invents appliance data when there's no authorized identity to scope it to.
    assert '<article class="panel">' not in result


# =============================================================== data: status / last check-in / diagnostics


def test_shows_appliance_status_last_check_in_and_diagnostics(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_appliance(conn, "appl-1", "partner-1", "cust-1", "AIC-1234", cpu=61.0, memory=48.0, disk=22.0, last_check_in="2026-08-26T15:30:00")
        _seed_health_history(conn, "appl-1", status="online", cpu=61.0, memory=48.0, created_at="2026-08-26T15:30:00")
        _seed_camera_status(conn, "appl-1", "cam-1", online=1, recording=1)
        _seed_camera_status(conn, "appl-1", "cam-2", online=0, recording=0)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_identity())
        result = main.operations_rdm_page(_stub_request())
    assert "AIC-1234" in result
    assert "2026-08-26T15:30:00" in result  # last check-in
    assert "61.0% / 48.0% / 22.0 GB" in result  # cpu/memory/disk diagnostics
    assert "1/2" in result  # cameras online/total


def test_no_appliances_renders_honest_empty_state_not_blank(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_identity())
        result = main.operations_rdm_page(_stub_request())
    assert "No appliances found for this account." in result
    assert result.strip() != ""


# =============================================================== actions: Restart VMS / Reboot appliance + confirmations


def test_action_buttons_present_for_role_with_appliance_action(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-1", "partner-1", "cust-1", "AIC-1234")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_identity())
        result = main.operations_rdm_page(_stub_request())
    assert 'data-command="restart_vms"' in result
    assert 'data-command="reboot_appliance"' in result
    assert ">Restart VMS<" in result
    assert ">Reboot appliance<" in result


def test_action_buttons_hidden_for_role_without_appliance_action(monkeypatch, db_path):
    # salesperson has no 'appliance.action' permission in partner_db.
    # ROLE_PERMISSIONS -- the page must show the appliance (view is fine)
    # but never offer a destructive action this role's own click would
    # just be rejected for server-side anyway.
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-1", "partner-1", "cust-1", "AIC-1234")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _salesperson_identity("partner-1"))
        result = main.operations_rdm_page(_stub_request())
    assert "AIC-1234" in result  # still visible
    assert 'class="filter queue-command"' not in result
    assert "appliance.action permission required" in result


def test_both_destructive_commands_have_confirmation_warnings(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-1", "partner-1", "cust-1", "AIC-1234")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_identity())
        result = main.operations_rdm_page(_stub_request())
    assert "DISRUPTIVE_COMMAND_WARNINGS" in result
    assert "reboot_appliance:" in result
    assert "restart_vms:" in result
    assert "if(!confirm(" in result  # every queue-command click is gated behind confirm()


def test_submits_to_the_existing_command_endpoint_no_new_route_invented(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-1", "partner-1", "cust-1", "AIC-1234")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_identity())
        result = main.operations_rdm_page(_stub_request())
    # The exact route already registered in appliance_cloud.py
    # (@app.post('/api/partner/appliances/{appliance_id}/commands')) --
    # confirms this page is a new frontend onto old backend, not a new
    # backend surface of its own.
    assert "fetch(`/api/partner/appliances/${button.dataset.appliance}/commands`" in result


# =============================================================== role protection on the VIEW side: no cross-partner leakage


def test_non_administrator_only_sees_their_own_partners_appliances(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-mine", "partner-1", "cust-1", "AIC-MINE")
        _seed_appliance(conn, "appl-theirs", "partner-2", "cust-2", "AIC-THEIRS")
        conn.commit()
        monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _technician_identity("partner-1"))
        result = main.operations_rdm_page(_stub_request())
    assert "AIC-MINE" in result
    assert "AIC-THEIRS" not in result


def test_administrator_sees_every_partners_appliances(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-a", "partner-1", "cust-1", "AIC-A")
        _seed_appliance(conn, "appl-b", "partner-2", "cust-2", "AIC-B")
        conn.commit()
        monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_identity())
        result = main.operations_rdm_page(_stub_request())
    assert "AIC-A" in result
    assert "AIC-B" in result


# =============================================================== navigation: reachable from the Operations hub


def test_operations_hub_links_to_the_rdm_page(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    result = main.operations_page(_stub_request())
    assert 'href="/operations/rdm"' in result
