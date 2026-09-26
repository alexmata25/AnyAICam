"""Playback "today" view staleness (2026-09-23, customer-reported: the
timeline correctly showed current activity, but the Recordings section
underneath it -- and the timeline's own event markers -- stayed frozen
on a much older date (a real, live-reproduced example: September 16)
no matter how many times Today was pressed, how many times the camera
was reselected, or how much real time passed).

Root cause, confirmed by direct live reproduction against the running
appliance (not just source inspection): ensureClipsLoaded()/
ensureEventsLoaded() each gated their real fetch behind a per-camera
"loaded once" flag (recordingsLoaded/analyticsLoaded) that, once set,
was NEVER cleared for the rest of the page's lifetime. A customer's
Playback tab is routinely left open for hours; the very first load
permanently froze what "today" looked like for as long as that tab
stayed open, with nothing -- not Today, not switching away and back to
the same camera, not time passing -- ever triggering a second fetch.

Same source-structure testing convention as this suite's other
Playback tests (_render() against _render_customer_playback()'s real
output); real-execution proof that the fixed functions actually
re-fetch (and REPLACE, never merge/duplicate) lives in
test_playback_pagination_core.mjs instead.
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


def test_no_loaded_once_gate_remains_anywhere_in_the_page(monkeypatch):
    """recordingsLoaded/analyticsLoaded were the entire bug -- a
    per-camera flag that, once set on the very first load, permanently
    skipped every future fetch for the rest of the tab's lifetime.
    Neither identifier should exist anywhere in the rendered page at
    all -- not as a declaration, not as a stray reference -- since
    nothing legitimate depends on them once the gate they existed for
    is gone."""
    html = _render(monkeypatch)
    assert "recordingsLoaded" not in html
    assert "analyticsLoaded" not in html


def test_ensure_clips_loaded_always_fetches_no_cache_hit_short_circuit(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("async function ensureClipsLoaded(cameraId){")
    end = html.index("\n  }", idx)
    block = html[idx:end]
    assert "await fetchClipsMetadata(cameraId," in block
    # The exact old short-circuit this bug lived in -- must be gone.
    assert ".has(cameraId)" not in block
    # A fresh successful result must REPLACE the cache entry outright
    # (never a spread/append), so a second call can never leave a
    # stale item sitting alongside a fresh one -- the exact shape a
    # duplicate-looking card would come from.
    assert "recordingsByCamera[cameraId]=clips;" in block
    assert "...recordingsByCamera[cameraId]" not in block


def test_ensure_events_loaded_always_fetches_no_cache_hit_short_circuit(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("async function ensureEventsLoaded(cameraId){")
    end = html.index("\n  }", idx)
    block = html[idx:end]
    assert "await fetchCameraEvents(cameraId)" in block
    assert ".has(cameraId)" not in block
    assert "analyticsByCamera[cameraId]=events;" in block


def test_a_failed_fetch_still_falls_back_to_whatever_was_already_showing(monkeypatch):
    """The one piece of the original contract that was already correct
    and must survive unchanged: a transient network failure must never
    be presented as "this camera has zero recordings" -- it falls back
    to the previous render (if any) so the next call (next Today press,
    next camera reselect) can simply retry."""
    html = _render(monkeypatch)
    idx = html.index("async function ensureClipsLoaded(cameraId){")
    end = html.index("\n  }", idx)
    block = html[idx:end]
    assert "if(clips===null){" in block
    assert "return recordingsByCamera[cameraId]||[];" in block


def test_renders_clean_after_the_fix_with_real_recordings(monkeypatch):
    """Sanity check that removing the loaded-once Sets did not break
    the page's own initial (server-rendered) recording list -- the
    seeded recordingsByCamera/analyticsByCamera objects themselves are
    untouched by this fix, only the JS-side re-fetch gate is."""
    html = _render(
        monkeypatch,
        recordings=[{"id": "r1", "start": "2026-09-23T10:00:00", "end": "2026-09-23T10:05:00", "name": "clip.mkv"}],
    )
    assert '"id": "r1"' in html or "'id': 'r1'" in html or "r1" in html
