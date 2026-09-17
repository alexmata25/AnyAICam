"""Face Access UI coverage: the live-tile "Unlock Door" button and the
Camera Settings ("Camera Settings -- Face Access") section built on top
of the already-tested door_access.py API (GET/POST /api/customer/
cameras/{camera_id}/door-config, POST .../door/unlock -- see
test_door_access.py for that API's own coverage, not re-tested here).

This file only proves the UI layer: the button/section render exactly
when the spec requires (door_access_enabled=1, and never otherwise --
"Do NOT show the Unlock button on cameras not mapped to an access-
control relay"), and never for a role that cannot use them.

Real HTTP through the real app (TestClient(main.app)), a throwaway
sqlite DB via override_target() -- same style/fixture shape as
test_live_view_page_customer_auth.py, which already exercises these
routes."""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_face_access_ui.db"


def _seed(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A','2026-01-01')")
    # cam-door: a configured door (Face Access enabled, relay 2, custom pulse)
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,"
        "door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
        "VALUES('cam-door','cust-a','site-a','appl-a',1,'Front Door',1,2,4500,'2026-01-01')"
    )
    # cam-plain: a normal camera, never mapped to a relay
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-plain','cust-a','site-a','appl-a',2,'Backyard','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-owner','owner-a@example.test','customer_owner','cust-a','x','all','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-viewer','viewer-a@example.test','customer_viewer','cust-a','x','selected','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-viewer','cam-door',1)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-viewer','cam-plain',1)"
    )
    conn.commit()


def _owner_cookie():
    return partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-a", None)


def _viewer_cookie():
    return partner_portal._token("viewer-a@example.test", "customer_viewer", None, "cust-a", None)


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


# --------------------------------------------------- 1. /customer-live grid tile


def test_grid_tile_shows_unlock_button_for_a_door_configured_camera(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="unlock-door-cam-door"' in response.text
    assert 'data-camera-id="cam-door"' in response.text


def test_grid_tile_never_shows_unlock_button_for_a_camera_with_no_relay_mapped(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="unlock-door-cam-plain"' not in response.text


def test_grid_page_wires_unlock_buttons_via_the_shared_client(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "wireUnlockButton" in response.text
    assert "querySelectorAll('.unlock-door')" in response.text


# --------------------------------------------------- 2. single-camera page tools row


def test_single_camera_page_shows_unlock_button_for_a_door_configured_camera(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="unlock-door-cam-door"' in response.text


def test_single_camera_page_never_shows_unlock_button_for_a_plain_camera(client):
    response = client.get("/customer/cameras/cam-plain/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    # The .unlock-door:disabled CSS rule is always present (same as
    # .talk-mic's own always-present CSS) -- only the actual button
    # element is conditional, so the assertion targets its specific id.
    assert 'id="unlock-door-cam-plain"' not in response.text


def test_single_camera_page_shows_unlock_button_for_an_authorized_viewer_too(client):
    """Capability hint only, same convention as the talk-mic button --
    a viewer without an explicit can_unlock grant still sees the button
    (it 403s from the real API on click); this proves it's never hidden
    by role alone, only by door_access_enabled."""
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie()})
    assert response.status_code == 200
    assert 'id="unlock-door-cam-door"' in response.text


# --------------------------------------------------- 3. Camera Settings section


def test_customer_owner_sees_the_camera_settings_face_access_section(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Camera Settings" in response.text
    assert 'id="door-access-section"' in response.text
    assert 'id="save-door-access"' in response.text


def test_customer_viewer_never_sees_the_camera_settings_section(client):
    """update_door_config() is customer_owner-only server-side; this
    proves the UI never renders a form a viewer could not actually
    submit, matching this codebase's established convention."""
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie()})
    assert response.status_code == 200
    assert "door-access-section" not in response.text
    # The HTML button itself must be absent -- the page's shared JS
    # still contains a defensive `getElementById('save-door-access')`
    # lookup regardless of role (it no-ops via its own `if` guard when
    # null, same convention as every other optional-element wiring on
    # this page), so the assertion targets the rendered element, not
    # the substring.
    assert 'id="save-door-access"' not in response.text


def test_camera_settings_section_prefills_the_cameras_own_current_configuration(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    # Enabled checkbox reflects the seeded door_access_enabled=1.
    assert 'id="door-access-enabled" type="checkbox" checked' in response.text
    # Relay 2 is the seeded channel -- its <option> must carry `selected`.
    assert '<option value="2" selected>Relay 2</option>' in response.text
    assert '<option value="1">Relay 1</option>' in response.text
    # The seeded pulse duration (4500ms) is pre-filled, not the default.
    assert 'id="door-relay-pulse-ms" type="number" min="1" step="1" value="4500"' in response.text


def test_camera_settings_section_for_a_not_yet_configured_camera_defaults_unchecked(client):
    response = client.get("/customer/cameras/cam-plain/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="door-access-section"' in response.text
    assert 'id="door-access-enabled" type="checkbox" checked' not in response.text
    assert 'id="door-access-enabled" type="checkbox" ' in response.text


def test_camera_settings_section_posts_to_the_real_door_config_route(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "/api/customer/cameras/${cameraId}/door-config" in response.text


# --------------------------------------------------- 4. Viewer access (can_unlock management)


def test_owner_sees_viewer_access_section_with_the_real_viewer_listed(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Viewer access — who can press Unlock Door" in response.text
    assert 'data-user-id="user-viewer"' in response.text
    assert "viewer-a@example.test" in response.text
    assert 'id="save-unlock-access"' in response.text


def test_viewer_access_checkbox_reflects_no_existing_grant_by_default(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-user-id="user-viewer"' in response.text
    assert 'data-user-id="user-viewer" checked' not in response.text


def test_viewer_access_checkbox_reflects_an_existing_grant(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as conn:
            conn.execute("UPDATE customer_camera_permissions SET can_unlock=1 WHERE user_id='user-viewer' AND camera_id='cam-door'")
            conn.commit()
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-user-id="user-viewer" checked' in response.text


def test_viewer_access_section_never_shows_for_a_not_yet_configured_camera(client):
    response = client.get("/customer/cameras/cam-plain/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    # The heading's own HTML text (em dash) never collides with the
    # page's shared JS comment about this same feature, which uses a
    # deliberately different phrasing/punctuation.
    assert "Viewer access — who can press Unlock Door" not in response.text
    assert 'id="save-unlock-access"' not in response.text


def test_viewer_never_sees_the_viewer_access_section(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie()})
    assert response.status_code == 200
    assert "Viewer access — who can press Unlock Door" not in response.text
    # The '.unlock-viewer-toggle' class is always referenced by the
    # page's shared JS (a harmless querySelectorAll over zero elements
    # when this section didn't render) -- the assertion targets an
    # actual rendered checkbox instead of that always-present selector.
    assert 'data-user-id="user-viewer"' not in response.text


def test_viewer_access_shows_an_empty_state_when_the_account_has_no_viewers(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as conn:
            conn.execute("DELETE FROM customer_camera_permissions WHERE user_id='user-viewer'")
            conn.execute("DELETE FROM partner_users WHERE id='user-viewer'")
            conn.commit()
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Viewer access" in response.text
    assert "No team members yet" in response.text
    assert 'id="save-unlock-access"' not in response.text


def test_viewer_access_section_posts_to_the_real_unlock_access_route(client):
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "/api/customer/cameras/${cameraId}/door-config/unlock-access" in response.text
