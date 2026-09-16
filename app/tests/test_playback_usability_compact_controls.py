"""Playback usability fix (2026-09-16), triggered by a real e2e browser
screenshot at 1440x900: the video player and the timeline must both be
visible together on a normal desktop viewport without scrolling. Before
this fix, `.playback-workspace-solo .camera-view{aspect-ratio:21/9}` at
the page's own real ~1220px container width rendered a ~523px-tall
video -- combined with the timeline section's own real ~285px height
and the page header above, that exceeded a 900px viewport by hundreds
of pixels (confirmed directly: `document.documentElement.scrollHeight`
was 1326 against a 900px viewport in the real captured screenshot).

Fix: height-first sizing (max-height, viewport-relative and budgeted
against the timeline's own real measured height) with width:auto
deriving from a true, undistorted 16:9 aspect-ratio, centered -- plus
compact icon-only primary toolbar buttons (skip back/forward, play/
pause, download, share, bookmark) instead of full words, each with a
title/aria-label so meaning isn't lost. Nothing removed: every control
is still a real, clickable, disabled-until-selected <button> -- only
its visual size and label changed.

Same _render()/_fake_request() convention as this suite's other
Playback tests (see test_playback_analytics_lanes.py).
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
# Video sizing: height-first, real aspect ratio, centered -- not the old
# full-width-then-stretched-short 21:9 rule.
# ---------------------------------------------------------------------------

def test_video_is_capped_by_height_not_stretched_by_width(monkeypatch):
    html = _render(monkeypatch)
    assert (
        ".playback-workspace-solo .camera-view{aspect-ratio:16/9;max-height:min(38vh,380px);"
        "width:auto;max-width:100%;margin:0 auto}"
        in html
    )
    # The old width-driven 21:9-at-full-width rule must actually be gone,
    # not merely joined by the new one -- otherwise cascade order could
    # silently let the old rule win again on some browser/media-query
    # combination.
    assert "aspect-ratio:21/9" not in html


def test_video_container_is_centered_within_its_panel(monkeypatch):
    html = _render(monkeypatch)
    assert ".playback-workspace-solo .panel{display:flex;justify-content:center}" in html


def test_video_element_itself_still_fills_its_now_correctly_sized_container(monkeypatch):
    """The inner <video> still gets width:100%;height:100% -- that's
    correct and unchanged: it fills whatever box the (now height-capped)
    outer .camera-view container resolves to. Only the container's own
    sizing rule changed, not this relationship."""
    html = _render(monkeypatch)
    assert '<video id="playback-video" controls playsinline style="width:100%;height:100%">' in html


# ---------------------------------------------------------------------------
# Compact controls: real, clickable icon buttons -- not text removed,
# just relabeled -- each with an accessible name.
# ---------------------------------------------------------------------------

def test_primary_toolbar_buttons_are_compact_icons_with_accessible_names(monkeypatch):
    html = _render(monkeypatch)
    expected = {
        "skip-back": ("⏪", "Back 10 seconds"),
        "timeline-play": ("▶", "Play"),
        "skip-forward": ("⏩", "Forward 10 seconds"),
        "download-selected": ("⬇", "Download"),
        "share-selected": ("⤴", "Share"),
    }
    for button_id, (glyph, label) in expected.items():
        assert f'id="{button_id}"' in html
        assert f'aria-label="{label}"' in html, button_id
        assert f">{glyph}<" in html, button_id


def test_bookmark_button_keeps_its_existing_not_available_yet_tooltip(monkeypatch):
    """The compaction must not silently drop the existing explanatory
    tooltip that was already there for a real, known limitation."""
    html = _render(monkeypatch)
    assert 'title="Bookmarking from Playback is not available yet."' in html
    assert 'aria-label="Bookmark">☆<' in html


def test_every_toolbar_button_is_still_a_real_disabled_until_selected_button(monkeypatch):
    """Nothing was demoted to a non-interactive icon/span -- every
    control this fix touched is still exactly what it was: a real
    <button type="button" disabled>, enabled later by the same
    existing JS this fix does not touch."""
    html = _render(monkeypatch)
    for button_id in ("skip-back", "timeline-play", "skip-forward", "download-selected", "share-selected", "bookmark-selected"):
        assert f'<button id="{button_id}" type="button" disabled' in html, button_id


def test_create_clip_and_browse_recordings_keep_their_original_text_labels(monkeypatch):
    """Not every control was converted -- "Create clip" and "Browse
    recordings" have no single universally-recognizable icon, so they
    deliberately stayed as text ("icons... where practical")."""
    html = _render(monkeypatch)
    assert '<button id="create-clip" type="button" disabled>Create clip</button>' in html
    assert '<button id="browse-recordings" type="button" class="ghost-button">Browse recordings</button>' in html


def test_toolbar_button_compaction_css_is_scoped_to_playback_only(monkeypatch):
    """Must not leak into Live View's own Monitor page, which reuses
    the bare .monitor-toolbar class -- scoped by the same
    #playback-monitor-timeline id already used for this page's other
    Live-View-shared-class overrides."""
    html = _render(monkeypatch)
    assert "#playback-monitor-timeline .monitor-toolbar button{padding:6px 10px;min-height:32px;" in html


# ---------------------------------------------------------------------------
# Play/pause state, now driven by one named helper instead of three
# separate bare textContent assignments -- same trigger points, same
# real-world behavior.
# ---------------------------------------------------------------------------

def test_play_pause_helper_sets_glyph_title_and_aria_label_together(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function setPlayButtonState(isPlaying){")
    body = html[idx: idx + 300]
    assert "timelinePlayButton.textContent=isPlaying?'⏸':'▶';" in body
    assert "timelinePlayButton.title=isPlaying?'Pause':'Play';" in body
    assert "timelinePlayButton.setAttribute('aria-label',isPlaying?'Pause':'Play');" in body


def test_play_pause_and_ended_events_all_call_the_shared_helper(monkeypatch):
    html = _render(monkeypatch)
    assert "video.addEventListener('play',()=>{{\n    setPlayButtonState(true);" in html or "setPlayButtonState(true);" in html
    play_idx = html.index("video.addEventListener('play'")
    pause_idx = html.index("video.addEventListener('pause'")
    ended_idx = html.index("video.addEventListener('ended'")
    assert "setPlayButtonState(true);" in html[play_idx: play_idx + 100]
    assert "setPlayButtonState(false);" in html[pause_idx: pause_idx + 100]
    assert "setPlayButtonState(false);" in html[ended_idx: ended_idx + 100]
