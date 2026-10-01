"""Continuous AI activity on a camera is ONE event (owner decision 2026-10-01).

Before: people active in view for two minutes produced a new person event --
card, clip, recording, notification -- every ~30 s (AI_PERSON_COOLDOWN_SECONDS),
because the "same real event" rule (event_clips.should_merge, 8 s) was only
ever applied between SAVED detections, which the cooldown keeps ~30 s apart.

Now an activity opens with the first qualifying detection (one card, one
notification) and every later scan that still sees qualifying objects extends
it -- including scans the cooldown does not save. Its one clip and one
Event-mode recording are built when it has been quiet for the continuation gap
(or reaches the maximum length, default 300 s) and cover it from before its
start to the post-roll after its last sighting. A different kind of object
arriving during the activity still gets its own card, playing the activity's
clip. PPE, Face Access and Voice Call keep running on every saved detection,
exactly as before.

Pure (no I/O): main.py wires it into the detector loop and save_yolo_events()."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# How far before an open activity's start its footage must be kept until its
# recording is cut: the pre-roll plus the previous-scan lead
# (detection_timing.MAX_EVENT_LEAD_SECONDS), with margin.
MAX_LEAD_SECONDS = 60


def continuation_gap(merge_gap_seconds: float, scan_interval_seconds: float) -> float:
    """Longest pause between sightings that still counts as the same activity:
    the merge gap, but never less than two scans (one missed scan must not
    split an activity)."""
    return max(float(merge_gap_seconds), 2.0 * float(scan_interval_seconds))


@dataclass
class Activity:
    camera: int
    owner_id: str
    start: datetime
    first_moment: datetime
    last_seen: datetime
    # Processing-clock times. Frame times (start/last_seen) run several
    # seconds behind the appliance clock (the detector reads frames a few HLS
    # segments behind live), so "quiet for the gap" and the cap are measured
    # on the clock the finaliser reads -- never by comparing it with a frame
    # time, which would end every activity between two scans.
    opened_at: datetime = field(default_factory=datetime.now)
    last_seen_at: datetime = field(default_factory=datetime.now)
    classes: set = field(default_factory=set)
    event_ids: list = field(default_factory=list)
    closed: bool = False


class ActivityTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[int, Activity] = {}

    def reset(self) -> None:
        with self._lock:
            self._open.clear()

    def continuing(self, camera: int, moment: datetime, *, gap_seconds: float, max_seconds: float) -> Optional[Activity]:
        """The open activity this detection continues, or None (start a new one)."""
        with self._lock:
            activity = self._open.get(camera)
            if activity is None or activity.closed:
                return None
            if (moment - activity.last_seen).total_seconds() > gap_seconds:
                return None
            if (moment - activity.start).total_seconds() > max_seconds:
                return None
            return activity

    def open(self, camera: int, owner_id: str, *, start: datetime, moment: datetime,
             observed_at: Optional[datetime] = None) -> Activity:
        observed_at = observed_at or datetime.now()
        activity = Activity(camera=camera, owner_id=owner_id, start=start, first_moment=moment, last_seen=moment,
                            opened_at=observed_at, last_seen_at=observed_at)
        with self._lock:
            previous = self._open.get(camera)
            if previous is not None:
                previous.closed = True  # superseded: its finaliser closes it now
            self._open[camera] = activity
        return activity

    def seen(self, camera: int, moment: datetime, *, gap_seconds: float, max_seconds: float,
             observed_at: Optional[datetime] = None) -> bool:
        """A scan still sees qualifying objects: extend the open activity if
        this sighting continues it. True if it did."""
        with self._lock:
            activity = self._open.get(camera)
            if activity is None or activity.closed:
                return False
            if (moment - activity.last_seen).total_seconds() > gap_seconds:
                return False
            if (moment - activity.start).total_seconds() > max_seconds:
                return False
            if moment > activity.last_seen:
                activity.last_seen = moment
            activity.last_seen_at = max(activity.last_seen_at, observed_at or datetime.now())
            return True

    def add(self, activity: Activity, *, classes=(), event_ids=()) -> None:
        with self._lock:
            activity.classes.update(classes)
            activity.event_ids.extend(event_ids)

    def is_open(self, camera: int) -> bool:
        with self._lock:
            activity = self._open.get(camera)
            return activity is not None and not activity.closed

    def get(self, camera: int, owner_id: str) -> Optional[Activity]:
        with self._lock:
            activity = self._open.get(camera)
            return activity if activity is not None and activity.owner_id == owner_id else None

    def close_due(self, activity: Activity, now: datetime, *, gap_seconds: float, max_seconds: float) -> Optional[datetime]:
        """None if the activity should be closed now, else when to check again."""
        with self._lock:
            if activity.closed:
                return None
            quiet_until = activity.last_seen_at + timedelta(seconds=gap_seconds)
            cap = activity.opened_at + timedelta(seconds=max_seconds)
            due = min(quiet_until, cap)
            return None if now >= due else due

    def close(self, activity: Activity) -> Activity:
        with self._lock:
            activity.closed = True
            if self._open.get(activity.camera) is activity:
                del self._open[activity.camera]
            return activity

    def in_flight_windows(self, camera: int, *, lead_seconds: float) -> list[tuple[datetime, datetime]]:
        """Footage an open activity will still need (its recording is cut when
        it closes): from before its start, open-ended."""
        with self._lock:
            activity = self._open.get(camera)
            if activity is None:
                return []
            return [(activity.start - timedelta(seconds=lead_seconds), datetime.max)]
