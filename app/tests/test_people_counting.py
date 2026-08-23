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

from people_counting import CountingLine, CounterState, PeopleCounter, _box_of, _dedupe_same_frame, _distance, _iou, _side_of_line


def vertical_line(direction: str = "both") -> CountingLine:
    return CountingLine(x1=0.5, y1=0.0, x2=0.5, y2=1.0, direction=direction)


def det(x: float, y: float = 0.5) -> dict:
    return {"x": x, "y": y}


def det_box(x: float, y: float, w: float, h: float) -> dict:
    """A detection carrying box size, for the box-aware matching tests
    -- plain det() (no "w"/"h" keys) is what every earlier test in this
    file uses, and must keep behaving identically (see
    test_no_box_info_behaves_exactly_like_before_this_feature below)."""
    return {"x": x, "y": y, "w": w, "h": h}


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


# ============================================================ 13. velocity-aware matching: the real walk-test failure, reproduced and fixed


def test_a_large_jump_that_exceeds_raw_fixed_distance_still_matches_when_consistent_with_established_velocity():
    """Directly reproduces the mechanism the real Camera 1 walk-test
    trace proved: a track takes one small step (establishing a velocity
    estimate), then a much larger step whose RAW distance from the
    track's last known position exceeds max_match_distance -- but whose
    distance from the track's VELOCITY-EXTRAPOLATED predicted position
    does not. Counterfactual proof included: a plain fixed-distance-
    from-last-position check on that same jump is asserted to exceed
    the threshold FIRST, then the real update() call is asserted to
    still preserve the track (and, since this jump also crosses the
    line, correctly count the crossing) -- proving this is a genuine
    fix for the observed failure, not just a passing test."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20)])   # frame 1: track born at x=0.20
    counter.update([det(0.35)])   # frame 2: step of +0.15 -- establishes velocity vx=0.15, still under the 0.20 fixed-distance tolerance so this step alone proves nothing new yet

    # Counterfactual: prove a NAIVE fixed-distance-from-last-position
    # check on the next jump would have failed.
    raw_jump_distance = _distance(0.63, 0.5, 0.35, 0.5)
    assert raw_jump_distance > counter.max_match_distance, (
        f"test setup error: this jump ({raw_jump_distance:.3f}) must exceed max_match_distance "
        f"({counter.max_match_distance}) to actually exercise the fix"
    )

    events = counter.update([det(0.63)])  # frame 3: real jump is +0.28 -- exceeds max_match_distance from the raw last position (0.35), but only 0.13 from the velocity-predicted position (0.35+0.15=0.50)

    assert len(counter.tracks) == 1, "the SAME track must have been preserved, not replaced by a new one"
    assert len(events) == 1, "the line was genuinely crossed (0.35 is left of 0.5, 0.63 is right) and must be counted"
    assert counter.in_count == 1
    assert counter.occupancy == 1


def test_without_an_established_velocity_the_same_large_jump_still_loses_the_track_the_safe_baseline():
    """The flip side, proving the 'safe baseline' claim: a track with
    NO prior velocity (only just created) gets no benefit from
    prediction -- an equally large jump on a brand-new track behaves
    identically to the original fixed-distance-only logic and fails to
    match, exactly as before this change."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20)])  # frame 1: track born, no velocity yet
    counter.update([det(0.63)])  # frame 2: a big jump on a track with only one observation -- predicted position falls back to the track's own current position (zero assumed velocity), identical to the old behavior
    assert len(counter.tracks) == 2, "no established velocity to bridge the gap -- a second, separate track is created, matching pre-fix behavior"
    assert counter.in_count == 0  # the crossing is lost, exactly like the real walk-test


def test_velocity_prediction_still_respects_max_match_distance_not_an_unlimited_reach():
    """The fix must not become 'anything matches' -- a jump wildly
    inconsistent with the established velocity (not just larger, but in
    a different direction/magnitude than the trend predicts) must still
    fail to match if it's far enough from the PREDICTED position."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20, 0.3)])
    counter.update([det(0.35, 0.3)])  # establishes a small rightward velocity
    # A detection far from BOTH the last position and the prediction --
    # nothing about extrapolation should rescue an unrelated jump.
    events = counter.update([det(0.35, 0.95)])  # same x, wildly different y
    assert len(counter.tracks) == 2  # did not match -- a new track was created instead


# ============================================================ 14. velocity-aware matching does not merge or swap two people crossing near each other


def test_two_people_both_making_the_same_large_velocity_assisted_jump_stay_correctly_separate():
    """Both people independently exhibit the exact large-jump pattern
    from the fix above, at the same time, on either side of the frame
    (y=0.3 and y=0.7) -- proves prediction is computed and matched
    PER TRACK, not globally, so it can't accidentally let one track's
    velocity "reach into" and steal the other's detection."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20, 0.3), det(0.20, 0.7)])
    counter.update([det(0.35, 0.3), det(0.35, 0.7)])
    events = counter.update([det(0.63, 0.3), det(0.63, 0.7)])
    assert len(counter.tracks) == 2, "still exactly two people, two tracks -- no merge"
    assert len(events) == 2, "both crossings counted"
    assert counter.in_count == 2
    track_ids = {t.track_id for t in counter.tracks.values()}
    assert len(track_ids) == 2  # genuinely distinct identities, not one track duplicated


def test_two_people_crossing_close_together_do_not_swap_identities_under_velocity_matching():
    """Two people close together (y=0.45 and y=0.55, only 0.10 apart --
    close enough that a naive nearest-detection-only match could
    plausibly confuse them) moving in OPPOSITE directions at the same
    large-jump pace. Correct behavior: each track's own velocity
    predicts its own continuation, so track A (moving right) should
    keep matching the detection that continues moving right, and track
    B (moving left) should keep matching the one continuing left --
    not swap onto each other's path."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20, 0.45), det(0.80, 0.55)])   # A moving right (y=0.45), B moving left (y=0.55)
    counter.update([det(0.35, 0.45), det(0.65, 0.55)])   # A: +0.15, B: -0.15 -- velocities established, opposite directions
    a_track_id_before = min(counter.tracks, key=lambda tid: abs(counter.tracks[tid].cy - 0.45))
    b_track_id_before = min(counter.tracks, key=lambda tid: abs(counter.tracks[tid].cy - 0.55))
    assert a_track_id_before != b_track_id_before

    events = counter.update([det(0.63, 0.45), det(0.37, 0.55)])  # A continues right past the predicted 0.50, B continues left past the predicted 0.50

    assert len(counter.tracks) == 2, "no merge -- still two distinct people"
    # Identity preserved: the track that was near y=0.45 is still the
    # one that ended up on the RIGHT (it was always moving right), and
    # the one near y=0.55 is still on the LEFT.
    a_track_after = counter.tracks[a_track_id_before]
    b_track_after = counter.tracks[b_track_id_before]
    assert a_track_after.cx > 0.5, "the rightward-moving person's own track continued rightward, not swapped"
    assert b_track_after.cx < 0.5, "the leftward-moving person's own track continued leftward, not swapped"
    assert len(events) == 2  # both crossed (A: left->right = in; B: right->left = out)
    assert counter.in_count == 1
    assert counter.out_count == 1


def test_two_people_moving_in_the_same_direction_close_together_never_get_double_matched_to_one_track():
    """Both moving the same direction, close together (y=0.40 / 0.50)
    -- confirms the assignment logic never lets two detections both
    claim the same track (each track_id appears at most once as a
    match target per frame), even once predicted positions from two
    similarly-moving tracks could plausibly land close to each other."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20, 0.40), det(0.22, 0.50)])
    counter.update([det(0.35, 0.40), det(0.37, 0.50)])
    events = counter.update([det(0.63, 0.40), det(0.65, 0.50)])
    assert len(counter.tracks) == 2
    assert len(events) == 2
    assert counter.in_count == 2


# ============================================================ 15. box-aware matching: IoU + scale consistency, on top of velocity


def test_no_box_info_behaves_exactly_like_before_this_feature():
    """The safe-baseline guarantee for THIS feature, mirroring the one
    already proven for velocity: callers that never send "w"/"h" (every
    test above this section) must see byte-identical behavior. Runs the
    exact fast-jump velocity scenario from section 13 and confirms the
    same outcome."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det(0.20)])
    counter.update([det(0.35)])
    events = counter.update([det(0.63)])
    assert len(counter.tracks) == 1
    assert len(events) == 1
    assert counter.in_count == 1


def test_fast_crossing_bridged_by_iou_when_raw_velocity_prediction_alone_would_still_fail():
    """The core new mechanism: a jump SO large that even the velocity-
    predicted position (section 13's fix) is still too far away to pass
    the plain distance gate -- but the person's box is large enough
    (a big, close subject -- exactly the 'perspective' case) that the
    PREDICTED box and the ACTUAL detection box clearly overlap anyway.
    Counterfactual proof included: both the plain velocity-only distance
    AND the fixed max_match_distance are asserted to reject this jump
    on their own, then the real IoU is computed and asserted to clear
    MIN_IOU_FOR_MATCH, before checking that update() still preserves the
    track and counts the crossing."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det_box(0.25, 0.5, 0.45, 0.45)])   # frame 1: track born, side +1
    counter.update([det_box(0.35, 0.5, 0.46, 0.46)])   # frame 2: +0.10 step -- establishes velocity, still side +1

    # Counterfactuals: prove the OLD signals alone would reject this jump.
    predicted_x = 0.35 + (0.35 - 0.25)  # = 0.45, the velocity-only prediction from frame 13's mechanism
    raw_velocity_residual = _distance(0.72, 0.5, predicted_x, 0.5)
    assert raw_velocity_residual > counter.max_match_distance, (
        f"test setup error: velocity-predicted residual ({raw_velocity_residual:.3f}) must still exceed "
        f"max_match_distance ({counter.max_match_distance}) for this test to actually exercise the NEW box signal"
    )
    predicted_box = _box_of(predicted_x, 0.5, 0.46, 0.46)
    actual_box = _box_of(0.72, 0.5, 0.47, 0.47)
    real_iou = _iou(predicted_box, actual_box)
    assert real_iou >= 0.15, f"test setup error: IoU ({real_iou:.3f}) must clear MIN_IOU_FOR_MATCH to rescue this match"

    events = counter.update([det_box(0.72, 0.5, 0.47, 0.47)])  # frame 3: the big jump -- crosses to side -1

    assert len(counter.tracks) == 1, "the SAME track must have been preserved via the box-overlap signal"
    assert len(events) == 1, "the line was genuinely crossed (0.35 left of 0.5, 0.72 right) and must be counted"
    assert counter.in_count == 1  # new_side < 0 -> "in", by this module's fixed convention


def test_iou_gate_still_rejects_a_jump_with_no_real_box_overlap_not_an_unlimited_reach():
    """The box signal must not become 'anything matches' either -- a
    jump that is both far in raw distance AND has essentially zero box
    overlap (a small, distant-looking box landing far from the
    prediction) must still fail to match."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det_box(0.20, 0.5, 0.08, 0.08)])
    counter.update([det_box(0.30, 0.5, 0.08, 0.08)])
    events = counter.update([det_box(0.90, 0.9, 0.08, 0.08)])  # far away in both x and y, small box -- no overlap possible
    assert len(counter.tracks) == 2, "no rescue -- distance fails and there is no meaningful box overlap"
    assert counter.out_count == 0 and counter.in_count == 0


def test_perspective_box_growth_while_approaching_the_camera_does_not_fragment_the_track():
    """A person walking TOWARD the camera has a box that grows steadily
    frame to frame -- the scale-consistency penalty must tolerate this
    gradual, expected growth (not just identical-size matches) and keep
    the same track alive through it, then still correctly count the
    eventual crossing."""
    line = vertical_line()
    counter = PeopleCounter(line)
    sequence = [
        (0.20, 0.10, 0.10),
        (0.28, 0.14, 0.14),
        (0.36, 0.19, 0.19),
        (0.44, 0.25, 0.25),
        (0.56, 0.32, 0.32),  # crosses the line while still growing
    ]
    all_events = []
    for x, w, h in sequence:
        all_events.extend(counter.update([det_box(x, 0.5, w, h)]))
    assert len(counter.tracks) == 1, "one continuous track throughout the approach, despite the box roughly tripling in size"
    assert len(all_events) == 1
    assert counter.in_count == 1


def test_perspective_box_shrink_while_leaving_the_camera_does_not_fragment_the_track():
    """The mirror case: a person walking AWAY has a shrinking box --
    equally gradual, equally must not be penalized into a fragmented
    track."""
    line = vertical_line()
    counter = PeopleCounter(line)
    sequence = [
        (0.20, 0.32, 0.32),
        (0.28, 0.25, 0.25),
        (0.36, 0.19, 0.19),
        (0.44, 0.14, 0.14),
        (0.56, 0.09, 0.09),
    ]
    for x, w, h in sequence:
        counter.update([det_box(x, 0.5, w, h)])
    assert len(counter.tracks) == 1
    assert counter.in_count == 1


def test_intermittent_missed_detection_preserves_box_continuity_across_the_gap():
    """Box info must survive a missed-frame gap exactly like position
    does -- a track's last-known size is still what the next real
    re-match is compared against, not reset to zero."""
    line = vertical_line()
    counter = PeopleCounter(line, max_missed_frames=3)
    counter.update([det_box(0.20, 0.5, 0.20, 0.20)])
    counter.update([det_box(0.35, 0.5, 0.21, 0.21)])  # velocity established, side +1
    counter.update([])  # missed -- occlusion mid-crossing
    events = counter.update([det_box(0.63, 0.5, 0.22, 0.22)])  # reappears on the far side, consistent size
    assert len(counter.tracks) == 1
    assert len(events) == 1
    assert counter.in_count == 1  # new_side < 0 -> "in", by this module's fixed convention


# ============================================================ 16. two nearby people: no identity swaps or accidental merges (box-aware)


def test_two_nearby_people_same_direction_different_sizes_never_swap_or_merge():
    """Two people close together in y (0.45 / 0.55 -- only 0.10 apart)
    moving the SAME direction, with CLEARLY different, stable box sizes
    (a closer/larger person and a farther/smaller person) -- proves the
    scale signal keeps their identities straight even when they are
    close enough in y that position alone is a weaker disambiguator
    than in the earlier, non-box two-people tests."""
    line = vertical_line()
    counter = PeopleCounter(line)
    counter.update([det_box(0.20, 0.45, 0.30, 0.30), det_box(0.22, 0.55, 0.10, 0.10)])
    counter.update([det_box(0.32, 0.45, 0.30, 0.30), det_box(0.34, 0.55, 0.10, 0.10)])
    events = counter.update([det_box(0.56, 0.45, 0.30, 0.30), det_box(0.58, 0.55, 0.10, 0.10)])
    assert len(counter.tracks) == 2, "still two distinct people, two tracks"
    assert len(events) == 2, "both crossings counted"
    assert counter.in_count == 2
    # Identity check: each track's OWN box size stayed consistent with
    # the person it actually belongs to -- a swap would show up as one
    # track suddenly carrying the other's size.
    sizes = sorted(t.w for t in counter.tracks.values())
    assert sizes == pytest.approx([0.10, 0.30], abs=0.01)


def test_two_nearby_people_opposite_directions_different_sizes_never_swap():
    """Two people close together in y, moving TOWARD each other and
    crossing near the same moment -- the classic swap-risk scenario --
    with different, stable box sizes making a swap immediately
    detectable if it happened."""
    line = vertical_line()
    counter = PeopleCounter(line)
    # A: larger box, moving left->right (y=0.45). B: smaller box, moving right->left (y=0.55).
    counter.update([det_box(0.20, 0.45, 0.28, 0.28), det_box(0.80, 0.55, 0.09, 0.09)])
    counter.update([det_box(0.32, 0.45, 0.28, 0.28), det_box(0.68, 0.55, 0.09, 0.09)])
    a_id_before = min(counter.tracks, key=lambda tid: abs(counter.tracks[tid].cy - 0.45))
    b_id_before = min(counter.tracks, key=lambda tid: abs(counter.tracks[tid].cy - 0.55))
    assert a_id_before != b_id_before

    events = counter.update([det_box(0.56, 0.45, 0.28, 0.28), det_box(0.44, 0.55, 0.09, 0.09)])

    assert len(counter.tracks) == 2, "no merge -- still two distinct people"
    assert len(events) == 2
    assert counter.in_count == 1
    assert counter.out_count == 1
    # Identity check: track A (near y=0.45) must still be the LARGE-box
    # track, and track B (near y=0.55) must still be the SMALL-box one
    # -- proves no swap happened even though both crossed at the same
    # moment on nearby paths.
    track_a_after = counter.tracks[a_id_before]
    track_b_after = counter.tracks[b_id_before]
    assert track_a_after.w == pytest.approx(0.28, abs=0.01)
    assert track_b_after.w == pytest.approx(0.09, abs=0.01)


def test_two_nearby_people_never_merge_into_a_single_track_across_a_close_pass():
    """A dedicated stress test for the 'no accidental merge' half of the
    safeguard: two people with stable, distinct box sizes pass close by
    each other (their y-separation narrows to 0.06 at the closest
    point, tighter than in the tests above) and then separate again --
    at no point during the sequence may the tracker collapse them into
    one track."""
    line = vertical_line()
    counter = PeopleCounter(line)
    frames = [
        (det_box(0.20, 0.40, 0.28, 0.28), det_box(0.22, 0.62, 0.09, 0.09)),
        (det_box(0.32, 0.43, 0.28, 0.28), det_box(0.34, 0.58, 0.09, 0.09)),
        (det_box(0.44, 0.46, 0.28, 0.28), det_box(0.46, 0.52, 0.09, 0.09)),  # closest approach: 0.06 apart in y
        (det_box(0.56, 0.43, 0.28, 0.28), det_box(0.58, 0.58, 0.09, 0.09)),
    ]
    for frame in frames:
        counter.update(list(frame))
        assert len(counter.tracks) == 2, "must never collapse to one track, even at closest approach"
    sizes = sorted(t.w for t in counter.tracks.values())
    assert sizes == pytest.approx([0.09, 0.28], abs=0.01)
