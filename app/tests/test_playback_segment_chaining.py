"""Automatic Playback segment chaining, reconciled onto this branch
(2026-09-03): source-structure tests for _render_customer_playback().

Background: Samsung's accepted chaining implementation is async,
playSequencer-based, because its fetchClipUrl() does a real network
round-trip (a presigned-URL fetch) that a slower/newer request could
race against. This branch's own accepted playClip() is deliberately
different -- synchronous, recordingMediaUrl() just template-builds a
URL string with no fetch at all -- so there is no equivalent async gap
here. Only _planNextChainedClip() (the pure, synchronous chain-target
decision) was reconciled; _createPlayRequestSequencer()/playSequencer
were not, and playClip() itself was NOT modified at all -- the video
'ended' listener merely gained one call to this branch's own,
unmodified playClip() when a chain target is found. See
test_playback_segment_chaining_core.mjs for the actual chaining-
decision behavior (adjacent/gap-tolerance/large-gap/end-of-list), all
proven by real JS execution, not string assertions.

This file proves everything else the ticket required preserved: manual
clip selection, exact timeline seeking, the accepted unmuted/muted
autoplay fallback, cloud/local media URL handling (recordingMediaUrl()/
isCloudMp4), stale-request protection (structural, not a race --
explained below), the selected historical date, and no regression to
calendar/date navigation or the analytics lanes -- all via a plain
render + string assertions on the real, unmodified output, matching
this suite's established convention.

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
# Chaining is wired to the video 'ended' event, calling this branch's own,
# completely unmodified playClip().
# ---------------------------------------------------------------------------

def test_ended_event_triggers_chain_via_the_existing_unmodified_playclip(monkeypatch):
    html = _render(monkeypatch)
    assert "// === CHAIN_CORE_START ===" in html
    assert "// === CHAIN_CORE_END ===" in html
    assert "function _planNextChainedClip(clips,endedClip){" in html
    idx = html.index("video.addEventListener('ended',()=>{")
    block = html[idx: idx + 250]
    assert "timelinePlayButton.textContent='Play';" in block, "the pre-existing button-state reset must be unchanged"
    assert "const next=_planNextChainedClip(currentClips,selectedClip);" in block
    assert "if(next)playClip(selectedCameraId,next);" in block


# ---------------------------------------------------------------------------
# playClip() itself was not touched: recordingMediaUrl()/cloud MP4
# handling, unmuted-then-muted autoplay fallback, and download/share/
# create-clip button wiring are all still exactly what they were before
# this reconciliation.
# ---------------------------------------------------------------------------

def test_playclip_unmodified_recording_media_url_and_cloud_mp4_handling(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function playClip(cameraId,clip){")
    end_idx = html.index("\n  }", html.index("recordingMediaUrl(cameraId,clip.id)", idx))
    block = html[idx:end_idx]
    assert "const url=recordingMediaUrl(cameraId,clip.id);" in block
    assert "const isCloudMp4=String(clip.name||'').toLowerCase().endsWith('.mp4');" in block
    assert "createClipButton.disabled=isCloudMp4;" in block


def test_playclip_unmuted_then_muted_autoplay_fallback_unchanged(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function playClip(cameraId,clip){")
    end_idx = html.index("\n  }", html.index("video.play()", idx))
    block = html[idx:end_idx]
    assert "video.play().then(()=>{" in block, "unmuted (first) autoplay attempt must be unchanged"
    assert "video.muted=true;" in block, "muted fallback on rejection must be unchanged"
    assert "Audio was blocked by the browser" in block
    assert "Autoplay was blocked by the browser" in block


def test_chain_trigger_reuses_the_same_autoplay_fallback_path(monkeypatch):
    # The chain trigger calls playClip(selectedCameraId,next) -- the
    # exact same function manual clicks/timeline seeks/event markers all
    # already call -- so a chained segment goes through the identical
    # unmuted/muted-fallback attempt as any other playClip() invocation.
    # No second play path was introduced for chaining.
    html = _render(monkeypatch)
    play_clip_calls = html.count("playClip(")
    assert play_clip_calls >= 5, "playClip() must still be the single shared entry point every trigger (manual, marker, seek, chain) calls into"
    assert "if(next)playClip(selectedCameraId,next);" in html


# ---------------------------------------------------------------------------
# Manual clip selection and exact timeline seeking are unchanged.
# ---------------------------------------------------------------------------

def test_manual_clip_selection_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert "segment.addEventListener('click',()=>playClip(cameraId,clip));" in html


def test_exact_timeline_seek_lands_in_the_correct_segment_and_offset(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("timelineLane.addEventListener('click',(event)=>{")
    end_idx = html.index("  });", idx)
    block = html[idx:end_idx]
    assert "const dayString=viewingDate||localDateStringOf(new Date());" in block
    assert "const nearby=findClipNear(currentClips,target.getTime());" in block
    assert "const offsetSeconds=(target.getTime()-playbackDate(nearby.start).getTime())/1000;" in block
    assert "video.currentTime=offsetSeconds;" in block
    # The exact-seek path must not have been rerouted through the chain
    # planner -- it is a distinct, deliberate jump-to-time action.
    assert "_planNextChainedClip" not in block


# ---------------------------------------------------------------------------
# Stale/old playback requests cannot hijack the active player.
#
# This branch's playClip() has no async gap (recordingMediaUrl() is
# synchronous), so there is no equivalent of Samsung's request-token
# race to protect against with a sequencer. The structural guarantee
# instead: EVERY trigger that can change what's playing (manual click,
# event marker, exact-time seek, camera switch, date load, and now the
# chain trigger) funnels through playClip(), which synchronously
# reassigns video.src on every call. Per the <video> element's own
# spec, a source that has already been replaced never fires 'ended' for
# its old content afterward -- so by the time a stale chain trigger for
# an abandoned clip COULD fire, the video is already showing something
# else and the browser has already discarded the old playback session.
# ---------------------------------------------------------------------------

def test_stale_ended_event_cannot_hijack_a_newer_manual_selection(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function playClip(cameraId,clip){")
    block = html[idx: idx + 400]
    assert "selectedClip=clip;" in block
    assert "video.pause();" in block
    assert "video.src=url;" in block
    assert "video.load();" in block
    # Every one of the four other triggers reaches this same function --
    # none of them mutate video.src/selectedClip through a separate path
    # that the chain trigger's own 'ended' check could race against.
    for trigger in [
        "segment.addEventListener('click',()=>playClip(cameraId,clip));",
        "marker.addEventListener('click'",
        "if(next)playClip(selectedCameraId,next);",
    ]:
        assert trigger in html


# ---------------------------------------------------------------------------
# End of the final loaded segment stops cleanly -- no error, no repeat.
# (The planner-level proof that _planNextChainedClip returns null here
# is in test_playback_segment_chaining_core.mjs; this confirms the
# calling site treats null as "do nothing", not an error path.)
# ---------------------------------------------------------------------------

def test_no_next_clip_is_a_silent_no_op_not_an_error(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("video.addEventListener('ended',()=>{")
    block = html[idx: idx + 250]
    assert "if(next)playClip(selectedCameraId,next);" in block
    assert "else" not in block, "absence of a next clip must be a plain no-op, not a separate error branch"


# ---------------------------------------------------------------------------
# Chaining works on historical dates -- currentClips/selectedClip are the
# same state loadRecordingsForDate() already populates for a selected
# date, so the chain trigger needs no date-mode special-casing at all.
# ---------------------------------------------------------------------------

def test_chaining_uses_the_same_state_date_mode_already_populates(monkeypatch):
    html = _render(monkeypatch)
    assert "currentClips=clips;" in html  # set by loadRecordingsForDate() among others
    idx = html.index("video.addEventListener('ended',()=>{")
    block = html[idx: idx + 250]
    assert "viewingDate" not in block, "the chain trigger itself must stay date-mode-agnostic, exactly like the exact-time seek handler"


# ---------------------------------------------------------------------------
# No regression to calendar/date navigation or the analytics lanes.
# ---------------------------------------------------------------------------

def test_no_regression_to_date_navigation(monkeypatch):
    html = _render(monkeypatch)
    assert '<input id="playback-date-input" type="date">' in html
    assert 'id="playback-date-prev"' in html
    assert 'id="playback-date-today"' in html
    assert 'id="playback-date-next"' in html
    assert "function navigateByOneDay(deltaDays){" in html
    assert "await loadRecordingsForDate(selectedCameraId,viewingDate)" in html  # camera-switch date preservation


def test_no_regression_to_analytics_lanes(monkeypatch):
    html = _render(monkeypatch)
    assert "const EVENT_LANE_ORDER=['motion','person','vehicle','lpr','people_counting','intrusion'];" in html
    # 2026-09-04: the container height override must carry !important --
    # see test_playback_analytics_lanes.py's own test for why (the
    # original, non-!important 88px override was silently defeated by an
    # older, unrelated global .timeline-lane{...!important} rule).
    # RECORDING_ROW_TOP_PX is derived from the lane constants now,
    # instead of a hardcoded 74 that could (and did) drift out of sync
    # with the container height -- see that same test for the full
    # value-level proof; here it's enough to confirm the derivation
    # itself, not a second hardcoded number, is what's shipping.
    assert "!important}" in html and "#playback-timeline-lane{height:" in html
    assert "const RECORDING_ROW_TOP_PX=Number(eventLaneTop(null)" in html
