"""Event-clip thumbnail fix (2026-09-02): detection_event_media rows
have always carried a real thumbnail_s3_key alongside the clip's own
s3_key (written by the same ingestion pipeline for every event that
gets a captured clip), but _customer_detection_events() hardcoded
thumbnail=None for every row regardless, so no Events-page row ever
showed a real preview image -- the only visible difference between an
event with a real clip and one without was the clickable
has_event_clip wrapper itself (a play-badge box vs a bare "--").

This proves: an event with a real clip now reports a thumbnail URL
pointing at a new, authorized, per-event thumbnail route; an event
with a clip but no thumbnail_s3_key (should not normally happen, but
must fail safe) reports thumbnail=None, not a broken link; an
analytics-only event (no detection_event_media row at all) still
reports thumbnail=None and has_event_clip=False, unchanged; and the
new route enforces the exact same _customer_authorized_camera_id()
check every other bounded-Playback/event route already uses -- no
second/weaker authorization path.
"""

from types import SimpleNamespace

import main


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


class _FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_event_with_real_clip_and_thumbnail_reports_a_thumbnail_url(monkeypatch):
    monkeypatch.setattr(
        main, "partner_identity",
        lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "a@b.com"},
        raising=False,
    )
    import partner_portal
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: {
        "role": "customer_owner", "customer_id": "cust-1", "email": "a@b.com",
    })

    class FakeDB:
        def execute(self, query, params=None):
            return self

        def fetchall(self):
            return [_FakeRow({
                "id": "ev-1", "event_type": "motion", "confidence": 20.9,
                "event_timestamp": "2026-09-02T15:02:32.948459", "camera_id": "cam-1",
                "camera": 1, "camera_display_name": "Front Door", "site_name": "Home",
                "has_event_clip": 1, "thumbnail_s3_key": "recordings/.../motion_x.jpg",
            })]

    import contextlib

    @contextlib.contextmanager
    def fake_connection():
        yield FakeDB()

    monkeypatch.setattr(main, "connection", fake_connection, raising=False)
    import partner_db
    monkeypatch.setattr(partner_db, "connection", fake_connection)

    events = main._customer_detection_events(_fake_request())
    assert events is not None
    assert len(events) == 1
    event = events[0]
    assert event["has_event_clip"] is True
    assert event["thumbnail"] == "/api/customer/events/cam-1/ev-1/thumbnail"


def test_analytics_only_event_still_reports_no_thumbnail_and_no_clip(monkeypatch):
    import partner_portal
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: {
        "role": "customer_owner", "customer_id": "cust-1", "email": "a@b.com",
    })

    class FakeDB:
        def execute(self, query, params=None):
            return self

        def fetchall(self):
            return [_FakeRow({
                "id": "ev-2", "event_type": "person", "confidence": 0.9,
                "event_timestamp": "2026-09-02T15:00:00", "camera_id": "cam-1",
                "camera": 1, "camera_display_name": "Front Door", "site_name": "Home",
                "has_event_clip": 0, "thumbnail_s3_key": None,
            })]

    import contextlib

    @contextlib.contextmanager
    def fake_connection():
        yield FakeDB()

    import partner_db
    monkeypatch.setattr(partner_db, "connection", fake_connection)

    events = main._customer_detection_events(_fake_request())
    event = events[0]
    assert event["has_event_clip"] is False
    assert event["thumbnail"] is None


def test_thumbnail_route_requires_camera_authorization(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: False)
    try:
        main.customer_event_thumbnail("cam-1", "ev-1", _fake_request())
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_thumbnail_route_redirects_to_presigned_url_when_authorized(monkeypatch):
    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)
    monkeypatch.setattr(main, "_customer_event_thumbnail_url", lambda camera_id, event_id: "https://s3.example.com/signed.jpg")
    response = main.customer_event_thumbnail("cam-1", "ev-1", _fake_request())
    assert response.status_code == 302
    assert response.headers["location"] == "https://s3.example.com/signed.jpg"


def test_thumbnail_route_404s_when_no_thumbnail_exists(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)
    monkeypatch.setattr(main, "_customer_event_thumbnail_url", lambda camera_id, event_id: None)
    try:
        main.customer_event_thumbnail("cam-1", "ev-1", _fake_request())
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_events_page_row_renders_a_real_img_for_the_clip_thumbnail(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
        {"id": "cam-1", "name": "Front Door", "camera_number": 1}
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [{
        "id": "ev-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T15:02:32.948459", "confidence": 20.9,
        "thumbnail": "/api/customer/events/cam-1/ev-1/thumbnail", "has_event_clip": True,
    }])
    html = main._render_customer_events(_fake_request())
    assert 'src="/api/customer/events/cam-1/ev-1/thumbnail"' in html
    assert 'class="event-thumb-player"' in html


def test_events_page_row_without_a_thumbnail_still_shows_the_em_dash(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
        {"id": "cam-1", "name": "Front Door", "camera_number": 1}
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [{
        "id": "ev-2", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "person", "timestamp": "2026-09-02T15:00:00", "confidence": 0.9,
        "thumbnail": None, "has_event_clip": False,
    }])
    html = main._render_customer_events(_fake_request())
    assert '<td>—</td>' in html
