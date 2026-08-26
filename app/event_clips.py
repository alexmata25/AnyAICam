"""Customer-facing event clip windowing (punch-list item 4).

Pure, dependency-free functions -- no FastAPI, no ffmpeg, no filesystem --
so the actual rule ("5 seconds before the event + the real event duration
+ 5 seconds after") is fully unit-testable without a camera or a VMS
process. app/main.py wires these into store_motion_event() (and the
equivalent analytics-event path) to compute the {start, end} window handed
to the existing build_manual_clip() job instead of linking directly to the
raw multi-minute recording segment (see linked_recording_for()'s
docstring/history) -- a customer's event clip must be its own short
window, not a deep link into the full continuous recording.

Also implements the "merge nearby detections into one real event" rule,
so a burst of near-simultaneous or rapidly repeated detections produces
one clip, not several tiny overlapping ones.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

DEFAULT_PRE_ROLL_SECONDS = 5
DEFAULT_POST_ROLL_SECONDS = 5

# Two detections are treated as the same real event if the gap between
# them is this small or less. Chosen to bridge a person briefly leaving
# frame and re-entering (e.g. walking behind a pillar) without merging
# two genuinely unrelated visits hours apart.
DEFAULT_MERGE_GAP_SECONDS = 8


class ClipWindow(NamedTuple):
    start: datetime
    end: datetime


def compute_clip_window(
    event_start: datetime,
    event_end: datetime,
    *,
    pre_roll_seconds: int = DEFAULT_PRE_ROLL_SECONDS,
    post_roll_seconds: int = DEFAULT_POST_ROLL_SECONDS,
    earliest_available: datetime | None = None,
) -> ClipWindow:
    """The customer-facing clip window: pre-roll + the real event duration
    + post-roll. NOT a fixed 15 seconds -- a 5-second event yields a
    ~15-second clip, a 12-second event yields a ~22-second clip, matching
    the actual detected duration in both cases.

    event_end must be >= event_start (a zero-duration/instant detection is
    valid -- it just means the "event" term of the window is 0 seconds,
    still producing pre_roll + post_roll of footage).

    earliest_available (optional) clamps the window's start so it never
    requests footage from before local recording began for this camera --
    without silently shrinking the *event* portion, only the pre-roll.
    """
    if event_end < event_start:
        raise ValueError("event_end must not be before event_start.")
    window_start = event_start - timedelta(seconds=pre_roll_seconds)
    if earliest_available is not None and window_start < earliest_available:
        window_start = earliest_available
    window_end = event_end + timedelta(seconds=post_roll_seconds)
    return ClipWindow(window_start, window_end)


def should_merge(
    previous_end: datetime,
    next_start: datetime,
    *,
    merge_gap_seconds: int = DEFAULT_MERGE_GAP_SECONDS,
) -> bool:
    """True if next_start begins within merge_gap_seconds of previous_end
    (or overlaps it entirely) -- the two detections are the same real
    event, not two separate ones."""
    return (next_start - previous_end).total_seconds() <= merge_gap_seconds


def merge_event_windows(
    events: list[tuple[datetime, datetime]],
    *,
    merge_gap_seconds: int = DEFAULT_MERGE_GAP_SECONDS,
) -> list[ClipWindow]:
    """Collapses a list of (start, end) detection windows into merged
    real-event windows. Input order does not matter -- sorted internally
    by start time. Never merges two events whose gap exceeds
    merge_gap_seconds, so unrelated visits stay separate."""
    if not events:
        return []
    ordered = sorted(events, key=lambda item: item[0])
    merged: list[list[datetime]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        current = merged[-1]
        if should_merge(current[1], start, merge_gap_seconds=merge_gap_seconds):
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return [ClipWindow(start, end) for start, end in merged]
