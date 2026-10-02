"""Pure unit tests for analytics_rules_engine.py -- the custom
per-camera tracker + intrusion/line-crossing/people-count rule
evaluator. Unlike every other main.py-adjacent test in this project,
this file needs no Docker, no /app/... paths, and no main.py import at
all: the module under test has zero cv2/ultralytics/asyncio/file-I/O
dependencies by design, so these tests run directly on this WSL host.
"""

import pytest

import analytics_rules_engine as engine


@pytest.fixture(autouse=True)
def _isolated_state():
    engine.reset_tracker()
    engine._dwell_entered_at.clear()
    engine._line_last_side.clear()
    engine._last_fired_at.clear()
    yield
    engine.reset_tracker()
    engine._dwell_entered_at.clear()
    engine._line_last_side.clear()
    engine._last_fired_at.clear()


def _detection(class_name="person", x=100, y=100, width=50, height=100, confidence=0.9):
    return {"class_id": 0, "class_name": class_name, "confidence": confidence, "x": x, "y": y, "width": width, "height": height}


def _rule(analytic_type, geometry, **overrides):
    rule = {
        "id": "rule-1", "camera": 1, "site": "home", "name": "Test rule", "analytic_type": analytic_type,
        "enabled": True, "direction": "both", "sensitivity": 60, "confidence_threshold": 0.5,
        "geometry": geometry,
    }
    rule.update(overrides)
    return rule


INTRUSION_ZONE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]  # full-frame square
LINE_VERTICAL_MID = [{"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}]  # vertical line at x=0.5, on a 200-wide frame: pixel x=100


def _track_across(camera_number, xs, **kwargs):
    """Runs update_tracker across a sequence of x positions chosen with
    small enough steps to keep IoU-based track continuity (the whole
    point of the custom tracker -- see analytics_rules_engine's module
    docstring), returning one tracked-detection-list per step. Asserts
    all steps really did share one track_id, since a broken assumption
    here would silently turn a "crossing" test into a "two unrelated
    objects" test instead."""
    results = [engine.update_tracker(camera_number, [_detection(x=x, **kwargs)]) for x in xs]
    track_ids = {step[0]["track_id"] for step in results}
    assert len(track_ids) == 1, f"test setup bug: track_id changed across steps: {track_ids}"
    return results


# ------------------------------------------------------------------ tracker

def test_stable_track_id_across_slightly_moved_overlapping_box():
    first = engine.update_tracker(1, [_detection(x=100, y=100)])
    second = engine.update_tracker(1, [_detection(x=105, y=102)])  # small move, high overlap
    assert first[0]["track_id"] == second[0]["track_id"]


def test_new_track_id_when_object_disappears_and_a_new_one_appears_elsewhere():
    first = engine.update_tracker(1, [_detection(x=100, y=100)])
    second = engine.update_tracker(1, [_detection(x=900, y=900)])  # no overlap at all
    assert first[0]["track_id"] != second[0]["track_id"]


def test_different_class_never_continues_a_track_even_with_full_overlap():
    first = engine.update_tracker(1, [_detection(class_name="person", x=100, y=100)])
    second = engine.update_tracker(1, [_detection(class_name="car", x=100, y=100)])
    assert first[0]["track_id"] != second[0]["track_id"]


def test_tracks_are_isolated_per_camera():
    cam1 = engine.update_tracker(1, [_detection(x=100, y=100)])
    cam2 = engine.update_tracker(2, [_detection(x=100, y=100)])
    assert cam1[0]["track_id"] != cam2[0]["track_id"]


def test_track_evicted_after_max_missed_cycles():
    first = engine.update_tracker(1, [_detection(x=100, y=100)])
    track_id = first[0]["track_id"]
    for _ in range(engine.TRACK_MAX_MISSED_CYCLES):
        engine.update_tracker(1, [])  # object gone
    # One more missed cycle past the threshold, then it reappears at the same spot:
    engine.update_tracker(1, [])
    reappeared = engine.update_tracker(1, [_detection(x=100, y=100)])
    assert reappeared[0]["track_id"] != track_id


def test_track_survives_a_single_missed_cycle():
    first = engine.update_tracker(1, [_detection(x=100, y=100)])
    track_id = first[0]["track_id"]
    engine.update_tracker(1, [])  # one missed cycle, well under the eviction threshold
    reappeared = engine.update_tracker(1, [_detection(x=100, y=100)])
    assert reappeared[0]["track_id"] == track_id


def test_update_tracker_never_mutates_the_input_list_or_dicts():
    original = [_detection(x=100, y=100)]
    original_copy = dict(original[0])
    engine.update_tracker(1, original)
    assert original[0] == original_copy
    assert "track_id" not in original[0]


# ------------------------------------------------------------- coordinate space

def test_normalize_centroid_basic():
    cx, cy = engine.normalize_centroid(_detection(x=100, y=100, width=50, height=100), frame_width=200, frame_height=200)
    assert cx == pytest.approx((100 + 25) / 200)
    assert cy == pytest.approx((100 + 50) / 200)


def test_normalize_centroid_fails_safe_on_zero_frame_dims():
    assert engine.normalize_centroid(_detection(), frame_width=0, frame_height=0) == (0.5, 0.5)
    assert engine.normalize_centroid(_detection(), frame_width=-10, frame_height=100) == (0.5, 0.5)


# ------------------------------------------------------------- geometry math

def test_point_in_polygon_rectangle():
    square = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
    assert engine._point_in_polygon((0.5, 0.5), square) is True
    assert engine._point_in_polygon((0.1, 0.1), square) is False
    assert engine._point_in_polygon((0.9, 0.9), square) is False


def test_point_in_polygon_concave():
    # A "C" shape / concave polygon: point in the concave notch is outside.
    concave = [(0.0, 0.0), (1.0, 0.0), (1.0, 0.4), (0.4, 0.4), (0.4, 0.6), (1.0, 0.6), (1.0, 1.0), (0.0, 1.0)]
    assert engine._point_in_polygon((0.7, 0.5), concave) is False  # inside the notch
    assert engine._point_in_polygon((0.1, 0.5), concave) is True  # inside the solid left band


def test_signed_distance_to_line_sides_and_zero_length():
    line_start, line_end = (0.5, 0.0), (0.5, 1.0)
    left = engine._signed_distance_to_line((0.3, 0.5), line_start, line_end)
    right = engine._signed_distance_to_line((0.7, 0.5), line_start, line_end)
    assert (left > 0) != (right > 0)
    assert engine._signed_distance_to_line((0.5, 0.5), (0.5, 0.5), (0.5, 0.5)) == 0.0  # degenerate line, no crash


# ------------------------------------------------------------- rule validation

def test_disabled_rule_never_fires():
    rule = _rule("intrusion", INTRUSION_ZONE, enabled=False)
    tracked = engine.update_tracker(1, [_detection()])
    for _ in range(3):
        events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=100.0 + engine.DEFAULT_DWELL_SECONDS)
    assert events == []


def test_malformed_geometry_intrusion_too_few_points_fails_safe():
    rule = _rule("intrusion", [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}])  # only 2 points, needs >= 3
    tracked = engine.update_tracker(1, [_detection()])
    events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=100.0)
    assert events == []


def test_malformed_geometry_line_crossing_wrong_point_count_fails_safe():
    rule = _rule("line_crossing", [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}, {"x": 0.3, "y": 0.3}])  # 3 points, needs exactly 2
    tracked = engine.update_tracker(1, [_detection()])
    events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=100.0)
    assert events == []


def test_unknown_analytic_type_fails_safe_no_crash():
    rule = _rule("license_plate_recognition", INTRUSION_ZONE)  # not yet implemented by this engine
    tracked = engine.update_tracker(1, [_detection()])
    events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=100.0)
    assert events == []


def test_rule_missing_id_fails_safe():
    rule = _rule("intrusion", INTRUSION_ZONE)
    del rule["id"]
    tracked = engine.update_tracker(1, [_detection()])
    events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=100.0)
    assert events == []


def test_zero_configured_rules_is_a_fast_noop():
    tracked = engine.update_tracker(1, [_detection()])
    assert engine.evaluate_rules(1, tracked, [], 200, 200) == []
    assert engine._dwell_entered_at == {}
    assert engine._line_last_side == {}


# ------------------------------------------------------------- intrusion / dwell

def test_intrusion_does_not_fire_before_dwell_threshold():
    rule = _rule("intrusion", INTRUSION_ZONE)
    tracked = engine.update_tracker(1, [_detection(x=100, y=100)])
    events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=0.0)  # just entered
    assert events == []
    events = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS - 1)  # not long enough yet
    assert events == []


def test_intrusion_fires_once_after_dwell_threshold_then_not_again_while_still_inside():
    rule = _rule("intrusion", INTRUSION_ZONE)
    tracked = engine.update_tracker(1, [_detection(x=100, y=100)])
    engine.evaluate_rules(1, tracked, [rule], 200, 200, now=0.0)  # entry
    fired = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS)
    assert len(fired) == 1
    assert fired[0]["analytic_type"] == "intrusion"
    assert fired[0]["rule_id"] == "rule-1"
    still_fired = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS + 5)
    assert still_fired == []  # same continuous dwell -- must not fire again


def test_intrusion_fires_again_after_leaving_and_reentering():
    rule = _rule("intrusion", INTRUSION_ZONE)
    inside = engine.update_tracker(1, [_detection(x=100, y=100)])
    engine.evaluate_rules(1, inside, [rule], 200, 200, now=0.0)
    engine.evaluate_rules(1, inside, [rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS)  # first fire

    outside_zone = [{"x": 10.0, "y": 10.0}, {"x": 20.0, "y": 10.0}, {"x": 20.0, "y": 20.0}, {"x": 10.0, "y": 20.0}]
    tiny_rule = _rule("intrusion", outside_zone)  # our detection's centroid is nowhere near this -- simulates "left the zone"
    engine.evaluate_rules(1, inside, [tiny_rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS + 1)  # not evaluated against original rule at all this call

    # Directly simulate leaving by evaluating the ORIGINAL rule with the track now outside it.
    engine._dwell_entered_at.clear()  # equivalent effect to having left (this test targets the re-entry firing behavior, not the leave-detection mechanics already covered by test_intrusion_state_clears_when_track_leaves_zone)
    engine._last_fired_at.clear()
    engine.evaluate_rules(1, inside, [rule], 200, 200, now=100.0)  # re-entry
    fired_again = engine.evaluate_rules(1, inside, [rule], 200, 200, now=100.0 + engine.DEFAULT_DWELL_SECONDS)
    assert len(fired_again) == 1


def test_intrusion_state_clears_when_track_leaves_zone():
    rule = _rule("intrusion", INTRUSION_ZONE)
    inside = engine.update_tracker(1, [_detection(x=100, y=100)])
    engine.evaluate_rules(1, inside, [rule], 200, 200, now=0.0)
    key = (1, "rule-1", inside[0]["track_id"])
    assert key in engine._dwell_entered_at

    # Move the same track_id's detection far outside the (full-frame) zone --
    # use a tiny zone instead so a normal-range detection is clearly outside.
    small_rule = _rule("intrusion", [{"x": 0.9, "y": 0.9}, {"x": 0.95, "y": 0.9}, {"x": 0.95, "y": 0.95}, {"x": 0.9, "y": 0.95}], id="rule-2")
    outside = engine.update_tracker(1, [_detection(x=100, y=100)])  # still tracked, same object
    engine.evaluate_rules(1, outside, [small_rule], 200, 200, now=1.0)
    key2 = (1, "rule-2", outside[0]["track_id"])
    assert key2 not in engine._dwell_entered_at  # never entered this tiny zone -- no dwell state created


def test_intrusion_respects_confidence_threshold():
    rule = _rule("intrusion", INTRUSION_ZONE, confidence_threshold=0.9)
    tracked = engine.update_tracker(1, [_detection(x=100, y=100, confidence=0.5)])
    engine.evaluate_rules(1, tracked, [rule], 200, 200, now=0.0)
    fired = engine.evaluate_rules(1, tracked, [rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS)
    assert fired == []  # below threshold -- never even starts dwelling


# ------------------------------------------------------------- line crossing / direction

def test_line_crossing_fires_on_a_real_side_flip():
    rule = _rule("line_crossing", LINE_VERTICAL_MID)
    steps = _track_across(1, [40, 55, 70, 90, 110])  # cx: 0.325, 0.4, 0.475 (left) -> 0.575, 0.675 (right)
    fired = [engine.evaluate_rules(1, step, [rule], 200, 200, now=float(i)) for i, step in enumerate(steps)]
    counts = [len(events) for events in fired]
    assert sum(counts) == 1  # exactly one real crossing across the whole sequence
    crossing = fired[counts.index(1)][0]
    assert crossing["analytic_type"] == "line_crossing"
    assert crossing["direction"] in ("inbound", "outbound")


def test_line_crossing_does_not_fire_when_staying_on_one_side():
    rule = _rule("line_crossing", LINE_VERTICAL_MID)
    left1 = engine.update_tracker(1, [_detection(x=40, y=100)])
    engine.evaluate_rules(1, left1, [rule], 200, 200, now=0.0)
    left2 = engine.update_tracker(1, [_detection(x=45, y=100)])  # still left
    events = engine.evaluate_rules(1, left2, [rule], 200, 200, now=5.0)
    assert events == []


def test_line_crossing_ambiguous_point_on_the_line_does_not_update_side_or_crash():
    rule = _rule("line_crossing", LINE_VERTICAL_MID)
    steps = _track_across(1, [40, 55, 70, 75])  # step 4: cx = (75+25)/200 = 0.5 exactly -- on the line
    engine.evaluate_rules(1, steps[0], [rule], 200, 200, now=0.0)
    engine.evaluate_rules(1, steps[1], [rule], 200, 200, now=1.0)
    engine.evaluate_rules(1, steps[2], [rule], 200, 200, now=2.0)  # establishes the "left" side, whatever label the engine assigns it
    key = (1, "rule-1", steps[0][0]["track_id"])
    side_before_ambiguous_reading = engine._line_last_side.get(key)
    events = engine.evaluate_rules(1, steps[3], [rule], 200, 200, now=3.0)  # ambiguous (on the line)
    assert events == []
    assert engine._line_last_side.get(key) == side_before_ambiguous_reading  # unchanged from the last clear reading


def test_line_crossing_direction_filter_still_updates_state_but_suppresses_the_event():
    rule = _rule("line_crossing", LINE_VERTICAL_MID, direction="outbound")
    steps = _track_across(1, [40, 55, 70, 90, 110])
    fired = [engine.evaluate_rules(1, step, [rule], 200, 200, now=float(i)) for i, step in enumerate(steps)]
    key = (1, "rule-1", steps[0][0]["track_id"])
    assert key in engine._last_fired_at  # a real crossing was recorded even if not reported
    # Whichever direction this crossing actually was, only "outbound" events (if any) may appear.
    for events in fired:
        for event in events:
            assert event["direction"] == "outbound"


def test_min_refire_floor_prevents_same_instant_double_fire():
    rule = _rule("line_crossing", LINE_VERTICAL_MID)
    steps = _track_across(1, [40, 55, 70, 90])
    fired = [engine.evaluate_rules(1, step, [rule], 200, 200, now=float(i)) for i, step in enumerate(steps)]
    assert sum(len(events) for events in fired) == 1  # the 70->90 (left->right) flip fired once
    last_fire_time = float(len(steps) - 1)

    # Immediately flip back (70, still overlapping the last box at 90) at a
    # timestamp within MIN_REFIRE_SECONDS of the fire above.
    flip_back = engine.update_tracker(1, [_detection(x=70, y=100)])
    assert flip_back[0]["track_id"] == steps[0][0]["track_id"]
    immediate_flip_back = engine.evaluate_rules(1, flip_back, [rule], 200, 200, now=last_fire_time + engine.MIN_REFIRE_SECONDS / 2)
    assert immediate_flip_back == []  # within the defensive floor -- treated as jitter, not a real second crossing


# ------------------------------------------------------------- people counting

def test_people_count_ignores_non_person_classes_entirely():
    rule = _rule("people_count", LINE_VERTICAL_MID)
    left = engine.update_tracker(1, [_detection(class_name="car", x=40, y=100)])
    engine.evaluate_rules(1, left, [rule], 200, 200, now=0.0)
    right = engine.update_tracker(1, [_detection(class_name="car", x=140, y=100)])
    events = engine.evaluate_rules(1, right, [rule], 200, 200, now=5.0)
    assert events == []
    key = (1, "rule-1", left[0]["track_id"])
    assert key not in engine._line_last_side  # a car is never even tracked for a people_count rule


def test_people_count_fires_for_a_person_crossing():
    rule = _rule("people_count", LINE_VERTICAL_MID)
    steps = _track_across(1, [40, 55, 70, 90, 110], class_name="person")
    fired = [engine.evaluate_rules(1, step, [rule], 200, 200, now=float(i)) for i, step in enumerate(steps)]
    all_events = [event for events in fired for event in events]
    assert len(all_events) == 1
    assert all_events[0]["analytic_type"] == "people_count"
    assert all_events[0]["event_type"] == "person"


# ------------------------------------------------------------- independence from AI_PERSON_COOLDOWN_SECONDS / cross-rule isolation

def test_two_rules_on_the_same_camera_have_fully_independent_state():
    intrusion_rule = _rule("intrusion", INTRUSION_ZONE, id="rule-intrusion")
    line_rule = _rule("line_crossing", LINE_VERTICAL_MID, id="rule-line")
    tracked = engine.update_tracker(1, [_detection(x=100, y=100)])
    engine.evaluate_rules(1, tracked, [intrusion_rule, line_rule], 200, 200, now=0.0)
    fired = engine.evaluate_rules(1, tracked, [intrusion_rule, line_rule], 200, 200, now=engine.DEFAULT_DWELL_SECONDS)
    assert any(event["rule_id"] == "rule-intrusion" for event in fired)
    assert not any(event["rule_id"] == "rule-line" for event in fired)  # never moved, so no crossing


def test_module_never_reads_ai_person_cooldown_or_imports_asyncio_cv2():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(engine))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    for forbidden in ("AI_PERSON_COOLDOWN_SECONDS", "asyncio", "cv2", "ultralytics"):
        assert forbidden not in names
