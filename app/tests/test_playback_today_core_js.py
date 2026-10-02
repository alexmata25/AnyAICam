"""Playback opens on the viewer's today; mobile is a self-refreshing
"latest recordings" view (2026-09-25).

Root cause of "Playback opens on an older date" (desktop and phone):
the page opened in an undated "most recent N recordings" view with an
EMPTY date input and preselected the last clip in that list. With cameras
in Event mode, or on any quiet day, the most recent recordings are from
earlier days, so an older day was shown and preselected while the
timeline showed an empty today. Browsers could also restore a previously
picked date into the date input (no autocomplete="off"), and the
server-side "today" is the appliance's day, not the viewer's.

The behavior is driven by PLAYBACK_TODAY_CORE in main.py, executed for
real under Node in several timezones by test_playback_today_core.mjs."""
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import main

HERE = Path(__file__).resolve().parent
MAIN_PY = HERE.parent / "main.py"
JS_TEST = HERE / "test_playback_today_core.mjs"
NODE = shutil.which("node")

# The same instant, 2026-09-26T03:30:00Z, is a different local day per zone.
TIMEZONES = [("America/Chicago", "2026-09-25"), ("America/Los_Angeles", "2026-09-25"),
             ("UTC", "2026-09-26"), ("Asia/Kolkata", "2026-09-26"), ("Pacific/Auckland", "2026-09-26")]


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
@pytest.mark.parametrize("tz,expected_today", TIMEZONES)
def test_playback_day_core_in_real_timezones(tz, expected_today):
    import os
    result = subprocess.run([NODE, str(JS_TEST), str(MAIN_PY), expected_today], capture_output=True, text=True,
                            timeout=60, env=dict(os.environ, TZ=tz))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"TZ={tz}" in result.stdout


def _render(monkeypatch, request_params=None):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: [])
    params = request_params or {}
    request = SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: params.get(key, default)))
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], request)


def _script(html):
    return [s for s in re.findall(r"<script>(.*?)</script>", html, re.S) if "createPlaybackDayController" in s][0]


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
def test_the_rendered_page_script_is_valid_javascript(monkeypatch, tmp_path):
    for script in re.findall(r"<script>(.*?)</script>", _render(monkeypatch), re.S):
        path = tmp_path / "page.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr


def test_a_stale_date_from_a_previous_session_can_never_be_restored(monkeypatch):
    html = _render(monkeypatch)
    assert '<input id="playback-date-input" type="date" autocomplete="off">' in html
    assert "dateInput.value='';  // never a date the browser restored" in _script(html)


def test_first_open_goes_to_the_viewers_today_and_deep_links_still_open_their_event(monkeypatch):
    script = _script(_render(monkeypatch))
    boot = script[script.index("dayController=createPlaybackDayController("):]
    assert "dayController.open();" in boot
    assert "if(initialEventId||initialTimestamp){" in boot and "renderCamera(initialTimestamp)" in boot
    assert "dayController.open({skipLoad:true});" in boot  # deep link keeps its event; mobile still refreshes today


def test_mobile_hides_the_date_controls_at_the_same_breakpoint_as_the_layout(monkeypatch):
    html = _render(monkeypatch)
    # 2026-09-25: phones get the compact calendar too (no longer hidden).
    assert "#playback-date-bar,#playback-available-dates{display:none" not in html
    assert '<div class="playback-date-row" id="playback-date-bar">' in html
    assert "window.matchMedia('(max-width:900px)')" in _script(html)


def test_today_and_manual_picks_go_through_the_day_controller(monkeypatch):
    script = _script(_render(monkeypatch))
    # Only the calendar remains (Previous/Today/Next removed 2026-09-25).
    assert "dayController.selectDate(dateInput.value);" in script
    assert "dateTodayButton" not in script and "navigateByOneDay" not in script


def test_the_refresh_never_touches_the_player(monkeypatch):
    script = _script(_render(monkeypatch))
    body = script[script.index("function applyRefreshedClips("):script.index("dayController=createPlaybackDayController(")]
    for forbidden in ("video.pause(", "video.play(", "removeAttribute('src')", "video.load(", "playClip(", "selectedClip=", "currentTime"):
        assert forbidden not in body, forbidden
    assert "refreshDay:async date=>" in script and "loadRecordingsForDate" not in script[script.index("refreshDay:async"):script.index("onClips:applyRefreshedClips")]


def test_the_mobile_events_poll_renders_the_latest_clips_and_only_the_viewed_day(monkeypatch):
    script = _script(_render(monkeypatch))
    assert "state.clips=clips;" in script
    assert "renderMobileRecentEvents(cameraId,state.clips||clips,viewingDate?eventsForLocalDate(state.events,viewingDate):state.events);" in script


def test_mobile_camera_switch_keeps_the_viewed_day(monkeypatch):
    """Today by default; a date picked on the phone's calendar carries
    across a camera switch, same as desktop (2026-09-25)."""
    script = _script(_render(monkeypatch))
    assert "await loadRecordingsForDate(selectedCameraId,viewingDate||localDateStringOf(new Date()))" in script


def test_no_timezone_is_hardcoded_in_the_day_logic():
    source = MAIN_PY.read_text(encoding="utf-8").replace("\r\n", "\n")
    core = source[source.index("// === PLAYBACK_TODAY_CORE_START ==="):source.index("// === PLAYBACK_TODAY_CORE_END ===")]
    for forbidden in ("America/", "Chicago", "Houston", "timeZone", "getTimezoneOffset", "toISOString", "UTC"):
        assert forbidden not in core, forbidden
