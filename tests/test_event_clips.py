"""Punch-list item 4: event clip windowing and merge-nearby-detections.

Fully behavioral -- app/event_clips.py has no dependency beyond the stdlib
datetime module.
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from event_clips import compute_clip_window, merge_event_windows, should_merge  # noqa: E402

MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def dt(seconds_offset=0):
    return datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=seconds_offset)


class ComputeClipWindowTests(unittest.TestCase):
    def test_a_5_second_event_yields_roughly_a_15_second_clip(self):
        window = compute_clip_window(dt(0), dt(5))
        self.assertEqual((window.end - window.start).total_seconds(), 15)
        self.assertEqual(window.start, dt(-5))
        self.assertEqual(window.end, dt(10))

    def test_a_12_second_event_yields_roughly_a_22_second_clip(self):
        window = compute_clip_window(dt(0), dt(12))
        self.assertEqual((window.end - window.start).total_seconds(), 22)

    def test_a_zero_duration_instant_detection_still_gets_pre_and_post_roll(self):
        window = compute_clip_window(dt(0), dt(0))
        self.assertEqual((window.end - window.start).total_seconds(), 10)

    def test_clip_is_never_a_fixed_15_seconds_regardless_of_event_length(self):
        # The explicit thing the user does NOT want: every clip being the
        # same fixed length no matter how long the real event was.
        short = compute_clip_window(dt(0), dt(1))
        long = compute_clip_window(dt(0), dt(30))
        self.assertNotEqual(
            (short.end - short.start).total_seconds(),
            (long.end - long.start).total_seconds(),
        )
        self.assertEqual((short.end - short.start).total_seconds(), 11)
        self.assertEqual((long.end - long.start).total_seconds(), 40)

    def test_custom_pre_and_post_roll(self):
        window = compute_clip_window(dt(0), dt(0), pre_roll_seconds=3, post_roll_seconds=7)
        self.assertEqual(window.start, dt(-3))
        self.assertEqual(window.end, dt(7))

    def test_event_end_before_start_is_rejected(self):
        with self.assertRaises(ValueError):
            compute_clip_window(dt(10), dt(0))

    def test_window_start_is_clamped_to_earliest_available_footage(self):
        earliest = dt(-2)
        window = compute_clip_window(dt(0), dt(5), earliest_available=earliest)
        self.assertEqual(window.start, earliest)  # not dt(-5), which predates recording
        self.assertEqual(window.end, dt(10))  # post-roll is untouched by the clamp


class ShouldMergeTests(unittest.TestCase):
    def test_detections_seconds_apart_merge(self):
        self.assertTrue(should_merge(dt(0), dt(3)))

    def test_detections_far_apart_do_not_merge(self):
        self.assertFalse(should_merge(dt(0), dt(3600)))

    def test_overlapping_detections_merge(self):
        self.assertTrue(should_merge(dt(10), dt(5)))  # next starts before previous ends

    def test_exactly_at_the_gap_boundary_merges(self):
        self.assertTrue(should_merge(dt(0), dt(8)))  # default gap is 8s

    def test_custom_merge_gap(self):
        self.assertFalse(should_merge(dt(0), dt(3), merge_gap_seconds=2))
        self.assertTrue(should_merge(dt(0), dt(3), merge_gap_seconds=3))


class MergeEventWindowsTests(unittest.TestCase):
    def test_a_burst_of_near_simultaneous_detections_becomes_one_event(self):
        events = [(dt(0), dt(2)), (dt(4), dt(6)), (dt(9), dt(11))]
        merged = merge_event_windows(events)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].start, dt(0))
        self.assertEqual(merged[0].end, dt(11))

    def test_unrelated_visits_far_apart_stay_separate(self):
        events = [(dt(0), dt(2)), (dt(3600), dt(3602))]
        merged = merge_event_windows(events)
        self.assertEqual(len(merged), 2)

    def test_input_order_does_not_matter(self):
        events = [(dt(9), dt(11)), (dt(0), dt(2)), (dt(4), dt(6))]
        merged = merge_event_windows(events)
        self.assertEqual(len(merged), 1)

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(merge_event_windows([]), [])

    def test_a_single_event_is_unchanged(self):
        merged = merge_event_windows([(dt(0), dt(5))])
        self.assertEqual(merged, [(dt(0), dt(5))])

    def test_three_overlapping_bursts_of_different_durations_produce_two_events(self):
        events = [
            (dt(0), dt(3)), (dt(5), dt(8)),      # cluster 1: 0-8
            (dt(100), dt(102)),                   # cluster 2: 100-102, alone
        ]
        merged = merge_event_windows(events)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], (dt(0), dt(8)))
        self.assertEqual(merged[1], (dt(100), dt(102)))


class LinkedRecordingWiringTests(unittest.TestCase):
    """app/main.py can't be imported in this dev environment (FastAPI) --
    static source checks for the wiring, matching this suite's established
    pattern."""

    def test_linked_recording_for_accepts_an_event_end_time_and_uses_compute_clip_window(self):
        start = MAIN_SOURCE.index("def linked_recording_for(")
        body = MAIN_SOURCE[start:start + 1600]
        self.assertIn("event_end_time: datetime | None = None", body)
        self.assertIn("from event_clips import compute_clip_window", body)
        self.assertIn("compute_clip_window(event_time, event_end_time or event_time)", body)

    def test_linked_recording_for_no_longer_returns_an_unbounded_offset_link(self):
        start = MAIN_SOURCE.index("def linked_recording_for(")
        body = MAIN_SOURCE[start:start + 2200]
        self.assertNotIn('#t={offset}', body)
        self.assertIn("#t={start_offset:.1f},{end_offset:.1f}", body)

    def test_store_motion_event_passes_the_real_event_end_time(self):
        self.assertIn("linked_recording=linked_recording_for(camera_number, start_time, end_time),", MAIN_SOURCE)


if __name__ == "__main__":
    unittest.main()
