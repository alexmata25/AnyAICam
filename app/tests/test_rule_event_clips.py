"""Line crossings, intrusion zones and Secure Edge INTRUSION ALARMs get the
event clip (2026-10-01).

On staging, 100 line crossings in a day reached the cloud with a thumbnail
and no clip, while person/car/PPE events had clips: the rule worker never
asked for media. Now, like People Counting, a rule event shows the clip that
already covers its moment on its camera (the person's own AI activity,
usually) or builds its own; the cloud lets these types reuse a covering AI
clip or another rule event's clip, under the same camera/appliance/tenant
and moment-inside-the-clip checks.
"""
from datetime import datetime

import pytest

import customer_analytics_rule_worker as worker
from test_analytics_media_reuse_cloud import (  # noqa: F401 -- fixtures and helpers
    _analytics_sync_enabled,
    _media,
    _owner_with_clip,
    _share,
    _sync,
    cloud,
)

FIRED = {"analytic_type": "line_crossing", "rule_id": "rule-1", "zone_name": "Front walk", "direction": "inbound",
         "confidence": 0.9, "track_id": 7}


@pytest.fixture()
def edge(monkeypatch):
    import main
    import event_media_sharing
    calls = {"appended": [], "scheduled": [], "attached": []}
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_analytics_event", lambda event: calls["appended"].append(dict(event)))
    monkeypatch.setattr(main, "_schedule_owned_analytics_clip",
                        lambda event_id, camera, moment, thumb, start=None: calls["scheduled"].append((event_id, camera, thumb)))
    monkeypatch.setattr(event_media_sharing, "attach_child",
                        lambda owner, child, camera: calls["attached"].append((owner, child, camera)))
    return calls


@pytest.mark.parametrize("analytic_type,event_type", [("line_crossing", "line_crossing"), ("intrusion", "intrusion"),
                                                      ("intrusion_alarm", "intrusion_alarm")])
def test_a_rule_event_shows_the_clip_already_covering_its_moment(edge, monkeypatch, analytic_type, event_type):
    import main
    monkeypatch.setattr(main, "_analytics_media_owner", lambda camera, event_id, moment, start=None: "activity-owner")
    record = worker.persist_rule_event(3, dict(FIRED, analytic_type=analytic_type), datetime(2026, 10, 1, 12, 0), "/thumb.jpg")
    assert record["event_type"] == event_type
    assert edge["appended"][0]["media_parent_event_id"] == "activity-owner"  # linked before it is synced
    assert edge["attached"] == [("activity-owner", record["id"], 3)] and edge["scheduled"] == []


def test_a_rule_event_with_nothing_covering_it_builds_its_own_clip(edge, monkeypatch):
    import main
    monkeypatch.setattr(main, "_analytics_media_owner", lambda camera, event_id, moment, start=None: event_id)
    record = worker.persist_rule_event(5, FIRED, datetime(2026, 10, 1, 12, 0), "/thumb.jpg")
    assert "media_parent_event_id" not in edge["appended"][0]
    assert edge["scheduled"] == [(record["id"], 5, "/thumb.jpg")] and edge["attached"] == []


def test_without_event_media_the_rule_event_is_still_saved(edge, monkeypatch):
    import main
    monkeypatch.setattr(main, "_analytics_media_owner", lambda camera, event_id, moment, start=None: None)
    worker.persist_rule_event(5, FIRED, datetime(2026, 10, 1, 12, 0), None)
    assert len(edge["appended"]) == 1 and edge["scheduled"] == [] and edge["attached"] == []


def test_the_real_owner_lookup_finds_the_activity_clip_covering_the_crossing(edge):
    import event_media_sharing
    from datetime import timedelta
    event_media_sharing.owners.reset()
    moment = datetime(2026, 10, 1, 12, 0, 0)
    event_media_sharing.owners.register(2, "person-activity", moment - timedelta(seconds=5), moment + timedelta(seconds=20))
    try:
        record = worker.persist_rule_event(2, FIRED, moment + timedelta(seconds=3), None)
    finally:
        event_media_sharing.owners.reset()
    assert edge["appended"][0]["media_parent_event_id"] == "person-activity"
    assert edge["attached"] == [("person-activity", record["id"], 2)]


# ------------------------------------------------------------------ cloud

@pytest.mark.parametrize("child_type", ["line_crossing", "intrusion", "intrusion_alarm"])
def test_cloud_gives_a_rule_event_the_person_activity_clip(cloud, child_type):
    key = _owner_with_clip(cloud, "cam-1", "person-1", event_type="person")
    assert _sync(cloud, "cam-1", "rule-1", child_type, parent="person-1").status_code == 200
    response = _share(cloud, "cam-1", "rule-1", "person-1")
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    assert _media("rule-1")["s3_key"] == key + ".mp4"


def test_cloud_lets_a_rule_event_share_another_rule_events_own_clip(cloud):
    key = _owner_with_clip(cloud, "cam-1", "crossing-1", event_type="line_crossing")
    assert _sync(cloud, "cam-1", "alarm-1", "intrusion_alarm", parent="crossing-1").status_code == 200
    assert _share(cloud, "cam-1", "alarm-1", "crossing-1").json()["status"] == "accepted"
    assert _media("alarm-1")["s3_key"] == key + ".mp4"


def test_cloud_keeps_every_boundary_for_rule_events(cloud):
    _owner_with_clip(cloud, "cam-2", "person-2", event_type="person")
    _owner_with_clip(cloud, "cam-1", "ppe-1", event_type="ppe")
    # Another camera's clip, and a PPE clip, are never shown on a line crossing.
    assert _sync(cloud, "cam-1", "rule-x", "line_crossing", parent="person-2").status_code == 200
    assert _share(cloud, "cam-1", "rule-x", "person-2").status_code in (403, 404, 409)
    assert _sync(cloud, "cam-1", "rule-y", "line_crossing", parent="ppe-1").status_code == 200
    assert _share(cloud, "cam-1", "rule-y", "ppe-1").status_code == 409



# ------------------------------------------------------------------ durable clip state (2026-10-01)

def _status(local_id, camera="cam-1"):
    from partner_db import connection
    with connection() as db:
        row = db.execute("SELECT media_status,media_status_reason FROM detection_events WHERE local_event_id=? AND camera_id=?",
                         (local_id, camera)).fetchone()
        return tuple(row) if row else None


def _sync_expecting(client, camera, local_id, event_type="intrusion_alarm", **kw):
    from test_analytics_media_reuse_cloud import _headers, MOMENT
    body = {"local_event_id": local_id, "event_type": event_type, "confidence": 0.9, "object_count": 1, "detections": [],
            "event_timestamp": kw.get("timestamp", MOMENT), "media_expected": True}
    if kw.get("parent"):
        body["parent_local_event_id"] = kw["parent"]
    return client.post(f"/api/appliance/analytics/{camera}/events", headers=_headers(), json=body)


def _report_failed(client, camera, local_id, reason="clip_unavailable", appliance="appl-1", credential="credential"):
    from test_analytics_media_reuse_cloud import _headers
    return client.post(f"/api/appliance/analytics/{camera}/events/{local_id}/media/failed",
                       headers=_headers(appliance, credential), json={"reason": reason})


def test_the_edge_promises_a_clip_only_when_one_is_coming(edge, monkeypatch):
    import main
    for owner, expected in (("activity-owner", True), (None, False)):
        edge["appended"].clear()
        monkeypatch.setattr(main, "_analytics_media_owner", lambda camera, event_id, moment, start=None, o=owner: o)
        worker.persist_rule_event(3, FIRED, datetime(2026, 10, 1, 12, 0), None)
        assert edge["appended"][0]["media_expected"] is expected
    import analytics_sync
    assert analytics_sync._build_payload({"id": "e", "event_type": "line_crossing", "timestamp": "t", "media_expected": True})["media_expected"] is True


def test_an_event_is_pending_until_its_own_clip_is_registered(cloud):
    assert _sync_expecting(cloud, "cam-1", "alarm-1").status_code == 200
    assert _status("alarm-1") == ("pending", None)
    _owner_with_clip(cloud, "cam-1", "alarm-1", event_type="intrusion_alarm")  # replays the event, then registers its clip
    assert _status("alarm-1") == ("available", None)
    # a late failure report can never hide a registered clip
    assert _report_failed(cloud, "cam-1", "alarm-1").json()["status"] == "ignored"
    assert _status("alarm-1") == ("available", None)


def test_a_shared_clip_makes_the_event_available(cloud):
    _owner_with_clip(cloud, "cam-1", "person-1", event_type="person")
    assert _sync_expecting(cloud, "cam-1", "alarm-2", parent="person-1").status_code == 200
    assert _status("alarm-2") == ("pending", None)
    assert _share(cloud, "cam-1", "alarm-2", "person-1").json()["status"] == "accepted"
    assert _status("alarm-2") == ("available", None)


def test_a_lost_clip_is_reported_failed_and_shown_as_unavailable(cloud):
    import event_media
    _sync_expecting(cloud, "cam-1", "alarm-3")
    assert _report_failed(cloud, "cam-1", "alarm-3").json() == {"status": "accepted", "media_status": "failed"}
    assert _status("alarm-3") == ("failed", "clip_unavailable")
    assert event_media.media_state(False, "2099-01-01T00:00:00", status="failed") == "unavailable"
    assert event_media.media_state(False, "2000-01-01T00:00:00", status="pending") == "processing"  # still retrying, however long
    assert event_media.media_state(True, "2000-01-01T00:00:00", status="pending") == "ready"


def test_failure_reports_keep_every_ownership_check(cloud):
    _sync_expecting(cloud, "cam-1", "alarm-4")
    # another customer's appliance cannot mark this event, nor reach the camera
    assert _report_failed(cloud, "cam-1", "alarm-4", appliance="appl-2", credential="credential-2").status_code == 403
    assert _report_failed(cloud, "cam-1", "alarm-4", reason="anything").status_code == 400
    assert _report_failed(cloud, "cam-1", "not-synced").status_code == 404
    assert _status("alarm-4") == ("pending", None)


def test_an_event_that_never_promised_a_clip_keeps_the_old_behaviour(cloud):
    assert _sync(cloud, "cam-1", "plain-1", "line_crossing").status_code == 200
    assert _status("plain-1") == (None, None)


def test_the_appliance_never_reports_failure_for_a_clip_still_being_retried(monkeypatch):
    import event_media_outbox
    import event_media_uploader
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", True)
    posts = []
    monkeypatch.setattr(event_media_uploader.recording_upload, "_control_plane_post", lambda path, body: posts.append(path) or {"status": "accepted"})
    monkeypatch.setattr(event_media_uploader.recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(event_media_uploader.recording_upload, "_camera_identity", lambda n: {"camera_id": "cam-1"})
    monkeypatch.setattr(event_media_uploader, "_ensure_detection_event_synced", lambda event_id, camera_id: True)
    monkeypatch.setattr(event_media_outbox, "load", lambda: [{"event_id": "queued-1"}])
    assert event_media_uploader.report_media_failed(event_id="queued-1", camera_number=1, reason="clip_unavailable") is False
    assert posts == []  # in the outbox: still pending, retries continue
    assert event_media_uploader.report_media_failed(event_id="gone-1", camera_number=1, reason="clip_unavailable") is True
    assert posts == ["/api/appliance/analytics/cam-1/events/gone-1/media/failed"]
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)
    assert event_media_uploader.report_media_failed(event_id="gone-2", camera_number=1, reason="clip_unavailable") is False


def test_a_child_whose_owner_has_no_clip_is_reported_failed(monkeypatch):
    import event_media_outbox
    import event_media_sharing
    import event_media_uploader
    reports = []
    monkeypatch.setattr(event_media_outbox, "load", lambda: [])
    monkeypatch.setattr(event_media_uploader, "report_media_failed", lambda **kw: reports.append(kw) or True)
    event_media_sharing.deliver_after_failure("child-1", 2, "owner-1")
    assert reports == [{"event_id": "child-1", "camera_number": 2, "reason": "owner_without_media"}]
