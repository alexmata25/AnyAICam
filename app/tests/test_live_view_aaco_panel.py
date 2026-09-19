"""Live View regression coverage unrelated to AACO's own UI.

2026-09-19: this file previously covered a page-specific fixed AACO
panel embedded only on /customer-live. That panel was retired in favor
of the persistent floating AACO assistant injected once by the shared
customer page shell (main.py's page_shell()) -- see
test_aaco_floating_widget.py for all AACO-widget coverage now,
including on this exact page. What remains here is Live View's own
existing behavior, confirmed unaffected by that change.

Real HTTP through the real app (TestClient(main.app)), the same
fixture shape as test_aaco_floating_widget.py."""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_aaco_panel.db"


def _seed(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,"
        "door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
        "VALUES('cam-door','cust-a','site-a','appl-a',1,'Front Door',1,2,4500,'2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-plain','cust-a','site-a','appl-a',2,'Backyard','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-owner','owner-a@example.test','customer_owner','cust-a','x','all','2026-01-01')"
    )
    conn.commit()


def _owner_cookie():
    return partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-a", None)


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed(conn)
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


def test_existing_live_grid_tiles_and_controls_are_unchanged(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-camera-id="cam-door"' in response.text
    assert 'data-camera-id="cam-plain"' in response.text
    assert 'id="unlock-door-cam-door"' in response.text
    assert "wireUnlockButton" in response.text
    assert "querySelectorAll('.unlock-door')" in response.text


def test_single_camera_live_page_keeps_its_own_existing_tools_unchanged(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="unlock-door-cam-door"' in response.text


def test_unauthenticated_request_still_redirects_exactly_as_before(client):
    response = client.get("/customer-live", follow_redirects=False)
    assert response.status_code in (303, 307)


def test_standalone_aaco_workspace_still_reachable_and_unchanged(client):
    response = client.get("/aaco", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "AACO loads VMS data only after a command" in response.text
    assert "Show the front entrance" in response.text
