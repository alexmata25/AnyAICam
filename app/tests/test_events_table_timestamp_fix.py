"""Events-table timestamp fix (2026-09-02, second half of the five-hour
offset investigation): _render_customer_events()'s "Time" column
formatted event.get("timestamp") -- a naive-UTC event_timestamp value
-- directly with strftime(), with no timezone conversion at all,
showing the raw UTC clock digits ~5-6 hours (DST-dependent) ahead of
the customer's real Central local time. The Playback page's own
timeline/tooltips were already fixed to convert via playbackDate();
this is the same root cause on the separate /events page's own
server-rendered table, which that earlier fix did not touch.
"""

from types import SimpleNamespace

import main


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def test_events_table_converts_naive_utc_timestamp_to_central(monkeypatch):
    # 2026-09-02T00:38:58 UTC == 2026-09-01 7:38:58 PM CDT (UTC-5, DST).
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
        {"id": "cam-1", "name": "Living Room", "camera_number": 5},
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [
        {
            "id": "ev-1", "camera": 5, "camera_id": "cam-1",
            "camera_name": "Living Room", "event_type": "motion",
            "timestamp": "2026-09-02T00:38:58.419155", "confidence": 0.8,
            "thumbnail": None, "has_event_clip": True,
        },
    ])
    html = main._render_customer_events(_fake_request())
    assert "Sep 01, 2026 · 07:38:58 PM" in html
    # The old bug's exact symptom: the raw UTC clock digits shown verbatim.
    assert "Sep 02, 2026 · 12:38:58" not in html


def test_events_table_handles_a_naive_utc_timestamp_crossing_midnight_into_dst_correctly(monkeypatch):
    # 2026-01-15T05:30:00 UTC (winter, CST = UTC-6) == 2026-01-14 11:30:00 PM CST.
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
        {"id": "cam-1", "name": "Front Door", "camera_number": 3},
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [
        {
            "id": "ev-2", "camera": 3, "camera_id": "cam-1",
            "camera_name": "Front Door", "event_type": "person",
            "timestamp": "2026-01-15T05:30:00", "confidence": 0.75,
            "thumbnail": None, "has_event_clip": False,
        },
    ])
    html = main._render_customer_events(_fake_request())
    assert "Jan 14, 2026 · 11:30:00 PM" in html
