"""Five-hour timestamp offset fix (2026-09-02): recordings.started_at
and detection_events.event_timestamp are naive strings holding actual
UTC wall-clock time (confirmed by comparing live rows against the
server's real UTC clock -- see main.py's _customer_camera_events()
docstring). The ECMAScript Date spec parses a date-time string with no
Z/offset as LOCAL time, not UTC. playbackDate() correctly appends 'Z'
before parsing (used by the mobile events list and the clip list), but
timelinePercent() and two tooltip builders on the same Playback page
called bare `new Date(...)` on the same naive values -- silently
treating raw UTC digits as if they were already local, showing/
positioning everything ~5-6 hours (DST-dependent) ahead of the
customer's real local time. The fix makes every one of these call
sites route through the same already-correct playbackDate() helper.

Renders the real page (same pattern as test_playback_autoplay_most_
recent.py) and asserts the generated JS no longer contains a bare
`new Date(...)` on these fields, and does contain the playbackDate()-
routed replacements.
"""

import main


def _fake_request():
    from types import SimpleNamespace
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def _render():
    return main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}],
        _fake_request(),
    )


def test_timeline_percent_uses_playback_date_not_bare_new_date():
    html = _render()
    assert "const d=playbackDate(dateStr);" in html
    assert "const d=new Date(dateStr);" not in html


def test_recording_segment_tooltip_uses_playback_date():
    html = _render()
    assert "segment.title=playbackDate(clip.start).toLocaleString();" in html
    assert "segment.title=new Date(clip.start).toLocaleString();" not in html


def test_event_marker_tooltip_uses_playback_date():
    html = _render()
    assert "playbackDate(event.timestamp).toLocaleString()" in html
    assert "new Date(event.timestamp).toLocaleString()" not in html


def test_mobile_events_list_still_uses_playback_date_unchanged():
    """This call site was already correct -- confirm the fix didn't
    touch or break it."""
    html = _render()
    assert "playbackDate(event.timestamp).toLocaleString()" in html


def test_clip_list_duration_math_still_uses_playback_date_unchanged():
    html = _render()
    assert "playbackDate(clip.end)-playbackDate(clip.start)" in html
