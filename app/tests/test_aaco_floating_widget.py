"""Coverage for the persistent floating AACO assistant injected once by
the shared customer page shell (main.py's page_shell() ->
aaco_web.render_aaco_floating_widget()) -- the "one primary AACO UI,
everywhere" integration.

2026-09-19: this replaces the earlier, page-specific fixed AACO panel
that used to live only on /customer-live (see the now-trimmed
test_live_view_aaco_panel.py). This file deliberately never re-tests
AACO's own parsing/execution (app/aaco.py, app/tests/test_aaco*.py) or
the underlying /api/aaco/command route's own behavior (test_aaco_web.py)
-- it proves the widget is injected exactly once, on exactly the right
pages, for exactly the right identities, using the one existing shared
client plumbing (aaco_web._AACO_CLIENT_CORE_JS /
render_aaco_command_panel()) rather than a second, page-local
reimplementation.

Real HTTP through the real app (TestClient(main.app)), the same
fixture shape as test_live_view_page_face_access_ui.py /
test_live_view_aaco_panel.py."""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aaco_floating_widget.db"


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


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator", None, None, None)


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


# --------------------------------------------------- presence across the shared shell


CUSTOMER_PAGES = ["/dashboard", "/customer-live", "/playback", "/investigate", "/alerts"]


@pytest.mark.parametrize("path", CUSTOMER_PAGES)
def test_floating_widget_appears_on_every_major_customer_page(client, path):
    """The whole point of injecting this from the shared page_shell()
    rather than page-by-page: it shows up on every normal customer
    page without that page's own code doing anything."""
    response = client.get(path, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-aaco-embed="aaco-float"' in response.text
    assert 'id="aaco-float-toggle"' in response.text
    assert 'id="aaco-float-panel"' in response.text
    assert 'id="aaco-float-command"' in response.text


def test_floating_widget_also_appears_on_the_single_camera_live_page(client):
    """2026-09-19: unlike the old fixed panel (deliberately scoped to
    only the grid landing page), the floating widget is injected by
    the shared shell every normal customer page uses -- the dedicated
    single-camera page is exactly such a page, and now legitimately
    gets it too, on purpose."""
    response = client.get("/customer/cameras/cam-door/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-aaco-embed="aaco-float"' in response.text
    # Its own existing per-camera tools remain untouched alongside it.
    assert 'id="unlock-door-cam-door"' in response.text


@pytest.mark.parametrize("path", CUSTOMER_PAGES)
def test_exactly_one_floating_widget_per_page_never_duplicated(client, path):
    response = client.get(path, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert body.count('data-aaco-embed="aaco-float"') == 1
    assert body.count('id="aaco-float-toggle"') == 1
    # The one and only fetch to this endpoint anywhere on the page --
    # never a second, page-local reimplementation of "how to call AACO".
    assert body.count("/api/aaco/command") == 1
    assert body.count("window.aacoSubmitCommand=function") == 1


def test_floating_widget_visible_to_both_owner_and_viewer_roles(client):
    for cookie in (_owner_cookie(), _viewer_cookie()):
        response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: cookie})
        assert response.status_code == 200
        assert 'data-aaco-embed="aaco-float"' in response.text


# --------------------------------------------------- authorization boundaries preserved


def test_unauthenticated_request_gets_no_customer_aaco_controls(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code in (303, 307)
    # Whatever the redirect response body is, it must never itself leak
    # the floating widget markup.
    assert 'data-aaco-embed="aaco-float"' not in response.text


def test_non_customer_role_never_gets_the_floating_widget(client):
    """The floating widget is gated to CUSTOMER_PORTAL_ROLES -- the
    exact same identities AACO's own /api/aaco/command authorization
    already targets exclusively -- not merely to "any authenticated
    session". Whether an administrator session lands on a 200 or is
    redirected away from a customer-scoped path, the one invariant
    that must hold either way is that it is never served the customer
    AACO widget."""
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()}, follow_redirects=False)
    if response.status_code == 200:
        assert 'data-aaco-embed="aaco-float"' not in response.text
    else:
        assert response.status_code in (303, 307)


def test_standalone_workspace_correctly_labels_a_door_unlock_result_kind():
    """render()'s kind->label lookup must have a branch for
    'door_unlock' (a real, previously-fixed gap) -- confirmed present
    in the shipped standalone workspace markup."""
    from aaco_web import _workspace
    workspace_html = _workspace()
    assert "door_unlock:'Door'" in workspace_html
    assert "if(body.kind==='door_unlock')" in workspace_html


def test_standalone_aaco_workspace_never_also_gets_a_second_floating_widget(client):
    """/aaco is already the full, dedicated AACO workspace -- page_shell()
    explicitly excludes active=="aaco" from the floating widget so a
    customer never sees two AACO surfaces stacked on the same page."""
    response = client.get("/aaco", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "AACO loads VMS data only after a command" in response.text
    assert 'data-aaco-embed="aaco-float"' not in response.text


# --------------------------------------------------- typed/voice commands and results


def test_typed_and_voice_commands_use_the_one_existing_command_path(client):
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    # Typed: the form's submit handler calls the one shared helper.
    assert body.count("window.aacoSubmitCommand(text") == 1
    assert "form.addEventListener('submit'" in body
    # Voice: browser-native SpeechRecognition, feature-detected, and
    # recognized speech re-enters the exact same submit path via
    # form.requestSubmit() -- never a second call to aacoSubmitCommand.
    assert "window.SpeechRecognition||window.webkitSpeechRecognition" in body
    assert "form.requestSubmit()" in body
    assert "getUserMedia" not in body
    assert "MediaRecorder" not in body


def test_navigation_results_still_work_through_the_floating_widget(client):
    """The universal window.aacoFloatHandleResult -- not a page-specific
    handler -- is what makes 'AACO navigates to Playback/Events/Live
    and the assistant remains available on the destination page' true:
    a plain same-origin navigation re-renders page_shell() on whatever
    page it lands on, which injects this exact same widget there
    again."""
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "window.aacoFloatHandleResult=function(body)" in body
    assert "window.location.assign(body.href)" in body
    assert "window.location.assign(first.href)" in body
    assert "aaco-highlight" in body


def test_floating_panel_starts_collapsed_and_toggles_without_consuming_permanent_space(client):
    """Mobile-first requirement: collapsed by default (a single small
    button, not an always-open panel), and the panel itself uses
    viewport-relative sizing rather than a fixed desktop-only width."""
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert '<div id="aaco-float-panel" class="aaco-float-panel" hidden>' in body
    assert "calc(100vw - 24px)" in body
    assert "@media (max-width:480px)" in body


# --------------------------------------------------- mic/voice mechanics (moved from the
# retired page-specific fixed panel -- render_aaco_command_panel() is now only ever
# embedded via this floating widget for customers, so its own voice behavior is
# verified here rather than per-page.


def test_aaco_mic_button_starts_disabled_with_an_unsupported_tooltip(client):
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert (
        '<button type="button" class="camera-tool aaco-mic-button" id="aaco-float-mic" '
        'title="Voice commands are not supported in this browser" '
        'aria-label="Voice commands are not supported in this browser" aria-pressed="false" disabled>'
    ) in response.text


def test_aaco_mic_is_feature_detected_and_enabled_only_when_supported(client):
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "if(micButton&&SpeechRecognitionCtor){" in body
    assert "micButton.disabled=false" in body


def test_aaco_mic_handles_permission_denied_and_no_speech_without_breaking_typing(client):
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "event.error==='not-allowed'" in body
    assert "Microphone permission was denied. Type your command instead." in body
    assert "event.error==='no-speech'" in body
    assert "No audio detected. Check that your microphone is unmuted and the correct input device is selected." in body


def test_aaco_mic_retries_once_automatically_on_a_first_no_speech_error(client):
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "function attemptRecognition(isRetry)" in body
    assert "if(event.error==='no-speech'&&!isRetry){" in body
    assert "attemptRecognition(true)" in body


def test_aaco_mic_shows_interim_transcript_while_still_listening(client):
    response = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "recognition.interimResults=true" in body
    assert "if(!result.isFinal){" in body


# --------------------------------------------------- existing behavior unchanged elsewhere


def test_existing_live_grid_tiles_and_controls_are_unchanged(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'data-camera-id="cam-door"' in response.text
    assert 'data-camera-id="cam-plain"' in response.text
    assert 'id="unlock-door-cam-door"' in response.text
    assert "wireUnlockButton" in response.text
    assert "querySelectorAll('.unlock-door')" in response.text


def test_floating_button_sits_above_the_phone_tab_bar(client):
    """2026-09-25: on phones the button (right:10px;bottom:10px) covered the
    bottom tab bar's last tab (Account); where the bar exists it now sits
    above it, and the Analytics submenu opens above the button."""
    html = client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert "mobile-nav" in html
    assert "@media (max-width:760px){body:has(.mobile-nav) .aaco-float-root{bottom:84px}" in html
    assert ".mobile-analytics-sheet{position:fixed;z-index:10000;" in html or "mobile-analytics-sheet" not in html
