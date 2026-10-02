"""Pure-function tests for app/local_recording_policy.py -- the Local
Event-mode recording decision logic. No ffmpeg, no filesystem, no
camera, no FastAPI: every test here feeds plain datetimes in and
checks a plain bool/datetime out.

Two things are tested, matching the module's own two questions:
1. is_buffer_segment_still_needed() / buffer_retention_cutoff() -- the
   mechanism that actually stops an idle Event-mode camera from
   accumulating disk usage.
2. should_start_new_event_recording() -- merge vs. start-fresh,
   including the max-length safety cap event_clips.should_merge()
   alone does not provide.
"""
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_recording_policy import (
    BufferSegment,
    buffer_retention_cutoff,
    is_buffer_segment_still_needed,
    should_start_new_event_recording,
)

NOW = datetime(2026, 9, 20, 12, 0, 0)


# --------------------------------------------------- buffer retention (the disk-usage fix)


def test_buffer_retention_cutoff_is_now_minus_pre_roll_minus_margin():
    cutoff = buffer_retention_cutoff(NOW, pre_roll_seconds=10, safety_margin_seconds=5)
    assert cutoff == NOW - timedelta(seconds=15)


def test_a_segment_within_the_pre_roll_lookback_is_still_needed():
    segment = BufferSegment(start=NOW - timedelta(seconds=20), end=NOW - timedelta(seconds=5))
    assert is_buffer_segment_still_needed(segment, now=NOW, pre_roll_seconds=10, safety_margin_seconds=5) is True


def test_a_segment_older_than_the_pre_roll_lookback_is_safe_to_delete():
    """This is the actual disk-usage fix: an Event-mode camera with no
    activity for 5 hours must not have 5 hours of buffer segments still
    sitting on disk -- each one ages out and is deleted almost
    immediately once it can no longer contribute pre-roll to a future
    event."""
    segment = BufferSegment(start=NOW - timedelta(hours=5, seconds=40), end=NOW - timedelta(hours=5, seconds=25))
    assert is_buffer_segment_still_needed(segment, now=NOW, pre_roll_seconds=10, safety_margin_seconds=5) is False


def test_a_segment_at_exactly_the_cutoff_is_still_needed_not_off_by_one():
    cutoff = buffer_retention_cutoff(NOW, pre_roll_seconds=10, safety_margin_seconds=5)
    segment = BufferSegment(start=cutoff - timedelta(seconds=30), end=cutoff)
    assert is_buffer_segment_still_needed(segment, now=NOW, pre_roll_seconds=10, safety_margin_seconds=5) is True


def test_an_old_segment_is_still_kept_while_an_event_build_is_actively_reading_it():
    """The janitor must never race a real in-progress extraction --
    even a segment old enough to otherwise be deleted stays if it
    overlaps a window some event build currently depends on."""
    old_segment = BufferSegment(start=NOW - timedelta(minutes=10), end=NOW - timedelta(minutes=10) + timedelta(seconds=30))
    in_flight = (old_segment.start - timedelta(seconds=5), old_segment.end + timedelta(seconds=5))
    assert is_buffer_segment_still_needed(
        old_segment, now=NOW, pre_roll_seconds=10, safety_margin_seconds=5,
        in_flight_event_windows=(in_flight,),
    ) is True


def test_an_old_segment_with_an_unrelated_in_flight_window_is_still_deleted():
    old_segment = BufferSegment(start=NOW - timedelta(minutes=10), end=NOW - timedelta(minutes=10) + timedelta(seconds=30))
    unrelated_window = (NOW - timedelta(seconds=5), NOW + timedelta(seconds=5))
    assert is_buffer_segment_still_needed(
        old_segment, now=NOW, pre_roll_seconds=10, safety_margin_seconds=5,
        in_flight_event_windows=(unrelated_window,),
    ) is False


# --------------------------------------------------- merge vs. start-new (with the safety cap)


def test_a_detection_well_within_the_merge_gap_extends_the_current_recording():
    """15 seconds of motion, then 3 more seconds of motion 2 seconds
    later, must be one recording -- not two tiny files."""
    assert should_start_new_event_recording(
        current_recording_start=NOW,
        current_recording_end=NOW + timedelta(seconds=15),
        detection_start=NOW + timedelta(seconds=17),
        merge_gap_seconds=8,
    ) is False


def test_a_detection_after_the_merge_gap_starts_a_new_recording():
    """Real, unrelated visits hours apart must never be merged into one
    file just because they happened on the same camera."""
    assert should_start_new_event_recording(
        current_recording_start=NOW,
        current_recording_end=NOW + timedelta(seconds=15),
        detection_start=NOW + timedelta(hours=5),
        merge_gap_seconds=8,
    ) is True


def test_merging_is_declined_once_it_would_exceed_the_max_length_safety_cap():
    """A single, literally unbroken activity period (e.g. a busy
    street) must still roll over into a new file eventually, even
    though every individual detection is well within the merge gap --
    this is a safety cap, never the normal clip-length rule."""
    assert should_start_new_event_recording(
        current_recording_start=NOW,
        current_recording_end=NOW + timedelta(seconds=295),
        detection_start=NOW + timedelta(seconds=301),
        merge_gap_seconds=8,
        max_event_recording_seconds=300,
    ) is True


def test_the_safety_cap_never_fires_for_a_normal_human_scale_event():
    """The cap must not become the de facto clip-length for ordinary
    events -- a normal 15-second event well under the cap merges
    normally."""
    assert should_start_new_event_recording(
        current_recording_start=NOW,
        current_recording_end=NOW + timedelta(seconds=15),
        detection_start=NOW + timedelta(seconds=18),
        merge_gap_seconds=8,
        max_event_recording_seconds=300,
    ) is False


def test_a_recording_exactly_at_the_cap_boundary_is_not_rejected():
    """Off-by-one check: reaching precisely max_event_recording_seconds
    is allowed; only exceeding it forces a new file."""
    assert should_start_new_event_recording(
        current_recording_start=NOW,
        current_recording_end=NOW + timedelta(seconds=295),
        detection_start=NOW + timedelta(seconds=300),
        merge_gap_seconds=8,
        max_event_recording_seconds=300,
    ) is False
