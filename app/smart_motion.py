"""Smart Motion: correlates raw pixel-diff motion events (motion_detector()
in main.py) with real object classifications from the existing YOLO
pipeline (ai_person_detector()/save_yolo_events() in main.py), so a
"smart_motion" analytics event is only produced when motion coincides
with an actual person/vehicle/animal detection -- not just lighting
changes, shadows, or wind blowing something in frame.

Deliberately dependency-light, matching camera_mapping.py's own
documented reasoning: no import of `main`, so this module (and its
tests) are free of main.py's own test-discovery-order fragility (see
camera_mapping.py's module docstring for the same pre-existing issue
in this codebase). Callers in main.py own all file I/O
(append_analytics_event) and the AnalyticsEventModel shape; this
module only tracks recent detections in memory and decides
whether/what to correlate -- no disk access, no network, no asyncio,
so it can never add latency or a failure mode to the recording/live
view/motion pipelines that call into it.
"""

import os
import time

SMART_MOTION_ENABLED = os.environ.get("ANYAICAM_SMART_MOTION_ENABLED", "true").strip().lower() == "true"

CORRELATION_WINDOW_SECONDS = max(
    5.0, float(os.environ.get("ANYAICAM_SMART_MOTION_CORRELATION_WINDOW_SECONDS", "45"))
)

_DEFAULT_CLASSES = "person,car,truck,bus,motorcycle,bicycle,dog,cat,bird"
SMART_MOTION_CLASSES = frozenset(
    value.strip()
    for value in (os.environ.get("ANYAICAM_SMART_MOTION_CLASSES") or _DEFAULT_CLASSES).split(",")
    if value.strip()
)

# Preference order when more than one class was seen in the same window:
# a person is the most actionable signal, then vehicles, then animals.
# Matches the same class vocabulary analytics_sync.py's VEHICLE_EVENT_TYPES
# already establishes as this codebase's canonical vehicle subclass list.
_CLASS_PRIORITY = ("person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat", "bird")

# Bounds memory per camera regardless of detection rate; far larger than
# anything CORRELATION_WINDOW_SECONDS could realistically need at the
# existing AI_PERSON_COOLDOWN_SECONDS (30s default) cadence.
_MAX_TRACKED_PER_CAMERA = 50

_recent_detections: dict[int, list[tuple[float, str]]] = {}


def reset_state() -> None:
    """Test-only: clears all tracked detections. Mirrors the reset
    pattern test_recording_uploader_motion_gate.py's _isolated_state
    fixture already uses for its own module-level state."""
    _recent_detections.clear()


def record_object_detection(camera_number: int, class_name: str, *, at: float | None = None) -> None:
    """Called by save_yolo_events() for every class it just saved to
    analytics_events.json, so a later motion event on this camera can
    be correlated against a real classification without re-reading
    that file on every motion frame."""
    if class_name not in SMART_MOTION_CLASSES:
        return
    timestamp = time.monotonic() if at is None else at
    bucket = _recent_detections.setdefault(camera_number, [])
    bucket.append((timestamp, class_name))
    if len(bucket) > _MAX_TRACKED_PER_CAMERA:
        del bucket[: len(bucket) - _MAX_TRACKED_PER_CAMERA]


def classify_motion(camera_number: int, *, at: float | None = None) -> str | None:
    """Returns the highest-priority class detected on this camera
    within CORRELATION_WINDOW_SECONDS of `at` (default: now), or None
    if nothing correlates -- meaning this motion event should stay
    ordinary/unclassified motion, exactly like it does today. Disabled
    entirely (always None) when SMART_MOTION_ENABLED is false, so the
    feature can be turned off without touching the calling code."""
    if not SMART_MOTION_ENABLED:
        return None
    now = time.monotonic() if at is None else at
    bucket = _recent_detections.get(camera_number) or []
    candidates = {
        class_name
        for timestamp, class_name in bucket
        if 0 <= now - timestamp <= CORRELATION_WINDOW_SECONDS
    }
    if not candidates:
        return None
    for class_name in _CLASS_PRIORITY:
        if class_name in candidates:
            return class_name
    # A configured class outside the known priority list still counts,
    # just without a specific preference among ties.
    return next(iter(candidates))
