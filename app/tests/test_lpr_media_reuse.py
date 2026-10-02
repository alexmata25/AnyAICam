"""License-plate events share event clips (2026-10-02).

run_lpr_scan() links a plate read to the clip already covering it (or
builds its own), but the cloud's ANALYTICS_MEDIA_PARENT_TYPES had no
'plate' in either direction: a plate's shared clip was refused and retried
forever, and so was any event whose covering clip was a plate's. Plates now
reuse and are reused under exactly the same boundaries as every other type.
"""
import pytest

from test_analytics_media_reuse_cloud import (  # noqa: F401 -- fixtures and helpers
    CLIP,
    MOMENT,
    _analytics_sync_enabled,
    _media,
    _owner_with_clip,
    _share,
    _sync,
    cloud,
)

PLATE_DETAIL = [{"plate": "ABC1234", "plate_confidence": 0.93}]


def test_a_plate_read_shows_the_vehicles_activity_clip(cloud):
    key = _owner_with_clip(cloud, "cam-1", "car-1", event_type="car")
    assert _sync(cloud, "cam-1", "plate-1", "plate", parent="car-1", detections=PLATE_DETAIL).status_code == 200
    response = _share(cloud, "cam-1", "plate-1", "car-1")
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    assert _media("plate-1")["s3_key"] == key + ".mp4"


def test_a_plate_owned_clip_is_shared_with_events_at_the_same_moment(cloud):
    key = _owner_with_clip(cloud, "cam-1", "plate-2", event_type="plate")
    for child_type in ("car", "line_crossing", "plate"):
        local_id = f"child-{child_type}"
        assert _sync(cloud, "cam-1", local_id, child_type, parent="plate-2").status_code == 200
        assert _share(cloud, "cam-1", local_id, "plate-2").json()["status"] == "accepted", child_type
        assert _media(local_id)["s3_key"] == key + ".mp4"


def test_another_camera_or_tenant_is_never_a_plates_parent(cloud):
    _owner_with_clip(cloud, "cam-2", "car-other-cam", event_type="car")
    assert _sync(cloud, "cam-1", "plate-x", "plate", parent="car-other-cam").status_code == 200
    assert _share(cloud, "cam-1", "plate-x", "car-other-cam").status_code in (403, 404, 409)
    _owner_with_clip(cloud, "cam-3", "car-tenant-2", event_type="car", appliance="appl-2", credential="credential-2",
                     prefix="cust-2/site-2/appl-2")
    assert _sync(cloud, "cam-1", "plate-y", "plate", parent="car-tenant-2").status_code == 200
    assert _share(cloud, "cam-1", "plate-y", "car-tenant-2").status_code in (403, 404, 409)
    assert _media("plate-x") is None and _media("plate-y") is None


def test_a_plate_outside_the_parents_clip_is_refused(cloud):
    _owner_with_clip(cloud, "cam-1", "car-2", event_type="car")
    late = CLIP[1].replace(":00:10", ":05:00")
    assert _sync(cloud, "cam-1", "plate-late", "plate", parent="car-2", timestamp=late).status_code == 200
    assert _share(cloud, "cam-1", "plate-late", "car-2").status_code in (403, 409)
    assert _media("plate-late") is None


def test_sharing_stays_one_level(cloud):
    _owner_with_clip(cloud, "cam-1", "car-3", event_type="car")
    assert _sync(cloud, "cam-1", "plate-3", "plate", parent="car-3").status_code == 200
    assert _share(cloud, "cam-1", "plate-3", "car-3").json()["status"] == "accepted"
    # a second plate cannot borrow the first plate's borrowed clip
    assert _sync(cloud, "cam-1", "plate-4", "plate", parent="plate-3").status_code == 200
    assert _share(cloud, "cam-1", "plate-4", "plate-3").status_code == 403
    assert _media("plate-4") is None


def test_an_unapproved_parent_type_is_refused(cloud):
    _owner_with_clip(cloud, "cam-1", "ppe-9", event_type="ppe")
    assert _sync(cloud, "cam-1", "plate-5", "plate", parent="ppe-9").status_code == 200
    assert _share(cloud, "cam-1", "plate-5", "ppe-9").status_code in (403, 409)


def test_the_appliance_sends_the_plates_clip_owner():
    import analytics_sync
    payload = analytics_sync._build_payload({"id": "plate-local", "event_type": "plate", "timestamp": MOMENT, "plate_number": "ABC1234",
                                             "confidence": 0.9, "media_parent_event_id": "car-local"})
    assert payload["event_type"] == "plate" and payload["parent_local_event_id"] == "car-local"
    assert payload["detections"][0]["plate"] == "ABC1234"
