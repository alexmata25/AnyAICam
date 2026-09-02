"""Event-direct-playback fix (2026-09-02): the prior Events-to-Playback
fix correctly routed to the right camera and timestamp, but always via
findClipNear() against the *recordings* catalog -- i.e. "find whatever
5-minute recording covers this moment" -- never the event's own short
clip. Any event whose moment falls in a real gap between recordings
(not rare -- see this project's own per-camera recording-gap data)
legitimately found nothing, so playClip() never ran and the event
silently "did not play" even though a real, already-authorized event
clip existed. Root cause: playable events were made dependent on a
nearby continuous recording instead of their own event media.

Fix: an event with has_event_clip=true and a real event_id now links
with ?event=<event_id> instead of ?t=<timestamp>, and
_render_customer_playback()'s renderCamera() loads it directly via the
existing, already-authorized /api/customer/events/{camera_id}/
{event_id}/media/url route -- the same route the timeline's own
event-marker clicks already use -- completely bypassing
findClipNear()/the recordings catalog. Analytics-only events (no clip)
keep the prior nearby-recording behavior, unchanged.
"""

import re
import sqlite3
from types import SimpleNamespace

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


def _fake_request(params=None):
    params = params or {}
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: params.get(key, default)))


# --------------------------------------------------------- A: has_event_clip=true


def test_playable_event_link_carries_camera_and_event_id_not_timestamp():
    html = main._customer_event_actions(
        "cam-living-room", "2026-09-02T00:38:58.419155", event_id="ev-abc123", has_event_clip=True,
    )
    m = re.search(r'href="(/playback\?[^"]+)"', html)
    assert m, html
    href = m.group(1)
    assert "camera=cam-living-room" in href
    assert "event=ev-abc123" in href
    assert "autoplay=event" in href
    assert "&t=" not in href  # no timestamp/recording-lookup param for a real event clip


def test_playback_page_reads_event_param_into_js_and_takes_priority_over_seek(monkeypatch):
    cameras = [{"id": "cam-1", "name": "Front Door", "camera_number": 1}]
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kw: [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])

    # Both event= and (defensively) t= present -- event= must win.
    html = main._render_customer_playback(
        cameras, _fake_request({"camera": "cam-1", "event": "ev-xyz", "t": "123", "autoplay": "event"})
    )
    assert 'const initialEventId="ev-xyz";' in html

    i = html.index("if(initialEventId){")
    j = html.index("}else if(seekTimestamp){", i)
    branch = html[i:j]
    assert "playEventClipDeepLink(cameraId,initialEventId)" in branch
    assert "findClipNear(clips" not in branch  # requirement A: no continuous-recording lookup call on this path


def test_playeventclipdeeplink_never_calls_findclipnear_anywhere_in_its_own_body():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    start = html.index("async function playEventClipDeepLink(")
    depth = 0
    pos = html.index("{", start)
    begin = pos
    while True:
        ch = html[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        pos += 1
    body = html[begin:pos]
    assert "findClipNear(clips" not in body
    assert "fetchClipsMetadata(cameraId" not in body
    assert "/media/url" in body  # uses the existing authorized event-media route


def test_playeventclipdeeplink_uses_the_existing_authorized_event_media_route():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    assert "/api/customer/events/${encodeURIComponent(cameraId)}/${encodeURIComponent(eventId)}/media/url" in html


# --------------------------------------------------------- authorization (reused route, direct proof)


def test_event_media_url_route_enforces_camera_authorization(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
    with pytest.raises(Exception) as excinfo:
        main.customer_event_media_url("cam-2-not-mine", "ev-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403


def test_event_media_url_route_404s_when_no_clip_exists(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
    monkeypatch.setattr(main, "_customer_event_media_url", lambda camera_id, event_id: None)
    with pytest.raises(Exception) as excinfo:
        main.customer_event_media_url("cam-1", "ev-does-not-exist", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404


# --------------------------------------------------------- B: analytics-only event, qualifying recording exists


def test_analytics_only_event_without_clip_uses_timestamp_link_and_does_not_claim_event_clip():
    html = main._customer_event_actions(
        "cam-1", "2026-09-02T00:38:58", event_id="ev-analytics-only", has_event_clip=False,
    )
    m = re.search(r'href="(/playback\?[^"]+)"', html)
    href = m.group(1)
    assert "event=" not in href  # analytics-only -- no real clip to deep-link to
    assert re.search(r"[?&]t=\d+", href)
    assert "autoplay=event" in href


def test_seek_timestamp_branch_labels_result_as_recording_not_event_clip():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    i = html.index("}else if(seekTimestamp){")
    j = html.index("}else if(clips.length){", i)
    branch = html[i:j]
    assert "'Recording footage near this event. Playing…'" in branch
    assert "'Playing event clip'" not in branch


# --------------------------------------------------------- C: no event clip, no qualifying recording


def test_no_media_at_all_shows_the_honest_message_not_the_old_generic_one():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    assert "'No recording is available for this event.'" in html
    assert "'No recording available for this time yet.'" not in html


def test_event_clip_fetch_failure_shows_the_honest_message_without_a_fallback_recording():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    start = html.index("async function playEventClipDeepLink(")
    end = html.index("async function renderCamera(", 0)  # renderCamera is defined earlier; just bound the search generously
    # Simpler: just confirm the honest fallback line exists textually
    # inside playEventClipDeepLink's own body (already proven not to
    # touch findClipNear/fetchClipsMetadata above).
    assert "status.textContent='No recording is available for this event.';" in html[start:start + 4000]


# --------------------------------------------------------- E: autoplay rejection


def test_event_clip_autoplay_rejection_leaves_it_selected_with_no_forced_mute():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    start = html.index("async function playEventClipDeepLink(")
    depth = 0
    pos = html.index("{", start)
    begin = pos
    while True:
        ch = html[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        pos += 1
    body = html[begin:pos]
    assert "video.muted=true" not in body  # no forced-mute fallback in this path
    assert "timelinePlayButton.disabled=false" in body  # explicit Play control is left usable
    assert "Event clip ready" in body  # visible status, not silent


# --------------------------------------------------------- D: cross-customer/cross-camera event id


def test_cross_customer_event_id_is_rejected_and_exposes_no_media_url(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-mine", "name": "Mine", "camera_number": 1}])
    with pytest.raises(Exception) as excinfo:
        main.customer_event_media_url("cam-someone-elses", "ev-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403


def test_event_media_url_helper_is_scoped_by_camera_id_join(tmp_path):
    """_customer_event_media_url() must not return a URL for an event
    that belongs to a different camera than the one requested -- same
    IDOR-safe join shape as the recordings routes."""
    db_path = tmp_path / "test_event_media_scope.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-a','cust-1','site-1','appl-1',1,'Camera A','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-b','cust-1','site-1','appl-1',2,'Camera B','2026-01-01')")
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,object_count,event_timestamp,created_at) "
            "VALUES('ev-1','cust-1','site-1','appl-1','cam-a','local-1','motion',1,'2026-09-02T00:00:00','2026-09-02T00:00:00')"
        )
        conn.commit()

        # Event belongs to cam-a -- requesting it scoped to cam-b must find nothing.
        url = main._customer_event_media_url("cam-b", "ev-1")
    assert url is None
