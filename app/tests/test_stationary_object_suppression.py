"""Stationary-object event suppression (2026-09-24, confirmed live on Ryzen):
a parked car re-reported every 30s cooldown produced a new event and a new
~4.4 MB S3 clip each time -- ~190 clips/hour, ~3.8 Mbps of uplink."""
from pathlib import Path

import stationary_objects as so

# The real, unchanged boxes camera 3 reported every ~35s (two parked cars).
PARKED = [
    {"class_name": "car", "confidence": 0.88, "x": 623, "y": 164, "width": 513, "height": 294},
    {"class_name": "car", "confidence": 0.58, "x": 945, "y": 79, "width": 334, "height": 163},
]
NEXT_FRAME = [
    {"class_name": "car", "confidence": 0.86, "x": 624, "y": 163, "width": 511, "height": 295},
    {"class_name": "car", "confidence": 0.61, "x": 944, "y": 80, "width": 335, "height": 161},
]


def _repeat(previous, current, seconds=35.0, **kwargs):
    return so.is_stationary_repeat(so.signature(previous), so.signature(current), seconds, **kwargs)


def test_the_same_parked_cars_are_a_repeat_not_a_new_event():
    assert _repeat(PARKED, NEXT_FRAME)


def test_one_of_the_parked_cars_leaving_is_not_a_new_event():
    assert _repeat(PARKED, NEXT_FRAME[:1])


def test_a_new_arrival_next_to_parked_cars_is_a_new_event():
    arriving = NEXT_FRAME + [{"class_name": "car", "x": 100, "y": 300, "width": 300, "height": 200}]
    assert not _repeat(PARKED, arriving)


def test_a_person_walking_past_a_parked_car_is_a_new_event():
    person = NEXT_FRAME + [{"class_name": "person", "x": 700, "y": 200, "width": 80, "height": 220}]
    assert not _repeat(PARKED, person)


def test_real_movement_is_a_new_event():
    moved = [dict(PARKED[0], x=PARKED[0]["x"] + 200)]
    assert not _repeat(PARKED[:1], moved)


def test_same_box_different_class_is_a_new_event():
    assert not _repeat(PARKED[:1], [dict(PARKED[0], class_name="truck")])


def test_a_still_object_is_re_reported_after_the_rearm_window():
    assert not _repeat(PARKED, NEXT_FRAME, seconds=so.STATIONARY_REARM_SECONDS)
    assert not _repeat(PARKED, NEXT_FRAME, seconds=10, rearm_seconds=5)


def test_rearm_zero_disables_suppression_and_first_frame_is_never_a_repeat():
    assert not _repeat(PARKED, NEXT_FRAME, rearm_seconds=0)
    assert not so.is_stationary_repeat(None, so.signature(NEXT_FRAME), 35.0)
    assert not so.is_stationary_repeat(so.signature(PARKED), [], 35.0)


def test_malformed_detections_are_ignored():
    assert so.signature([{"class_name": "car"}, "junk", {"class_name": "car", "x": 1, "y": 1, "width": 0, "height": 5}]) == []


def test_the_ai_detector_skips_stationary_repeats_and_remembers_what_it_reported():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "and not stationary_repeat" in source
    assert "ai_last_reported_signature[camera_number] = detection_signature" in source
