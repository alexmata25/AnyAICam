"""End-to-end regression coverage for the Admin Portal <-> Partner Portal
identity bridge (see app/admin_partner_bridge.py for the design), against
the real app (TestClient(main.app)):

  - GET /operations/rdm using the bridge instead of a direct Partner
    Portal session (function-level: current_user()/partner_identity()
    monkeypatched, same pattern as test_operations_rdm.py).
  - POST /api/partner/appliances/{id}/commands (appliance_cloud.py's
    queue_command()) actually accepting a bridged identity end-to-end
    through real HTTP with a real, signed Admin Portal session cookie
    (main.create_session()) and no Partner Portal cookie at all --
    proving the bridge reaches the destructive-action endpoint itself,
    not just the read-only page. queue_command() closes over the
    current_user callable it was registered with at import time, so
    monkeypatching main.current_user would silently do nothing here;
    a real session cookie is what actually exercises this path.
  - link/unlink routes, including the exact scenarios the task called
    out: customer accounts can never link, admin roles below manage_
    settings can never link, and no password is ever read for it.
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


def _admin_portal_user(role="administrator"):
    return {"id": "admin-1", "role": role, "enabled": True, "email": "admin@example.test", "camera_ids": []}


def _administrator_partner_identity():
    return {"role": "administrator", "email": "tech@example.test", "partner_id": None, "customer_id": None}


# =============================================================== /operations/rdm via the bridge (function-level)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_bridge_integration.db"


def _seed_appliance(conn, appliance_id, partner_id, customer_id, cloud_id):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, f"Partner {partner_id}", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{appliance_id}", customer_id, "Main", "2026-01-01"))
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,state,created_at) VALUES(?,?,?,?,?,?,?)",
        (appliance_id, customer_id, f"site-{appliance_id}", cloud_id, partner_id, "online", "2026-01-01"),
    )
    conn.commit()


def _seed_partner_user(conn, user_id, email, role, partner_id="partner-1"):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "Tech", role, "x", 1, "2026-01-01"),
    )
    conn.commit()


def _seed_link(conn, admin_user_id, admin_email, partner_user_id, partner_email, now="2026-08-27T00:00:00"):
    conn.execute(
        "INSERT INTO admin_partner_links(admin_user_id,admin_email,partner_user_id,partner_email,linked_at,linked_by,revoked_at) VALUES(?,?,?,?,?,?,NULL)",
        (admin_user_id, admin_email, partner_user_id, partner_email, now, admin_email),
    )
    conn.commit()


def test_rdm_page_renders_via_bridge_when_no_direct_partner_session(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)  # no direct session at all
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_appliance(conn, "appl-1", "partner-1", "cust-1", "AIC-BRIDGE")
        _seed_partner_user(conn, "pu-1", "tech@example.test", "technician")
        _seed_link(conn, "admin-1", "admin@example.test", "pu-1", "tech@example.test")
        result = main.operations_rdm_page(_stub_request())
    assert "AIC-BRIDGE" in result
    assert "Viewing via your linked partner account" in result
    assert "tech@example.test" in result
    assert 'id="unlink-partner-account"' in result
    assert 'data-command="restart_vms"' in result  # technician has appliance.action


def test_rdm_page_falls_back_to_honest_state_when_bridge_role_has_drifted_to_customer(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_partner_user(conn, "pu-1", "tech@example.test", "customer_viewer")  # drifted since linking
        _seed_link(conn, "admin-1", "admin@example.test", "pu-1", "tech@example.test")
        result = main.operations_rdm_page(_stub_request())
    assert "Sign in to the Partner Portal directly" in result
    assert 'data-command="restart_vms"' not in result


def test_rdm_page_still_shows_link_offer_for_a_real_direct_partner_session_not_yet_linked(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_partner_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        result = main.operations_rdm_page(_stub_request())
    assert 'id="link-partner-account"' in result
    assert "Viewing via your linked partner account" not in result  # this is a direct session, not a bridge


# =============================================================== link / unlink routes (function-level)


def test_link_route_requires_admin_portal_access(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "v-1", "role": "viewer", "enabled": True})
    with pytest.raises(main.HTTPException) as excinfo:
        main.link_partner_account(_stub_request())
    assert excinfo.value.status_code == 403


def test_link_route_requires_a_live_partner_session(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    with pytest.raises(main.HTTPException) as excinfo:
        main.link_partner_account(_stub_request())
    assert excinfo.value.status_code == 400


def test_link_route_refuses_a_customer_partner_identity(monkeypatch):
    """The exact requirement: customer accounts must never gain partner
    access through this bridge -- even if the admin somehow has a live
    'customer_owner'/'customer_viewer' partner_identity() session."""
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: {"role": "customer_owner", "email": "cust@example.test", "partner_id": None, "customer_id": "cust-1"})
    with pytest.raises(main.HTTPException) as excinfo:
        main.link_partner_account(_stub_request())
    assert excinfo.value.status_code == 403
    assert "can't be linked" in excinfo.value.detail


def test_link_route_creates_a_real_link_and_never_touches_a_password_field(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _administrator_partner_identity())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_partner_user(conn, "pu-1", "tech@example.test", "administrator")
        result = main.link_partner_account(_stub_request())
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        link = conn2.execute("SELECT * FROM admin_partner_links WHERE admin_user_id='admin-1'").fetchone()
        password_untouched = conn2.execute("SELECT password_hash FROM partner_users WHERE id='pu-1'").fetchone()["password_hash"]
    assert result["status"] == "linked"
    assert link["partner_email"] == "tech@example.test"
    assert password_untouched == "x"  # unchanged -- this route never wrote a password anywhere


def test_unlink_route_revokes_the_link(monkeypatch, db_path):
    monkeypatch.setattr(main, "current_user", lambda request: _admin_portal_user())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_partner_user(conn, "pu-1", "tech@example.test", "technician")
        _seed_link(conn, "admin-1", "admin@example.test", "pu-1", "tech@example.test")
        result = main.unlink_partner_account(_stub_request())
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        link = conn2.execute("SELECT revoked_at FROM admin_partner_links WHERE admin_user_id='admin-1'").fetchone()
    assert result["status"] == "unlinked"
    assert link["revoked_at"] is not None


# =============================================================== POST /api/partner/appliances/{id}/commands -- real HTTP, real cookies


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    db_path = tmp_path / "test_commands.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client, db_path


def _admin_session_cookie(admin_id="admin-1", role="administrator"):
    main.save_users([{"id": admin_id, "email": "admin@example.test", "role": role, "enabled": True, "camera_ids": []}])
    return main.create_session(admin_id)


def test_command_endpoint_accepts_a_bridged_admin_session_with_no_partner_cookie_at_all(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_appliance(conn, "appl-http-1", "partner-1", "cust-1", "AIC-HTTP-1")
    _seed_partner_user(conn, "pu-1", "tech@example.test", "technician")
    _seed_link(conn, "admin-1", "admin@example.test", "pu-1", "tech@example.test")
    token = _admin_session_cookie()

    response = client.post(
        "/api/partner/appliances/appl-http-1/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies={main.SESSION_COOKIE_NAME: token},  # no partner_portal.SESSION_COOKIE set at all
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    queued = sqlite3.connect(db_path).execute(
        "SELECT command,created_by FROM appliance_commands WHERE appliance_id='appl-http-1'"
    ).fetchone()
    assert queued == ("restart_vms", "tech@example.test")  # queued under the real partner identity's own email


def test_command_endpoint_rejects_an_admin_session_with_no_bridge_at_all(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_appliance(conn, "appl-http-2", "partner-1", "cust-1", "AIC-HTTP-2")
    token = _admin_session_cookie()

    response = client.post(
        "/api/partner/appliances/appl-http-2/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 403


def test_command_endpoint_partner_only_session_is_completely_unaffected_by_the_bridge(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_appliance(conn, "appl-http-3", "partner-1", "cust-1", "AIC-HTTP-3")
    partner_token = partner_portal._token("owner@example.test", "administrator", None, None, None)

    response = client.post(
        "/api/partner/appliances/appl-http-3/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies={partner_portal.SESSION_COOKIE: partner_token},  # no admin cookie at all
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_command_endpoint_still_denies_a_customer_partner_session(http_client):
    client, db_path = http_client
    conn = sqlite3.connect(db_path)
    _seed_appliance(conn, "appl-http-4", "partner-1", "cust-1", "AIC-HTTP-4")
    customer_token = partner_portal._token("cust-owner@example.test", "customer_owner", None, "cust-1", None)

    response = client.post(
        "/api/partner/appliances/appl-http-4/commands",
        json={"command": "restart_vms", "confirmed": True},
        cookies={partner_portal.SESSION_COOKIE: customer_token},
    )
    assert response.status_code == 403
