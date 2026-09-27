"""Exclusion-zone behaviour the Living Room physical walk test depends on
(2026-09-27). Complements test_detection_exclusion.py with: resolution
independence, fail-safe handling of bad configuration, reload/restart,
per-camera isolation with different zones, and the suppression evidence
/api/ai/status now reports (a suppressed detection creates no event, so
before this there was nothing to check during a walk test).

The zone below is the real "Physical test - ignore floor in front of TV"
geometry drawn on the Ryzen Living Room camera.
"""

import json
import logging

import numpy as np
import pytest

import detection_exclusion
import main
from partner_db import connection
from test_detection_exclusion import LEFT_HALF, _add_zone, db  # noqa: F401  (db is a fixture)

LIVING_ROOM_ZONE = [
    {"x": 0.3393749952316284, "y": 0.09944443172878689},
    {"x": 0.6190624952316284, "y": 0.09944443172878689},
    {"x": 0.6190624952316284, "y": 0.9938888761732313},
    {"x": 0.3393749952316284, "y": 0.9938888761732313},
]
RIGHT_HALF = [{"x": 0.5, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0.5, "y": 1.0}]


@pytest.fixture(autouse=True)
def _clean_stats():
    detection_exclusion.reset_stats()
    yield
    detection_exclusion.reset_stats()


def _person_at(cx, cy, width, height, box_fraction=(0.09, 0.24), name="person", confidence=0.9):
    """A person-sized box centred at (cx, cy), as fractions of a width x height frame."""
    w, h = int(box_fraction[0] * width), int(box_fraction[1] * height)
    return {"class_name": name, "confidence": confidence,
            "x": int(round(cx * width - w / 2)), "y": int(round(cy * height - h / 2)), "width": w, "height": h}


# ------------------------------------------------------------------ resolution independence

@pytest.mark.parametrize("width,height", [(640, 360), (1280, 720), (1920, 1080), (3840, 2160), (640, 480), (2688, 1520)])
def test_the_same_zone_decides_identically_at_every_stream_resolution(db, width, height):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    walker_in_front_of_tv = _person_at(0.48, 0.55, width, height)
    just_inside_right_edge = _person_at(0.60, 0.50, width, height)
    just_outside_right_edge = _person_at(0.64, 0.50, width, height)  # the chair beside the sofa
    on_the_stairs = _person_at(0.25, 0.70, width, height)
    on_the_sofa = _person_at(0.80, 0.50, width, height)
    kept = detection_exclusion.filter_detections(
        1, [walker_in_front_of_tv, just_inside_right_edge, just_outside_right_edge, on_the_stairs, on_the_sofa], frame)
    assert kept == [just_outside_right_edge, on_the_stairs, on_the_sofa]


def test_motion_mask_covers_the_zone_fraction_of_the_frame(db):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    mask = detection_exclusion.motion_exclusion_mask(1).reshape(90, 160)
    columns = np.where(mask.any(axis=0))[0]
    rows = np.where(mask.any(axis=1))[0]
    assert columns.min() / 160 == pytest.approx(0.339, abs=1 / 160 + 1e-6)
    assert (columns.max() + 1) / 160 == pytest.approx(0.619, abs=1 / 160 + 1e-6)
    assert rows.min() / 90 == pytest.approx(0.099, abs=1 / 90 + 1e-6)
    assert not mask[:, :50].any() and not mask[:, 105:].any()  # stairs and sofa still watched


# ------------------------------------------------------------------ bad configuration fails safe

@pytest.mark.parametrize("geometry_json", [
    "not json at all", '"a string"', '{"x": 0.1, "y": 0.1}', "[]",
    '[{"x": 0, "y": 0}, {"x": 1, "y": 1}]',                      # only two points
    '[{"x": 0, "y": 0}, {"x": 1}, {"x": 1, "y": 1}]',             # a point without y
    '[{"x": "left", "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]',
    "",                                                          # empty (the column is NOT NULL)
])
def test_malformed_or_missing_geometry_excludes_nothing_and_never_raises(db, geometry_json):
    with connection() as conn:
        conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at) "
                     "VALUES('bad','cust-1','site-1','app-1','cam-1','exclusion','Bad',NULL,?,1,'x','x')", (geometry_json,))
    detection_exclusion.reset_cache()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    detections = [_person_at(0.48, 0.55, 1280, 720)]
    assert detection_exclusion.filter_detections(1, detections, frame) == detections
    assert detection_exclusion.motion_exclusion_mask(1) is None
    assert detection_exclusion.status(1)["zones_active"] == 0


def test_a_bad_zone_does_not_disable_a_good_one_on_the_same_camera(db):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    with connection() as conn:
        conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at) "
                     "VALUES('bad','cust-1','site-1','app-1','cam-1','exclusion','Bad',NULL,'garbage',1,'x','x')")
    detection_exclusion.reset_cache()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert detection_exclusion.filter_detections(1, [_person_at(0.48, 0.55, 1280, 720)], frame) == []


def test_a_rule_store_failure_keeps_every_detection(db, monkeypatch):
    def broken(camera_id):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(detection_exclusion, "_load_zones", broken)
    detection_exclusion.reset_cache()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    detections = [_person_at(0.48, 0.55, 1280, 720)]
    assert detection_exclusion.filter_detections(1, detections, frame) == detections


def test_a_missing_frame_keeps_every_detection(db):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    detections = [_person_at(0.48, 0.55, 1280, 720)]
    assert detection_exclusion.filter_detections(1, detections, None) == detections


# ------------------------------------------------------------------ reload and restart

def test_zone_edits_apply_within_the_cache_window_and_survive_a_restart(db, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(detection_exclusion.time, "monotonic", lambda: clock["now"])
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    walker = _person_at(0.48, 0.55, 1280, 720)

    assert detection_exclusion.filter_detections(1, [walker], frame) == [walker]  # no zone yet
    with connection() as conn:  # the edge sync writes a newly drawn zone
        conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at) "
                     "VALUES('lr','cust-1','site-1','app-1','cam-1','exclusion','TV floor',NULL,?,1,'x','x')", (json.dumps(LIVING_ROOM_ZONE),))
    assert detection_exclusion.filter_detections(1, [walker], frame) == [walker]  # still cached (<= CACHE_SECONDS)
    clock["now"] += detection_exclusion.CACHE_SECONDS + 0.1
    assert detection_exclusion.filter_detections(1, [walker], frame) == []  # picked up, no restart

    detection_exclusion.reset_cache()  # a VMS restart: all in-memory state gone, zone lives in the database
    assert detection_exclusion.filter_detections(1, [walker], frame) == []

    with connection() as conn:  # the customer deletes the zone
        conn.execute("DELETE FROM customer_analytics_rules WHERE id='lr'")
    clock["now"] += detection_exclusion.CACHE_SECONDS + 0.1
    assert detection_exclusion.filter_detections(1, [walker], frame) == [walker]


# ------------------------------------------------------------------ per-camera isolation

def test_two_cameras_with_different_zones_never_share_them(db):
    _add_zone(camera="cam-1", geometry=LEFT_HALF, rule_id="left")
    _add_zone(camera="cam-2", geometry=RIGHT_HALF, rule_id="right")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    left, right = _person_at(0.2, 0.5, 1280, 720), _person_at(0.8, 0.5, 1280, 720)
    assert detection_exclusion.filter_detections(1, [left, right], frame) == [right]
    assert detection_exclusion.filter_detections(2, [left, right], frame) == [left]
    mask1 = detection_exclusion.motion_exclusion_mask(1).reshape(90, 160)
    mask2 = detection_exclusion.motion_exclusion_mask(2).reshape(90, 160)
    assert mask1[:, :80].all() and not mask1[:, 80:].any()
    assert mask2[:, 80:].all() and not mask2[:, :80].any()
    assert detection_exclusion.status(1)["suppressed_total"] == 1
    assert detection_exclusion.status(2)["suppressed_total"] == 1


def test_another_customers_camera_with_the_same_number_is_not_affected(db):
    # cam-3 is camera_number 1 on a different appliance/customer; this edge
    # maps its camera 1 to cam-1 only, so cam-3's zone never applies here.
    _add_zone(camera="cam-3", geometry=LIVING_ROOM_ZONE, rule_id="other", customer="cust-2")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    walker = _person_at(0.48, 0.55, 1280, 720)
    assert detection_exclusion.filter_detections(1, [walker], frame) == [walker]


# ------------------------------------------------------------------ evidence for the physical test

def test_suppressed_detections_leave_evidence_without_creating_events(db, caplog):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    walker = _person_at(0.48, 0.55, 1280, 720, confidence=0.92)
    sofa = _person_at(0.80, 0.50, 1280, 720)
    with caplog.at_level(logging.INFO, logger="anyaicam.detection_exclusion"):
        assert detection_exclusion.filter_detections(1, [walker, sofa], frame) == [sofa]
        assert detection_exclusion.filter_detections(1, [walker], frame) == []
    status = detection_exclusion.status(1)
    assert status["zones_active"] == 1
    assert status["suppressed_total"] == 2
    assert status["suppressed_by_class"] == {"person": 2}
    assert status["last_suppressed_at"]
    last = status["last_suppressed"][0]
    assert last["class_name"] == "person" and last["confidence"] == 0.92
    assert 0.339 <= last["centre"][0] <= 0.619 and 0.099 <= last["centre"][1] <= 0.994
    assert status["zones"][0][0] == [0.3394, 0.0994]
    lines = [r.getMessage() for r in caplog.records if "detection_exclusion.suppressed" in r.getMessage()]
    assert len(lines) == 1 and "camera=1" in lines[0]  # rate-limited: one line per camera per minute


def test_outside_detections_are_never_counted_as_suppressed(db):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    detection_exclusion.filter_detections(1, [_person_at(0.25, 0.7, 1280, 720), _person_at(0.8, 0.5, 1280, 720)], frame)
    status = detection_exclusion.status(1)
    assert status["suppressed_total"] == 0 and status["last_suppressed"] == []


def test_ai_status_reports_exclusion_evidence_per_camera(db, monkeypatch):
    _add_zone(geometry=LIVING_ROOM_ZONE)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2])
    monkeypatch.setattr(main, "ai_detection_state", {1: {"status": "running"}, 2: {"status": "running"}})
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    detection_exclusion.filter_detections(1, [_person_at(0.48, 0.55, 1280, 720)], frame)
    cameras = {entry["camera"]: entry for entry in main.ai_detection_status()["cameras"]}
    assert cameras[1]["exclusion_zones"]["zones_active"] == 1
    assert cameras[1]["exclusion_zones"]["suppressed_total"] == 1
    assert cameras[2]["exclusion_zones"] == {
        "suppressed_total": 0, "suppressed_by_class": {}, "last_suppressed_at": None,
        "last_suppressed": [], "zones_active": 0, "zones": [],
    }
