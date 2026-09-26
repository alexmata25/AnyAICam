"""Playback timeline hover/scrub time readout (2026-09-23, customer-
reported: the timeline had no way to see the exact hour/minute/second
position being selected while hovering or scrubbing left/right).

Same source-structure testing convention as test_playback_timeline_
scrubbing.py (this file's own sibling): _render_customer_playback()'s
returned HTML is inspected directly for the real DOM/JS this feature
adds, not a browser-rendered DOM -- see that file's own docstring for
why (real-execution proof of the pure SCRUB_CORE logic lives in
test_playback_timeline_scrubbing_core.mjs instead; this fix adds no
new pure logic of its own, only a persistent DOM element + one
mousemove/mouseleave pair, reusing timelineFractionFromClientX()/
timelineFractionToLocalMs()/currentTimelineDayString() unchanged).
"""

from types import SimpleNamespace

import main


def _fake_request(t=None, camera=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: {"t": t, "camera": camera}.get(key, default)))


def _render(monkeypatch, recordings=None, events=None):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: recordings or [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: events or [])
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: events or [])
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())


def test_hover_tooltip_element_created_once_and_hidden_by_default(monkeypatch):
    html = _render(monkeypatch)
    assert "const timelineHoverTooltip=document.createElement('div');" in html
    assert "timelineHoverTooltip.className='timeline-hover-tooltip';" in html
    assert "timelineHoverTooltip.hidden=true;" in html


def test_hover_tooltip_reappended_after_every_timeline_render_same_as_playhead(monkeypatch):
    """Must survive renderTimeline()'s own timelineLane.innerHTML=''
    clear on every camera/date switch, exactly like the pre-existing
    playheadEl already does -- a persistent element re-created per
    render would lose no functionality here, but would silently
    duplicate its own mousemove/mouseleave listeners on every render
    if it were (wrongly) re-created instead of reused; this proves it
    is the same create-once, re-append pattern, not a new one."""
    html = _render(monkeypatch)
    idx = html.index("function renderTimeline(cameraId,clips,events,dayString){")
    block = html[idx: idx + 700]
    assert "timelineLane.innerHTML='';" in block
    assert "timelineLane.appendChild(playheadEl);" in block
    assert "timelineLane.appendChild(timelineHoverTooltip);" in block
    assert block.index("timelineLane.innerHTML='';") < block.index("timelineLane.appendChild(timelineHoverTooltip);"), (
        "must be re-appended AFTER the clear, or it would be immediately wiped out"
    )


def test_mousemove_shows_exact_time_using_the_existing_scrub_core(monkeypatch):
    """Reuses timelineFractionFromClientX()/timelineFractionToLocalMs()/
    currentTimelineDayString() completely unchanged -- no second,
    parallel time-computation path -- so the displayed hover time can
    never disagree with where a click/drag at that same position would
    actually seek to."""
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('mousemove',(event)=>{")
    block = html[idx: idx + 500]
    assert "const fraction=timelineFractionFromClientX(event.clientX);" in block
    assert "const ms=timelineFractionToLocalMs(currentTimelineDayString(),fraction);" in block
    assert "timelineHoverTooltip.textContent=formatTimelineClockTime(ms);" in block
    assert "timelineHoverTooltip.style.left=(fraction*100)+'%';" in block
    assert "timelineHoverTooltip.hidden=false;" in block


def test_mouseleave_hides_the_tooltip(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('mouseleave',()=>{")
    block = html[idx: idx + 100]
    assert "timelineHoverTooltip.hidden=true;" in block


def test_format_timeline_clock_time_is_zero_padded_hh_mm_ss(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function formatTimelineClockTime(ms){")
    end = html.index("\n  }", idx)
    block = html[idx:end]
    assert "new Date(ms)" in block
    assert "getHours()).padStart(2,'0')" in block
    assert "getMinutes()).padStart(2,'0')" in block
    assert "getSeconds()).padStart(2,'0')" in block


def test_hover_tooltip_is_pointer_events_none_same_as_the_playhead(monkeypatch):
    """Must never intercept the drag/click handlers meant for the lane
    underneath it -- same reason positionPlayhead()'s own element
    already is (see .timeline-playhead's own CSS comment)."""
    html = _render(monkeypatch)
    idx = html.index(".timeline-hover-tooltip{")
    block = html[idx: idx + 300]
    assert "pointer-events:none" in block


def test_mousemove_listener_is_registered_after_the_existing_drag_handlers_are_defined(monkeypatch):
    """Must come after pointerdown/pointermove/pointerup/click are all
    wired -- proves this is purely additive (a new, independent
    listener) rather than replacing or reordering any of the existing
    drag-to-scrub wiring test_playback_timeline_scrubbing.py already
    covers."""
    html = _render(monkeypatch)
    assert html.index("timelineLane.addEventListener('pointerdown',") < html.index("timelineLane.addEventListener('mousemove',")
    assert html.index("timelineLane.addEventListener('click',") < html.index("timelineLane.addEventListener('mousemove',")
