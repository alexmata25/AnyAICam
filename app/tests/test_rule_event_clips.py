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
