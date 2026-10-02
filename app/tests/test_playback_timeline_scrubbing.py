"""Playback timeline scrubbing (2026-09-15, customer request: "a true
video scrubbing timeline, like a professional VMS"): source-structure
tests for _render_customer_playback().

Real behavior:
- Click OR drag the playhead anywhere on the ruler seeks the video to
  that recorded moment. Moving left/right moves backward/forward in
  recorded time.
- The playhead stays synchronized with the video while it plays (a
  video 'timeupdate' listener, not just the gesture that started it).
- Recorded portions of the selected local day are what the pre-existing
  segment bars already show; scrubbing into a gap (no covering
  recording) never pretends video exists -- it pauses, shows a clear
  status message, and (on the gesture's real endpoint) the same toast
  the old click handler already used.
- Crossing between two different recording files (adjacent or
  separated by a real gap) is resolved automatically via the existing
  playClip() media-load path -- no new recording pipeline, no clip
  generation, nothing duplicated.

resolveScrubTarget()/coveringClipAt()/timelineFractionToLocalMs() are
the pure, DOM-free decision core (=== SCRUB_CORE_START/END === markers)
shared by both the click and the drag handlers -- see
test_playback_timeline_scrubbing_core.mjs for real-execution proof of
their actual logic (gap detection, offset math, day-boundary edges).
This file proves the surrounding wiring: which DOM state each gesture
reads/writes, and that the pre-existing discovery/local-date fixes
(de56b83) are unaffected.

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
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: events or [])
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())


# ---------------------------------------------------------------------------
# The pure SCRUB_CORE block exists and is extractable (its own markers,
# matching CHAIN_CORE/DATE_NAV_CORE/LANE_CORE's established convention).
# ---------------------------------------------------------------------------

def test_scrub_core_markers_and_functions_present(monkeypatch):
    html = _render(monkeypatch)
    assert "// === SCRUB_CORE_START ===" in html
    assert "// === SCRUB_CORE_END ===" in html
    idx = html.index("// === SCRUB_CORE_START ===")
    end = html.index("// === SCRUB_CORE_END ===")
    core = html[idx:end]
    assert "function timelineFractionToLocalMs(dayString,fraction){" in core
    assert "function coveringClipAt(clips,timestampMs,parseDate){" in core
    assert "function resolveScrubTarget(clips,dayString,fraction,parseDate){" in core
    # Deliberately NOT findClipNear()'s "nearest within 5 minutes"
    # leniency -- a scrub bar must say "gap", never snap to something
    # nearby and pretend video exists there.
    assert "findClipNear" not in core


# ---------------------------------------------------------------------------
# Click-to-seek: still date-mode-aware, still routes through the shared
# core, still plays (matching the pre-existing "click always plays"
# contract) and announces a genuine gap via the pre-existing toast.
# ---------------------------------------------------------------------------

def test_click_to_seek_uses_the_shared_core_and_still_autoplays(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('click',(event)=>{")
    block = html[idx: idx + 400]
    assert "if(justDragged){justDragged=false;event.stopPropagation();event.preventDefault();return;}" in block
    assert "if(event.target!==timelineLane)return;" in block
    assert "seekToTimelineFraction(timelineFractionFromClientX(event.clientX),{autoplay:true,announceGap:true});" in block
    assert "{capture:true}" in html  # the click listener must run in capture phase for the justDragged guard to work


def test_seek_to_timeline_fraction_pauses_and_announces_a_genuine_gap(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function seekToTimelineFraction(fraction,options){")
    end = html.index("\n  }", idx)
    block = html[idx:end]
    assert "if(!covering){" in block
    assert "video.pause();" in block
    assert "status.textContent='No recording available at this time.';" in block
    assert "if(announceGap&&typeof showToast==='function')showToast('No recording available at that time.');" in block


def test_seek_to_timeline_fraction_switches_files_automatically(monkeypatch):
    """Crossing into a different (possibly non-adjacent) recording file
    is resolved without the customer picking one manually -- reuses the
    existing playClip() media-load path, never a second one, and never
    touches recording_uploader.py/the upload pipeline."""
    html = _render(monkeypatch)
    idx = html.index("function seekToTimelineFraction(fraction,options){")
    block = html[idx: idx + 1600]
    assert "if(selectedClip&&selectedClip.id===covering.id){" in block, "same-file scrubbing must be an instant local seek, no reload"
    assert "video.currentTime=offsetSeconds;" in block
    assert "playClip(selectedCameraId,covering,{autoplay});" in block
    assert "video.addEventListener('loadedmetadata',()=>{video.currentTime=offsetSeconds;},{once:true});" in block


# ---------------------------------------------------------------------------
# Drag-to-scrub: threshold-gated, rAF-throttled live updates, autoplay
# suppressed mid-drag, exact final position always applied on release.
# ---------------------------------------------------------------------------

def test_pointerdown_arms_the_gesture_without_starting_a_scrub(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('pointerdown',(event)=>{")
    block = html[idx: idx + 300]
    assert "scrubStartClientX=event.clientX;" in block
    assert "scrubHasDragged=false;" in block
    assert "scrubWasPlayingBeforeDrag=!video.paused&&!video.ended;" in block
    # Arming a gesture must never itself flip isScrubbing/seek anything.
    assert "isScrubbing" not in block
    assert "seekToTimelineFraction" not in block


def test_pointermove_below_threshold_does_not_start_scrubbing(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('pointermove',(event)=>{")
    block = html[idx: idx + 500]
    assert "if(Math.abs(event.clientX-scrubStartClientX)<SCRUB_DRAG_THRESHOLD_PX)return;" in block
    assert "scrubHasDragged=true;" in block
    assert "isScrubbing=true;" in block


def test_pointermove_live_updates_never_autoplay_or_announce(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('pointermove',(event)=>{")
    block = html[idx: idx + 900]
    assert "requestAnimationFrame(()=>{" in block
    assert "seekToTimelineFraction(timelineFractionFromClientX(scrubLatestClientX),{autoplay:false,announceGap:false});" in block


def test_pointermove_is_raf_throttled(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('pointermove',(event)=>{")
    block = html[idx: idx + 900]
    assert "if(scrubRafPending)return;" in block
    assert "scrubRafPending=true;" in block
    assert "scrubRafPending=false;" in block
    # Live drag positions are throttled; the gesture's real endpoint
    # (endTimelineScrub, below) deliberately is not.
    assert "if(!isScrubbing)return;" in block


def test_drag_release_applies_final_position_synchronously_and_resumes_only_if_it_was_playing(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function endTimelineScrub(event){")
    block = html[idx: idx + 900]
    assert "if(scrubHasDragged){" in block
    assert "justDragged=true;" in block
    assert "seekToTimelineFraction(timelineFractionFromClientX(event.clientX),{autoplay:scrubWasPlayingBeforeDrag,announceGap:true});" in block
    assert "scrubHasDragged=false;" in block
    assert "isScrubbing=false;" in block
    assert "timelineLane.addEventListener('pointerup',endTimelineScrub);" in html
    assert "timelineLane.addEventListener('pointercancel',endTimelineScrub);" in html


def test_endtimelinescrub_ignores_a_pointer_that_never_dragged(monkeypatch):
    """A plain click's pointerup must not double-seek -- endTimelineScrub
    only acts when scrubHasDragged is true; the click event (not this
    function) is what handles a genuine plain click."""
    html = _render(monkeypatch)
    idx = html.index("function endTimelineScrub(event){")
    block = html[idx: idx + 200]
    assert "if(scrubStartClientX===null||(event&&event.pointerId!==scrubPointerId))return;" in block


# ---------------------------------------------------------------------------
# Playhead: created once, re-appended every render, synchronized to the
# video during playback, hidden whenever there's nothing real to show.
# ---------------------------------------------------------------------------

def test_playhead_element_created_once_and_hidden_by_default(monkeypatch):
    html = _render(monkeypatch)
    assert "const playheadEl=document.createElement('div');" in html
    assert "playheadEl.className='timeline-playhead';" in html
    assert "playheadEl.hidden=true;" in html


def test_playhead_reappended_after_every_timeline_render(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function renderTimeline(cameraId,clips,events,dayString){")
    block = html[idx: idx + 600]
    assert "timelineLane.innerHTML='';" in block
    assert "timelineLane.appendChild(playheadEl);" in block
    assert block.index("timelineLane.innerHTML='';") < block.index("timelineLane.appendChild(playheadEl);"), (
        "must be re-appended AFTER the clear, or it would be immediately wiped out"
    )


def test_video_timeupdate_keeps_playhead_synchronized(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("video.addEventListener('timeupdate',()=>{")
    block = html[idx: idx + 700]
    assert "if(isScrubbing||!selectedClip)return;" in block, "a live drag must own the playhead, not fight with timeupdate"
    assert "playbackDate(selectedClip.start).getTime()+video.currentTime*1000" in block
    assert "positionPlayhead(fraction,true);" in block


def test_playhead_hidden_on_every_selection_reset(monkeypatch):
    html = _render(monkeypatch)
    # renderCamera() and loadRecordingsForDate() both reset playback
    # state on every load/switch -- the playhead must go with it, same
    # as selectedClip=null already does, so a stale position from a
    # previous camera/date never lingers into the next one.
    assert html.count("selectedClip=null;\n    playheadEl.hidden=true;") >= 2
    # The motion-event clip player's own onReady also hides it -- an
    # event clip is a different media identity than any timeline
    # recording.
    idx = html.index("onReady:()=>{")
    block = html[idx: idx + 500]
    assert "playheadEl.hidden=true;" in block


def test_position_playhead_toggles_gap_styling(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function positionPlayhead(fraction,hasCoverage){")
    block = html[idx: idx + 250]
    assert "playheadEl.hidden=false;" in block
    assert "playheadEl.style.left=(fraction*100)+'%';" in block
    assert "playheadEl.classList.toggle('timeline-playhead--gap',!hasCoverage);" in block
    assert ".timeline-playhead{" in html
    assert ".timeline-playhead--gap{" in html


# ---------------------------------------------------------------------------
# playClip()'s new options.autoplay: every pre-existing trigger keeps
# the exact original always-play behavior (no call site below passes
# options at all except the new scrub path).
# ---------------------------------------------------------------------------

def test_playclip_autoplay_defaults_true_for_every_pre_existing_trigger(monkeypatch):
    html = _render(monkeypatch)
    assert "const autoplay=!options||options.autoplay!==false;" in html
    for unchanged_trigger in [
        "segment.addEventListener('click',()=>playClip(cameraId,clip));",
        "if(next)playClip(selectedCameraId,next);",
    ]:
        assert unchanged_trigger in html, "pre-existing triggers must still call playClip() with no options -- autoplay stays true"
    idx = html.index("function playClip(cameraId,clip,options){")
    end = html.index("\n  }", idx)
    block = html[idx: end]
    assert "if(autoplay){" in block
    assert "video.play().then(()=>{" in block


# ---------------------------------------------------------------------------
# No regression to the discovery/local-date fixes from de56b83.
# ---------------------------------------------------------------------------

def test_discovery_and_local_date_fixes_unaffected(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("async function renderCamera(seekTimestamp){")
    end = html.index("async function playEventClipDeepLink(", idx)
    body = html[idx:end]
    assert "clipPanel.hidden=false;" in body
    assert "const dayString=seekTimestamp?localDateStringOf(playbackDate(seekTimestamp)):localDateStringOf(new Date());" in body
    assert "renderTimeline(cameraId,clips,events,dayString);" in body
    assert "function localDayBoundsToUtcNaiveIso(dateString){" in html
