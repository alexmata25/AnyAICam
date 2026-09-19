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


def test_aaco_playback_and_events_results_actually_navigate_not_just_a_dangling_link(client):
    """2026-09-19 fix: a playback/events AACO result previously only
    ever inserted a manual 'Open Playback'/'Open in Investigate' link
    the customer had to separately notice and click -- unlike 'live',
    which already scrolls/highlights with no click required. Confirms
    the page now actually navigates to the authorized href on its own,
    with the manual link kept only as a fallback."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "window.location.assign(body.href)" in body
    assert "window.location.assign(first.href)" in body
    # The fallback link itself must still be built before navigating,
    # not replaced by it.
    assert "openPlayback.href=body.href" in body
    assert "openEvent.href=first.href" in body


def test_aaco_mic_button_starts_disabled_with_an_unsupported_tooltip(client):
    """The button's own initial HTML is always safe-by-default --
    disabled with an explanatory tooltip -- before any feature
    detection JS has even run, so a browser that never executes the
    script (or one that lacks SpeechRecognition) never shows a mic
    that looks clickable but silently does nothing."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert (
        '<button type="button" class="camera-tool aaco-mic-button" id="aaco-live-mic" '
        'title="Voice commands are not supported in this browser" '
        'aria-label="Voice commands are not supported in this browser" aria-pressed="false" disabled>'
    ) in response.text


def test_aaco_mic_is_feature_detected_and_enabled_only_when_supported(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "window.SpeechRecognition||window.webkitSpeechRecognition" in body
    # The button is only ever re-enabled inside the feature-detected
    # branch -- never unconditionally.
    assert "if(micButton&&SpeechRecognitionCtor){" in body
    assert "micButton.disabled=false" in body


def test_aaco_mic_recognized_speech_goes_through_the_same_form_submit_never_a_second_aaco_call(client):
    """The one and only invocation of window.aacoSubmitCommand in this
    panel is inside the form's own 'submit' listener -- confirmed by
    counting occurrences. Voice recognition writes into the existing
    input and calls form.requestSubmit(), which re-enters that exact
    same listener; it never calls window.aacoSubmitCommand a second
    time on its own, which would be a real second, undisciplined path
    to AACO."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert body.count("window.aacoSubmitCommand(text") == 1
    assert "form.requestSubmit()" in body
    assert "input.value=transcript" in body


def test_aaco_mic_handles_permission_denied_and_no_speech_without_breaking_typing(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "event.error==='not-allowed'" in body
    assert "Microphone permission was denied. Type your command instead." in body
    assert "event.error==='no-speech'" in body
    assert "No audio detected. Check that your microphone is unmuted and the correct input device is selected." in body
    # The typed-command <form> and its own submit listener exist
    # completely independently of whether voice support/permission
    # succeeds -- confirmed by their both being present regardless.
    assert 'id="aaco-live-form"' in body
    assert "form.addEventListener('submit'" in body


def test_aaco_mic_retries_once_automatically_on_a_first_no_speech_error(client):
    """2026-09-19 investigation: a real browser confirmed onstart fires
    (permission granted, recognition genuinely begins) but every
    attempt still ended in a no-speech error -- consistent with the
    recognition window elapsing before the user's speech registered.
    The narrow fix: retry exactly once, silently, before reporting
    failure to the customer, and never drop the visible "listening"
    state across that single retry."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "function attemptRecognition(isRetry)" in body
    assert "if(event.error==='no-speech'&&!isRetry){" in body
    assert "attemptRecognition(true)" in body
    assert "Still listening" in body
    # The retry must not cause the mic to visually flicker back to idle
    # between the two attempts.
    assert "if(!retrying)stopListening()" in body


def test_aaco_mic_shows_interim_transcript_while_still_listening(client):
    """interimResults=true (previously false) gives the customer live
    feedback the moment the recognizer hears anything, distinguishing
    "the mic is picking up audio" from "nothing is reaching it at
    all" -- only a final result fills the input and submits."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "recognition.interimResults=true" in body
    assert "if(!result.isFinal){" in body
    assert "Hearing:" in body


def test_aaco_mic_never_touches_getusermedia_or_mediarecorder_directly(client):
    """SpeechRecognition performs its own microphone capture inside the
    browser -- this page's own code never calls getUserMedia() or
    MediaRecorder itself, unlike this same page's separate, unrelated
    talk-down feature (real push-to-talk camera audio), which
    legitimately does use those APIs elsewhere on this page. The
    assertion is scoped to the AACO panel's own script block so the
    talk-down feature's real, legitimate use of those APIs elsewhere
    on the page can never make this test pass by accident."""
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    panel_script_start = body.index("data-aaco-embed=\"aaco-live\"")
    panel_script_end = body.index("</script>", panel_script_start) + len("</script>")
    panel_script = body[panel_script_start:panel_script_end]
    assert "getUserMedia" not in panel_script
    assert "MediaRecorder" not in panel_script


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
