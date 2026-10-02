"""Fast/pure tests only -- no ffmpeg, no YOLO, no asyncio, no network.
smart_motion.py deliberately has zero I/O, so every test just exercises
record_object_detection()/classify_motion() directly against the
module's own in-memory state, reset before and after each test the
same way test_recording_uploader_motion_gate.py resets its own
module-level state.
"""

import pytest

import smart_motion as sm


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    sm.reset_state()
    monkeypatch.setattr(sm, "SMART_MOTION_ENABLED", True)
    monkeypatch.setattr(sm, "CORRELATION_WINDOW_SECONDS", 45.0)
    monkeypatch.setattr(sm, "SMART_MOTION_CLASSES", frozenset(sm._DEFAULT_CLASSES.split(",")))
    yield
    sm.reset_state()


def test_no_detections_means_no_classification():
    assert sm.classify_motion(1, at=1000.0) is None


def test_a_recent_person_detection_correlates():
    sm.record_object_detection(1, "person", at=1000.0)
    assert sm.classify_motion(1, at=1010.0) == "person"


def test_a_detection_just_inside_the_window_still_correlates():
    sm.record_object_detection(1, "person", at=1000.0)
    assert sm.classify_motion(1, at=1000.0 + sm.CORRELATION_WINDOW_SECONDS) == "person"


def test_a_detection_just_outside_the_window_does_not_correlate():
    sm.record_object_detection(1, "person", at=1000.0)
    assert sm.classify_motion(1, at=1000.0 + sm.CORRELATION_WINDOW_SECONDS + 0.01) is None


def test_a_detection_on_a_different_camera_does_not_correlate():
    sm.record_object_detection(2, "person", at=1000.0)
    assert sm.classify_motion(1, at=1005.0) is None


def test_a_class_not_in_smart_motion_classes_is_never_recorded():
    sm.record_object_detection(1, "suitcase", at=1000.0)
    assert sm.classify_motion(1, at=1001.0) is None


@pytest.mark.parametrize("vehicle_class", ["car", "truck", "bus", "motorcycle", "bicycle"])
def test_vehicle_subclasses_all_correlate(vehicle_class):
    sm.record_object_detection(1, vehicle_class, at=1000.0)
    assert sm.classify_motion(1, at=1005.0) == vehicle_class


@pytest.mark.parametrize("animal_class", ["dog", "cat", "bird"])
def test_animal_classes_all_correlate(animal_class):
    sm.record_object_detection(1, animal_class, at=1000.0)
    assert sm.classify_motion(1, at=1005.0) == animal_class


def test_person_outranks_vehicle_when_both_seen_in_window():
    sm.record_object_detection(1, "car", at=1000.0)
    sm.record_object_detection(1, "person", at=1002.0)
    assert sm.classify_motion(1, at=1005.0) == "person"


def test_vehicle_outranks_animal_when_both_seen_in_window():
    sm.record_object_detection(1, "dog", at=1000.0)
    sm.record_object_detection(1, "car", at=1001.0)
    assert sm.classify_motion(1, at=1005.0) == "car"


def test_priority_holds_regardless_of_recording_order():
    # A later, lower-priority detection must not override an earlier,
    # higher-priority one still inside the window.
    sm.record_object_detection(1, "person", at=1000.0)
    sm.record_object_detection(1, "cat", at=1010.0)
    assert sm.classify_motion(1, at=1015.0) == "person"


def test_disabled_flag_always_returns_none_even_with_a_fresh_detection(monkeypatch):
    monkeypatch.setattr(sm, "SMART_MOTION_ENABLED", False)
    sm.record_object_detection(1, "person", at=1000.0)
    assert sm.classify_motion(1, at=1001.0) is None


def test_per_camera_tracking_is_independent():
    sm.record_object_detection(1, "person", at=1000.0)
    sm.record_object_detection(2, "dog", at=1000.0)
    assert sm.classify_motion(1, at=1005.0) == "person"
    assert sm.classify_motion(2, at=1005.0) == "dog"


def test_bucket_is_bounded_per_camera():
    for index in range(sm._MAX_TRACKED_PER_CAMERA + 20):
        sm.record_object_detection(1, "person", at=1000.0 + index)
    assert len(sm._recent_detections[1]) == sm._MAX_TRACKED_PER_CAMERA


def test_negative_elapsed_time_does_not_correlate():
    # A detection recorded "in the future" relative to `at` (clock
    # skew/reordering) must not be treated as a valid correlation.
    sm.record_object_detection(1, "person", at=2000.0)
    assert sm.classify_motion(1, at=1000.0) is None


def test_custom_classes_env_style_configuration_is_respected(monkeypatch):
    monkeypatch.setattr(sm, "SMART_MOTION_CLASSES", frozenset({"person"}))
    sm.record_object_detection(1, "dog", at=1000.0)
    sm.record_object_detection(1, "person", at=1000.0)
    assert sm.classify_motion(1, at=1005.0) == "person"
    sm.reset_state()
    sm.record_object_detection(1, "dog", at=1000.0)
    assert sm.classify_motion(1, at=1005.0) is None
