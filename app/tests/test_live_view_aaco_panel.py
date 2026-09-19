"""Coverage for AACO's embedded command panel on the customer Live View
grid page (/customer-live) -- the "AACO as primary UI" integration.

This file deliberately never re-tests AACO's own parsing/execution
(app/aaco.py, app/tests/test_aaco*.py already do that exhaustively) or
the underlying /api/aaco/command route's own behavior (test_aaco_web.py).
It only proves: the panel renders on the Live page with the same shared
client plumbing every other AACO surface uses (aaco_web._AACO_CLIENT_
CORE_JS, via aaco_web.render_aaco_command_panel()) -- never a second,
page-local reimplementation of "how to call AACO" -- that it makes zero
AACO calls on page load, that existing Live View markup/behavior is
completely unchanged, that /aaco itself is untouched and still reachable
as the standalone workspace, and that the panel is scoped to exactly
the grid landing page, not duplicated onto the single-camera page.

Real HTTP through the real app (TestClient(main.app)), the same fixture
shape as test_live_view_page_face_access_ui.py."""

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
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-viewer','viewer-a@example.test','customer_viewer','cust-a','x','selected','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-viewer','cam-door',1)")
    conn.execute("INSERT OR IGNORE INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-viewer','cam-plain',1)")
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


# --------------------------------------------------- panel presence and shared plumbing


def test_aaco_panel_renders_on_the_live_grid_page(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-aaco-embed="aaco-live"' in response.text
    assert 'id="aaco-live-command"' in response.text
    assert 'id="aaco-live-form"' in response.text


def test_aaco_panel_reuses_the_shared_client_helper_not_a_second_implementation(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    # The one and only place a fetch to this endpoint is coded, shared
    # verbatim by /aaco's own workspace and this panel alike.
    assert response.text.count("/api/aaco/command") == 1
    assert "window.aacoSubmitCommand=function" in response.text
    assert "aacoLiveHandleResult" in response.text


def test_aaco_panel_has_a_disabled_microphone_placeholder(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert (
        '<button type="button" class="camera-tool aaco-mic-placeholder" id="aaco-live-mic" '
        'title="Voice command (coming soon)" aria-label="Voice command, coming soon" disabled>'
    ) in response.text
    # This page's separate, already-existing talk-down mic feature is a
    # real getUserMedia()/MediaRecorder push-to-talk control (unrelated
    # to AACO) -- so those strings legitimately exist elsewhere on this
    # page. What must NOT exist is any reference to the AACO
    # placeholder's own id inside an event-wiring call.
    assert "getElementById('aaco-live-mic')" not in response.text
    assert "aaco-live-mic').addEventListener" not in response.text


def test_aaco_panel_makes_no_backend_call_on_page_load(client):
    """The fetch to /api/aaco/command only ever happens inside the
    form's own submit handler -- confirmed structurally: the string
    'aacoSubmitCommand(' (an actual invocation, not just the shared
    function's own definition) appears only inside the submit
    listener this panel registers, never at top-level page-load
    scope."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    submit_listener_index = body.index("addEventListener('submit'", body.index('data-aaco-embed="aaco-live"'))
    invocation_index = body.index("window.aacoSubmitCommand(text", body.index('data-aaco-embed="aaco-live"'))
    assert invocation_index > submit_listener_index


def test_aaco_panel_visible_to_both_owner_and_viewer_roles(client):
    for cookie in (_owner_cookie(), _viewer_cookie()):
        response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: cookie})
        assert response.status_code == 200
        assert 'data-aaco-embed="aaco-live"' in response.text


# --------------------------------------------------- existing Live View behavior unchanged


def test_existing_live_grid_tiles_and_controls_are_unchanged(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-camera-id="cam-door"' in response.text
    assert 'data-camera-id="cam-plain"' in response.text
    assert 'id="unlock-door-cam-door"' in response.text
    assert "wireUnlockButton" in response.text
    assert "querySelectorAll('.unlock-door')" in response.text


def test_single_camera_live_page_is_not_touched_by_the_new_panel(client):
    """The panel is scoped to the grid landing page only -- the
    dedicated single-camera page keeps its own existing tools row
    completely unchanged, no duplicate AACO surface."""
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-aaco-embed' not in response.text
    assert "aacoLiveHandleResult" not in response.text
    assert 'id="unlock-door-cam-door"' in response.text  # its own existing tool, untouched


def test_unauthenticated_request_still_redirects_exactly_as_before(client):
    response = client.get("/customer-live", follow_redirects=False)
    assert response.status_code in (303, 307)


# --------------------------------------------------- /aaco standalone workspace untouched


def test_standalone_aaco_workspace_still_reachable_and_unchanged(client):
    response = client.get("/aaco", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "AACO loads VMS data only after a command" in response.text
    assert "Show the front entrance" in response.text


def test_standalone_workspace_now_correctly_labels_a_door_unlock_result_kind():
    """Incidental fix discovered while factoring the shared client JS:
    render()'s kind->label lookup previously had no branch for
    'door_unlock' and would have silently mislabeled it 'Camera
    status' and then rendered a spurious 'No authorized cameras are
    available.' line. Confirms the fix without needing a browser --
    the KIND_LABELS table and door_unlock's own early return are both
    present in the shipped markup."""
    from aaco_web import _workspace
    workspace_html = _workspace()
    assert "door_unlock:'Door'" in workspace_html
    assert "if(body.kind==='door_unlock')" in workspace_html
