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
    assert "ai_stationary_memory.remember(camera_number, detection_signature, now_monotonic)" in source
    assert "ai_stationary_memory.is_repeat(" in source


def _frame(*boxes):
    return so.signature([{"class_name": c, "x": x, "y": y, "width": w, "height": h} for c, x, y, w, h in boxes])


# Camera 3, 20:45:37 -> 20:49:11 UTC on the live Ryzen after the first fix:
# the same parked cars every time, plus low-confidence boxes flickering in
# and out (the truck at ~1560,61 and a small car at ~2110,248). Comparing
# against only the last reported frame fired an event at every one of these.
CAM3 = [
    _frame(("car", 1185, 344, 1072, 608), ("car", 2116, 223, 442, 206), ("car", 596, 62, 127, 96), ("car", 2380, 143, 178, 96), ("truck", 1109, 37, 319, 153)),
    _frame(("car", 1184, 345, 1068, 607), ("car", 2117, 223, 441, 205), ("car", 592, 55, 130, 102), ("truck", 1107, 40, 318, 148), ("truck", 1562, 61, 267, 148)),
    _frame(("car", 1185, 343, 1071, 611), ("car", 2120, 223, 438, 207), ("car", 598, 65, 128, 94), ("car", 2110, 248, 149, 109), ("truck", 1108, 40, 321, 149)),
    _frame(("car", 1185, 344, 1069, 610), ("car", 2114, 222, 444, 208), ("car", 595, 62, 128, 95), ("truck", 1108, 40, 323, 150), ("truck", 1560, 64, 275, 144)),
    _frame(("car", 1185, 344, 1072, 608), ("car", 2116, 223, 442, 206), ("car", 2380, 143, 178, 96), ("truck", 1109, 37, 319, 153)),
]


def _replay(memory, frames, start=0.0, step=70.0):
    reported = []
    for i, frame in enumerate(frames):
        now = start + i * step
        if not memory.is_repeat(3, frame, now):
            memory.remember(3, frame, now)
            reported.append(i)
    return reported


def test_flickering_boxes_around_parked_cars_stop_re_firing():
    reported = _replay(so.StationaryMemory(rearm_seconds=1800), CAM3)
    # frame 0 (first sight), frame 1 (the 1560,61 truck first appears) and
    # frame 2 (the 2110,248 car first appears); frames 3-4 only contain
    # boxes already seen -> no more events.
    assert reported == [0, 1, 2]


def test_the_memory_still_reports_a_genuinely_new_arrival():
    memory = so.StationaryMemory(rearm_seconds=1800)
    _replay(memory, CAM3)
    arrival = CAM3[0] + _frame(("car", 300, 700, 400, 250))
    assert not memory.is_repeat(3, arrival, 400.0)


def test_memory_entries_rearm_after_the_window_and_zero_disables():
    memory = so.StationaryMemory(rearm_seconds=100)
    memory.remember(3, CAM3[0], 0.0)
    assert memory.is_repeat(3, CAM3[0], 50.0)
    assert not memory.is_repeat(3, CAM3[0], 150.0)
    off = so.StationaryMemory(rearm_seconds=0)
    off.remember(3, CAM3[0], 0.0)
    assert not off.is_repeat(3, CAM3[0], 1.0)


def test_memory_is_per_camera():
    memory = so.StationaryMemory(rearm_seconds=1800)
    memory.remember(3, CAM3[0], 0.0)
    assert not memory.is_repeat(2, CAM3[0], 10.0)
