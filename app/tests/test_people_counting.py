"""Tests for people_counting.py -- the People Counting tracker/line-
crossing engine. Pure Python, zero dependency on main.py/cv2/YOLO/the
app container or any database; runs anywhere plain Python 3 runs.

Every "difficult case" required for this engagement is proven here
deterministically against synthetic detection sequences -- this is the
correct, standard way to validate tracking/counting LOGIC before a
live-camera trial (a live trial can prove the algorithm behaves
sensibly on real footage and doesn't harm the appliance; it cannot, by
itself, prove the counting math is correct the way a controlled,
known-ground-truth synthetic sequence can).

A "frame" here is one call to PeopleCounter.update() with a list of
person-centroid detections in normalized 0.0-1.0 coordinates. The
counting line runs vertically down the middle of the frame
(x1=0.5,y1=0.0 -> x2=0.5,y2=1.0) for every test unless noted -- walking
from x<0.5 to x>0.5 is an IN crossing by this module's fixed
positive-side-is-inbound convention. All per-frame movement in these
sequences is kept to a fixed, safe 0.12 step (well under the default
0.20 match-distance tolerance, including across the single-frame gaps
used in the occlusion tests) so no test sits on a floating-point
matching-distance boundary.
"""

import pytest

from people_counting import CountingLine, CounterState, PeopleCounter, _dedupe_same_frame, _side_of_line


def vertical_line(direction: str = "both") -> CountingLine:
    return CountingLine(x1=0.5, y1=0.0, x2=0.5, y2=1.0, direction=direction)


def det(x: float, y: float = 0.5) -> dict:
    return {"x": x, "y": y}


# A standard, verified-safe left-to-right walk that crosses x=0.5
# between the 3rd and 4th points (0.44 -> 0.56), and a mirrored
# right-to-left walk that crosses between the same two points reversed.
WALK_IN = (0.20, 0.32, 0.44, 0.56, 0.68, 0.80)
WALK_OUT = tuple(reversed(WALK_IN))


# ============================================================ 1. one person crossing normally


def test_one_person_crossing_left_to_right_counts_one_in():
    counter = PeopleCounter(vertical_line())
    for x in WALK_IN:
        counter.update([det(x)])
    assert counter.in_count == 1
    assert counter.out_count == 0
    assert counter.occupancy == 1


def test_one_person_crossing_right_to_left_counts_one_out():
    counter = PeopleCounter(vertical_line())
    for x in WALK_OUT:
        counter.update([det(x)])
    assert counter.out_count == 1
    assert counter.in_count == 0
    assert counter.occupancy == -1


def test_the_specific_frame_the_crossing_is_detected_on_is_reported():
    counter = PeopleCounter(vertical_line())
    all_events = []
    for x in WALK_IN[:4]:  # 0.20, 0.32, 0.44, 0.56 -- crosses on the 4th update() call
        all_events.extend(counter.update([det(x)]))
    assert len(all_events) == 1
    assert all_events[0].direction == "in"
    assert all_events[0].frame_index == 4


# ============================================================ 2. crossing in both directions


def test_crossing_both_directions_updates_in_out_and_occupancy_correctly():
    counter = PeopleCounter(vertical_line())
    for x in (0.20, 0.32, 0.44, 0.56, 0.68):  # crosses in
        counter.update([det(x)])
    for x in (0.80, 0.92):  # keeps moving away, no second crossing
        counter.update([det(x)])
    assert counter.occupancy == 1
    for x in (0.80, 0.68, 0.56, 0.44):  # walks back across
        counter.update([det(x)])
    assert counter.in_count == 1
    assert counter.out_count == 1
    assert counter.occupancy == 0


def test_direction_filter_inbound_only_ignores_outbound_crossings():
    counter = PeopleCounter(vertical_line(direction="inbound"))
    for x in WALK_OUT:  # a pure right-to-left (out) walk
        counter.update([det(x)])
    assert counter.out_count == 0  # filtered out -- direction='inbound' only counts 'in'
    assert counter.in_count == 0


def test_direction_filter_outbound_only_ignores_inbound_crossings():
    counter = PeopleCounter(vertical_line(direction="outbound"))
    for x in WALK_IN:  # a pure left-to-right (in) walk
        counter.update([det(x)])
    assert counter.in_count == 0
    assert counter.out_count == 0


# ============================================================ 3. multiple people crossing close together


def test_two_people_crossing_the_same_direction_close_together_are_both_counted():
    counter = PeopleCounter(vertical_line())
    # Person A at y=0.3, person B at y=0.7 -- far enough apart in y
    # (0.4) that nearest-neighbor matching never confuses the two
    # tracks even though they cross at nearly the same x each frame.
    for xa, xb in zip(WALK_IN, (0.22, 0.34, 0.46, 0.58, 0.70, 0.82)):
        counter.update([det(xa, 0.3), det(xb, 0.7)])
    assert counter.in_count == 2
    assert len(counter.tracks) == 2  # two distinct people, two distinct tracks


def test_two_people_crossing_opposite_directions_simultaneously_both_counted_correctly():
    counter = PeopleCounter(vertical_line())
    for xa, xb in zip(WALK_IN, WALK_OUT):
        counter.update([det(xa, 0.3), det(xb, 0.7)])
    assert counter.in_count == 1
    assert counter.out_count == 1
    assert counter.occupancy == 0


# ============================================================ 4. person approaches line but turns around


def test_person_approaching_the_line_then_turning_around_is_not_counted():
    counter = PeopleCounter(vertical_line())
    for x in (0.20, 0.30, 0.38, 0.44, 0.40, 0.30, 0.18):  # never actually crosses x=0.5
        counter.update([det(x)])
    assert counter.in_count == 0
    assert counter.out_count == 0
    assert counter.occupancy == 0


def test_person_touching_the_line_exactly_then_retreating_is_not_counted():
    counter = PeopleCounter(vertical_line())
    for x in (0.30, 0.40, 0.50, 0.40, 0.30):  # 0.50 is exactly ON the line (side == 0)
        counter.update([det(x)])
    assert counter.in_count == 0
    assert counter.out_count == 0


# ============================================================ 5. person stands on/near the line (no double count from jitter)


def test_person_jittering_on_the_line_across_consecutive_frames_is_not_double_counted():
    counter = PeopleCounter(vertical_line())
    for x in (0.30, 0.42):  # approach, land just left of the line
        counter.update([det(x)])
    # Realistic detection noise while someone stands near the line.
    for x in (0.51, 0.49, 0.52, 0.48, 0.51):
        counter.update([det(x)])
    # At most one crossing should have been counted despite repeated
    # sign flips within the cooldown window (default 2 frames).
    assert counter.in_count + counter.out_count <= 1


# ============================================================ 6. person crosses, disappears, and returns


def test_person_crosses_then_leaves_frame_briefly_then_reappears_same_side_not_double_counted():
    counter = PeopleCounter(vertical_line(), max_missed_frames=3)
    for x in (0.20, 0.32, 0.44, 0.56):  # crosses in
        counter.update([det(x)])
    assert counter.in_count == 1
    for _ in range(2):  # briefly out of frame -- within the missed-frames grace window
        counter.update([])
    counter.update([det(0.65)])  # reappears a bit further right, same side -- same track, no new crossing
    assert counter.in_count == 1
    assert counter.out_count == 0


def test_person_crosses_disappears_returns_and_crosses_back_counts_the_second_crossing():
    counter = PeopleCounter(vertical_line(), max_missed_frames=3)
    for x in (0.20, 0.32, 0.44, 0.56):  # crosses in (last_crossed_frame = 4)
        counter.update([det(x)])
    for _ in range(2):  # frames 5, 6 -- brief gap
        counter.update([])
    for x in (0.60, 0.48, 0.36):  # frames 7, 8, 9 -- reappears, genuinely walks back across
        counter.update([det(x)])
    assert counter.in_count == 1
    assert counter.out_count == 1
    assert counter.occupancy == 0


def test_disappearance_longer_than_grace_window_starts_a_new_track_a_disclosed_limitation():
    """Documents a real, disclosed limitation: if a person is gone for
    LONGER than max_missed_frames, reappearing creates a brand-new
    track with no known previous side -- a crossing that happened
    entirely during that gap is not detected. This is expected
    behavior for a simple centroid tracker at a low polling rate, not
    a bug; the report calls this out explicitly."""
    counter = PeopleCounter(vertical_line(), max_missed_frames=2)
    for x in (0.20, 0.30):  # still on the left, hasn't crossed yet
        counter.update([det(x)])
    for _ in range(5):  # gone far longer than the grace window
        counter.update([])
    assert len(counter.tracks) == 0  # the original track was pruned
    counter.update([det(0.70)])  # reappears on the right -- could have crossed while gone
    assert counter.in_count == 0  # not counted -- the new track has no "before" side to compare


# ============================================================ 7. temporary occlusion


def test_brief_occlusion_mid_crossing_still_counts_correctly_once_resolved():
    counter = PeopleCounter(vertical_line(), max_missed_frames=3)
    counter.update([det(0.42)])  # approaching, still on the left
    counter.update([])  # occluded for one frame right as they cross
    counter.update([det(0.54)])  # occlusion resolves on the far side -- 0.12 from last known position
    assert counter.in_count == 1


# ============================================================ 8. duplicate detections in the same frame


def test_duplicate_detections_in_the_same_frame_are_merged_not_double_counted():
    counter = PeopleCounter(vertical_line())
    # Two near-identical boxes for what is obviously one real person,
    # every frame, walking across.
    for x in (0.20, 0.32, 0.44, 0.56):
        counter.update([det(x), det(x + 0.005)])
    assert counter.in_count == 1
    assert len(counter.tracks) == 1


def test_dedupe_helper_does_not_merge_two_genuinely_distinct_close_people():
    """The dedupe distance must stay well below the tracker's own
    max_match_distance, or two real, closely-walking people would be
    wrongly merged into one."""
    deduped = _dedupe_same_frame([det(0.30, 0.3), det(0.30, 0.7)], merge_distance=0.06)
    assert len(deduped) == 2  # 0.4 apart in y -- clearly two different people


def test_dedupe_helper_merges_genuinely_duplicate_boxes():
    deduped = _dedupe_same_frame([det(0.30, 0.50), det(0.302, 0.501)], merge_distance=0.06)
    assert len(deduped) == 1


# ============================================================ 9. counter restart / persistence, and explicit reset


def test_fresh_counter_starts_at_zero_occupancy():
    counter = PeopleCounter(vertical_line())
    assert counter.in_count == 0
    assert counter.out_count == 0
    assert counter.occupancy == 0


def test_state_and_restore_round_trip_preserves_cumulative_counts_across_a_restart():
    counter = PeopleCounter(vertical_line())
    for x in (0.20, 0.32, 0.44, 0.56):
        counter.update([det(x)])
    saved = counter.state()
    assert saved.in_count == 1

    # Simulates an "analytics process restart" or "camera reconnect":
    # a brand new PeopleCounter (empty tracks) restores from the saved
    # state -- cumulative in/out counts survive; in-flight tracks do
    # not (see CounterState's own docstring on why that's the correct,
    # disclosed trade-off).
    restarted = PeopleCounter(vertical_line())
    restarted.restore(saved)
    assert restarted.in_count == 1
    assert restarted.occupancy == 1
    assert len(restarted.tracks) == 0  # in-flight tracks are never persisted


def test_explicit_admin_reset_sets_occupancy_and_future_deltas_move_from_there():
    counter = PeopleCounter(vertical_line())
    counter.reset_counts(occupancy=3)
    assert counter.occupancy == 3
    assert counter.in_count == 3
    assert counter.out_count == 0
    for x in (0.20, 0.32, 0.44, 0.56):  # one more real crossing after the reset
        counter.update([det(x)])
    assert counter.occupancy == 4


def test_reset_counts_never_goes_negative_even_with_a_bad_input():
    counter = PeopleCounter(vertical_line())
    counter.reset_counts(occupancy=-5)
    assert counter.occupancy == 0


# ============================================================ 10. no-person scenes


def test_empty_detections_every_frame_never_crashes_and_never_changes_counts():
    counter = PeopleCounter(vertical_line())
    for _ in range(10):
        events = counter.update([])
        assert events == []
    assert counter.occupancy == 0
    assert len(counter.tracks) == 0


# ============================================================ 11. geometry / line construction from real rule storage


def test_counting_line_builds_from_the_real_rule_geometry_shape():
    """Proves this reuses the EXACT geometry shape the existing
    line_crossing rule-builder already persists -- a list of {"x","y"}
    dict points -- rather than a second, incompatible format."""
    geometry = [{"x": 0.1, "y": 0.2}, {"x": 0.9, "y": 0.8}]
    line = CountingLine.from_rule_geometry(geometry, direction="inbound")
    assert line.x1 == 0.1 and line.y1 == 0.2
    assert line.x2 == 0.9 and line.y2 == 0.8
    assert line.direction == "inbound"


def test_counting_line_rejects_geometry_with_fewer_than_two_points():
    with pytest.raises(ValueError):
        CountingLine.from_rule_geometry([{"x": 0.5, "y": 0.5}])


def test_side_of_line_is_a_fixed_deterministic_convention():
    line = vertical_line()
    assert _side_of_line(line, 0.9, 0.5) != _side_of_line(line, 0.1, 0.5)
    assert _side_of_line(line, 0.5, 0.5) == 0  # exactly on the line


# ============================================================ 12. debug channel: additive, doesn't alter counting behavior


def test_debug_false_returns_only_events_exactly_as_before():
    """The default call shape used by all 26 tests above must be
    completely unaffected by the debug parameter's existence."""
    counter = PeopleCounter(vertical_line())
    result = counter.update([det(0.20)])
    assert isinstance(result, list)  # not a tuple -- unchanged shape


def test_debug_true_returns_events_and_debug_entries_as_a_tuple():
    counter = PeopleCounter(vertical_line())
    result = counter.update([det(0.20)], debug=True)
    assert isinstance(result, tuple) and len(result) == 2
    events, debug_entries = result
    assert isinstance(events, list)
    assert isinstance(debug_entries, list)


def test_debug_entries_report_new_track_with_no_prior_side():
    counter = PeopleCounter(vertical_line())
    events, debug_entries = counter.update([det(0.20)], debug=True)
    assert len(debug_entries) == 1
    entry = debug_entries[0]
    assert entry["track_status"] == "new_track"
    assert entry["prev_side"] is None
    assert entry["crossing_reason"] == "new_track_no_prior_side"


def test_debug_entries_report_a_counted_crossing_with_its_reason():
    counter = PeopleCounter(vertical_line())
    for x in (0.20, 0.32, 0.44):
        counter.update([det(x)], debug=True)
    events, debug_entries = counter.update([det(0.56)], debug=True)  # the crossing frame
    assert len(events) == 1
    assert debug_entries[0]["crossed"] is True
    assert debug_entries[0]["crossing_reason"] == "counted_in"
    assert debug_entries[0]["prev_side"] is not None
    assert debug_entries[0]["new_side"] is not None
    assert debug_entries[0]["prev_side"] != debug_entries[0]["new_side"]


def test_debug_entries_report_a_direction_filtered_crossing_with_its_reason():
    counter = PeopleCounter(vertical_line(direction="outbound"))
    for x in (0.20, 0.32, 0.44):
        counter.update([det(x)], debug=True)
    events, debug_entries = counter.update([det(0.56)], debug=True)
    assert len(events) == 0
    assert debug_entries[0]["crossed"] is False
    assert debug_entries[0]["crossing_reason"] == "side_changed_but_direction_filter_excludes_in"


def test_debug_entries_report_missed_frames_and_expiration():
    counter = PeopleCounter(vertical_line(), max_missed_frames=1)
    counter.update([det(0.20)], debug=True)
    events, debug_entries = counter.update([], debug=True)  # missed -- within grace (max=1)
    assert debug_entries[0]["track_status"] == "missed_this_frame"
    events, debug_entries = counter.update([], debug=True)  # missed again -- exceeds grace, pruned
    assert debug_entries[0]["track_status"] == "expired_pruned"


def test_debug_true_does_not_change_in_out_counts_versus_debug_false():
    """The most important guarantee: turning on debug=True changes
    nothing about the actual counting outcome -- run the identical
    sequence twice, once with debug on and once off, and confirm
    identical final in_count/out_count."""
    counter_plain = PeopleCounter(vertical_line())
    counter_debug = PeopleCounter(vertical_line())
    sequence = [0.20, 0.32, 0.44, 0.56, 0.68, 0.80, 0.68, 0.56, 0.44]
    for x in sequence:
        counter_plain.update([det(x)])
        counter_debug.update([det(x)], debug=True)
    assert counter_plain.in_count == counter_debug.in_count
    assert counter_plain.out_count == counter_debug.out_count
    assert counter_plain.occupancy == counter_debug.occupancy
