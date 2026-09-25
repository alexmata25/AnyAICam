"""Playback calendar/date-picker reconciliation + Previous/Next Day
(2026-09-03): source-structure tests for _render_customer_playback().

Background: Samsung's own live main.py already carries the full,
accepted date-navigation feature -- <input type="date">, the Today
button, available-recording-date chips (renderAvailableDates(), backed
by GET /api/customer/recordings/{camera_id}/dates ->
_customer_recording_dates()), date-driven recording load
(loadRecordingsForDate(), backed by _customer_recordings_for_date() /
_local_date_bounds_to_utc()), the exact selected-date 00:00-24:00
timeline, exact-time seeking within that date, and DST-safe local-time
date bounds (see _local_date_bounds_to_utc()'s own 2026 America/Chicago
DST-boundary tests). None of that is touched by this change -- every
test below that references those pieces is asserting they are still
present and byte-identical, not re-testing them from scratch.

Genuinely new in this change: explicit "<- Previous Day" / "Next Day
->" buttons (previously nonexistent anywhere, Samsung included), and a
fix so a camera switch preserves the currently-selected date instead
of always silently dropping back to the default view (see
test_camera_switch_preserves_selected_date below).

The pure, DOM-free date-shift formula (shiftedDateString()) is tested
for real, including both 2026 America/Chicago DST transition days, via
Node in test_playback_date_navigation.mjs/_js.py -- see those files.
This file covers everything provable from a plain render + string
assertions on the real, unmodified output, matching this suite's
established convention (test_playback_autoplay_most_recent.py,
test_playback_analytics_lanes.py).

Same import-inside-container constraint and _fake_request()/
_render_customer_playback() calling convention as this suite's other
Playback tests.
"""

from types import SimpleNamespace

import main


def _fake_request(t=None, camera=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: {"t": t, "camera": camera}.get(key, default)))


def _render(monkeypatch, recordings=None, events=None):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: recordings or [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: events or [])
    # This branch's own accepted (git-only) thumbnail/event-clip-player
    # work has _render_customer_playback() also call _customer_camera_
    # events() directly for the timeline's own event markers (a real,
    # already-existing DB call, separate from _customer_detection_
    # events() above) -- mocked the same way so this test suite never
    # needs a real detection_events/detection_event_media schema.
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: events or [])
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())


# ---------------------------------------------------------------------------
# 1. Selecting a historical date (e.g. 08/21/2026) -- the existing date
#    input, wired to the existing loadRecordingsForDate(), unchanged.
# ---------------------------------------------------------------------------

def test_selecting_a_historical_date_via_the_date_input(monkeypatch):
    html = _render(monkeypatch)
    assert '<input id="playback-date-input" type="date" autocomplete="off">' in html  # 2026-09-25: no browser-restored stale date
    assert "dateInput.addEventListener('change',()=>{" in html
    # 2026-09-25: a pick goes through the day controller (desktop keeps it;
    # mobile has no picker), which loads it via loadRecordingsForDate().
    assert "dayController.selectDate(dateInput.value);" in html


# ---------------------------------------------------------------------------
# 2. Today -- unchanged: returns to the default/most-recent view.
# ---------------------------------------------------------------------------

def test_today_button_goes_to_todays_date(monkeypatch):
    """Changed deliberately 2026-09-25: Today used to switch to the undated
    "most recent recordings" view, which on a quiet day or an Event-mode
    camera showed (and preselected) an older day. It now loads today's
    recordings in the viewer's local timezone."""
    html = _render(monkeypatch)
    assert '<button id="playback-date-today" type="button" class="ghost-button">Today</button>' in html
    idx = html.index("dateTodayButton.addEventListener('click',()=>{")
    block = html[idx: idx + 120]
    assert "dayController.goToday();" in block
    assert "viewingDate=null;" not in block


# ---------------------------------------------------------------------------
# 3 & 4. Previous/Next Day buttons exist and are wired through the same
#    loadRecordingsForDate() path as every other date-selection method.
# ---------------------------------------------------------------------------

def test_previous_and_next_day_buttons_exist_and_are_wired(monkeypatch):
    html = _render(monkeypatch)
    assert '<button id="playback-date-prev" type="button" class="ghost-button" aria-label="Previous day">' in html
    assert '<button id="playback-date-next" type="button" class="ghost-button" aria-label="Next day">' in html
    assert "datePrevButton.addEventListener('click',()=>navigateByOneDay(-1));" in html
    assert "dateNextButton.addEventListener('click',()=>navigateByOneDay(1));" in html
    idx = html.index("function navigateByOneDay(deltaDays){")
    block = html[idx: idx + 500]
    assert "dayController.selectDate(target);" in block
    assert "viewingDate||localDateStringOf(new Date())" in block, "must default to today's local date when no date is selected yet"
    assert "target>dateInput.max" in block, "must not navigate into the future, matching the date input's own existing max="


# ---------------------------------------------------------------------------
# 5 & 6. Date with recordings vs. without -- loadRecordingsForDate()'s own
#    existing, honest status text for both cases, unchanged. No silent
#    fallback to another day anywhere in this function.
# ---------------------------------------------------------------------------

def test_date_with_and_without_recordings_status_text(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function loadRecordingsForDate(cameraId,date){")
    body = html[idx: html.index("dateInput.addEventListener", idx)]
    assert "`${clips.length} recording(s) found for ${date}. Select one, or a point on the timeline, to play.`" in body
    assert "`No recordings are available for ${date}.`" in body
    # No code path in this function ever reassigns `date` or calls
    # loadRecordingsForDate with a different date after a fetch --
    # an empty result must never silently jump elsewhere.
    assert body.count("loadRecordingsForDate(") <= 1  # only its own definition context, no self-redirect


# ---------------------------------------------------------------------------
# 7. Camera switch preserves the selected date "when possible" -- the
#    genuine fix in this change (previously always reset to the default
#    view regardless of an active date).
# ---------------------------------------------------------------------------

def test_camera_switch_preserves_selected_date(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("cameraTiles.forEach(tile=>{")
    block = html[idx:html.index("let lastTap=0;", idx)]
    # 2026-09-25: desktop keeps the viewed date; mobile always stays on today.
    assert "if(viewingDate||isPlaybackMobile()){" in block
    assert "loadRecordingsForDate(selectedCameraId,isPlaybackMobile()?localDateStringOf(new Date()):viewingDate)" in block
    assert "}else{" in block
    assert "await renderCamera();" in block, "the original default-view behavior must still run when no date is selected"
    # The old unconditional reset must be gone.
    assert "viewingDate=null;\n      dateInput.value='';" not in block


# ---------------------------------------------------------------------------
# 8. Filters preserve the selected date -- pre-existing behavior,
#    confirmed untouched by this change.
# ---------------------------------------------------------------------------

def test_filters_preserve_selected_date_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert "viewingDate?eventsForLocalDate(analyticsByCamera[selectedCameraId]||[],viewingDate):(analyticsByCamera[selectedCameraId]||[])" in html


# ---------------------------------------------------------------------------
# 9. DST transition dates -- proven for real (execution, not string
#    assertions) in test_playback_date_navigation.mjs/_js.py. This test
#    only confirms the extraction markers this change added are present
#    so that suite can actually find the code.
# ---------------------------------------------------------------------------

def test_date_nav_core_markers_present_for_dst_js_tests(monkeypatch):
    html = _render(monkeypatch)
    assert "// === DATE_NAV_CORE_START ===" in html
    assert "// === DATE_NAV_CORE_END ===" in html
    assert "function shiftedDateString(dateString,deltaDays){" in html


# ---------------------------------------------------------------------------
# 10. Exact timeline seek on a historical date -- the ruler click-to-
#    exact-time handler was already date-aware before this change (it
#    resolved the clicked pixel fraction against viewingDate, the same
#    selected-date state loadRecordingsForDate()/Previous/Next Day all
#    read and write). 2026-09-15: superseded by the timeline-scrubbing
#    feature's unified click/drag core (resolveScrubTarget(), see
#    test_playback_timeline_scrubbing.py) -- the click gesture itself,
#    and its date-mode awareness, are preserved; only the underlying
#    resolution function changed (deliberately, gap-strict now, not
#    findClipNear()'s "nearest within 5 minutes" leniency -- see that
#    function's own updated docstring for why). currentTimelineDayString()
#    is the exact same viewingDate-or-today expression this test always
#    asserted, just factored into its own named function.
# ---------------------------------------------------------------------------

def test_exact_timeline_seek_still_resolves_against_the_viewed_date(monkeypatch):
    html = _render(monkeypatch)
    assert "function currentTimelineDayString(){" in html
    idx = html.index("function currentTimelineDayString(){")
    block = html[idx: idx + 120]
    assert "return viewingDate||localDateStringOf(new Date());" in block, (
        "the seek base-day resolution (already reading viewingDate before the original date-navigation change) must still be date-mode-aware"
    )
    assert "timelineLane.addEventListener('click',(event)=>{" in html
    click_idx = html.index("timelineLane.addEventListener('click',(event)=>{")
    click_block = html[click_idx: click_idx + 400]
    assert "seekToTimelineFraction(timelineFractionFromClientX(event.clientX),{autoplay:true,announceGap:true});" in click_block


# ---------------------------------------------------------------------------
# 11. Segment chaining on a historical date -- the chaining core is
#    completely untouched by this change; it operates on currentClips/
#    the active video element, not on viewingDate.
# ---------------------------------------------------------------------------

# Automatic segment chaining (_planNextChainedClip()/CHAIN_CORE) was
# reconciled onto this branch in a follow-up commit -- deliberately
# narrower than Samsung's own (no playSequencer: this branch's
# recordingMediaUrl() is synchronous, so there's no async URL-resolution
# gap for a sequencer to guard). See test_playback_segment_chaining.py
# for the full reconciliation. This test now runs for real, confirming
# what its deferral reasoning always claimed: the chain core stays
# date-mode-agnostic, so reconciling it required no changes here at all.
def test_segment_chaining_unaffected_by_date_mode(monkeypatch):
    html = _render(monkeypatch)
    assert "// === CHAIN_CORE_START ===" in html
    assert "// === CHAIN_CORE_END ===" in html
    idx = html.index("// === CHAIN_CORE_START ===")
    end_idx = html.index("// === CHAIN_CORE_END ===", idx)
    chain_core = html[idx:end_idx]
    assert "viewingDate" not in chain_core, "the chaining core must stay date-mode-agnostic"
