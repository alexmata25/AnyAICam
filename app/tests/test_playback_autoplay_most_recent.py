"""Playback: without a deep-link timestamp, the page now auto-loads the
most recent recording instead of leaving the player with no source
until something is clicked. Fixes a real customer-reported regression:
backend URLs and data were correct (see test_customer_recordings_r4.py
and test_recording_read_credentials_cache.py), but the player stayed
black at 0:00 because nothing ever called playClip() on a normal page
load -- only an explicit click or a ?t= deep link did. Clicking a
segment/row/marker is unchanged; this only adds what happens with none
of those yet.

Updated for the bounded-load rewrite (test_playback_bounded_load.py):
_render_customer_playback() now sources its initial data from
_customer_recording_rows() (metadata only, no url) instead of the
unbounded _customer_camera_recordings(), and playClip() takes
(cameraId, clip) instead of just (clip) since a recording's URL is
now fetched on demand rather than being present on the clip object
already -- these tests assert against that same current signature.

Same import-inside-container constraint as the other main.py-importing
test files in this suite.
"""

from types import SimpleNamespace

import main


def _fake_request(t=None, camera=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: {"t": t, "camera": camera}.get(key, default)))


def test_no_deep_link_auto_plays_the_most_recent_clip(monkeypatch):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: [
        {"id": "rec-old", "start": "2026-08-20T10:00:00", "end": "2026-08-20T10:05:00", "name": "old.mp4"},
        {"id": "rec-newest", "start": "2026-08-23T18:57:10", "end": "2026-08-23T19:01:08", "name": "newest.mp4"},
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])
    html = main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())
    i = html.index("if(seekTimestamp){")
    j = html.index("filterButtons.forEach", i)
    branch = html[i:j]
    assert "}else if(clips.length){" in branch
    assert "playClip(cameraId,clips[clips.length-1]);" in branch  # the newest clip, since clips is oldest-first


def test_deep_link_timestamp_behavior_is_unchanged(monkeypatch):
    """A ?t= deep link must still try findClipNear() first, exactly as
    before -- the new auto-play branch only ever runs in its absence."""
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: [
        {"id": "rec-old", "start": "2026-08-20T10:00:00", "end": "2026-08-20T10:05:00", "name": "old.mp4"},
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])
    html = main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request(t="2026-08-20T10:02:00"))
    assert "const initialTimestamp=" in html
    assert '"2026-08-20T10:02:00"' in html


def test_no_recordings_at_all_still_shows_the_honest_empty_state(monkeypatch):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])
    html = main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())
    assert "No recordings available yet." in html
