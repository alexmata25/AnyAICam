"""Suppress repeat AI events for objects that have not moved (2026-09-24).

Confirmed live on the Ryzen: a parked car on each driveway camera and people
sitting in the living room produced a new AI event -- and a new ~4.4 MB clip
uploaded to S3 -- every time the detector's 30s cooldown expired, with the
exact same bounding boxes each time (e.g. camera 3's car at x=623 y=164
513x294, 54 events an hour). About 190 clips/hour, ~3.8 Mbps of continuous
uplink, and a timeline full of the same parked car.

A detection frame is a repeat when every object in it matches (same class,
IoU >= threshold) an object the camera has already reported. Anything new --
another object, a different class, or a real move -- is never a repeat, so
arrivals and movement still create events immediately. A still object is
re-reported after `rearm_seconds` so it is never silent forever.

StationaryMemory (second live pass, same day): comparing against only the
LAST reported frame still let a stationary scene re-fire every ~60s,
because low-confidence detections flicker in and out between frames (a
0.39 "car" or 0.38 "truck" at the same spot, present in one frame and not
the next) -- each reappearance looked "new". The memory keeps every box
reported in the last rearm window, per camera.
"""
import os

STATIONARY_IOU_THRESHOLD = min(1.0, max(0.1, float(os.environ.get("ANYAICAM_AI_STATIONARY_IOU_THRESHOLD", "0.7"))))
# 0 disables suppression entirely (every cooldown expiry reports again).
STATIONARY_REARM_SECONDS = max(0, int(os.environ.get("ANYAICAM_AI_STATIONARY_REARM_SECONDS", "1800")))


def signature(detections) -> list[tuple[str, float, float, float, float]]:
    """(class, x, y, width, height) for each well-formed detection."""
    boxes = []
    for item in detections or []:
        if not isinstance(item, dict):
            continue
        try:
            box = (float(item["x"]), float(item["y"]), float(item["width"]), float(item["height"]))
        except (KeyError, TypeError, ValueError):
            continue
        if box[2] > 0 and box[3] > 0:
            boxes.append((str(item.get("class_name") or item.get("class_id") or ""), *box))
    return boxes


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_w = min(ax + aw, bx + bw) - max(ax, bx)
    overlap_h = min(ay + ah, by + bh) - max(ay, by)
    if overlap_w <= 0 or overlap_h <= 0:
        return 0.0
    inter = overlap_w * overlap_h
    return inter / (aw * ah + bw * bh - inter)


def is_stationary_repeat(previous, current, seconds_since_previous: float, *,
                         iou_threshold: float = STATIONARY_IOU_THRESHOLD,
                         rearm_seconds: int = STATIONARY_REARM_SECONDS) -> bool:
    """True when `current` (a signature()) shows nothing new relative to
    the last reported `previous` signature, reported under rearm_seconds ago."""
    if rearm_seconds <= 0 or not previous or not current or seconds_since_previous >= rearm_seconds:
        return False
    for label, *box in current:
        if not any(label == prev_label and iou(box, prev_box) >= iou_threshold
                   for prev_label, *prev_box in previous):
            return False
    return True


class StationaryMemory:
    """Per-camera memory of reported boxes: (label, box, first_reported_at).
    An entry expires rearm_seconds after it was FIRST reported, so an object
    that never moves is re-reported once per rearm window, not never."""

    MAX_ENTRIES_PER_CAMERA = 64

    def __init__(self, *, iou_threshold: float = STATIONARY_IOU_THRESHOLD, rearm_seconds: int = STATIONARY_REARM_SECONDS):
        self.iou_threshold = iou_threshold
        self.rearm_seconds = rearm_seconds
        self._entries: dict = {}

    def _live(self, camera, now: float) -> list:
        entries = [e for e in self._entries.get(camera, []) if now - e[2] < self.rearm_seconds]
        self._entries[camera] = entries
        return entries

    def _known(self, entries, label, box) -> bool:
        return any(label == known_label and iou(box, known_box) >= self.iou_threshold for known_label, known_box, _ in entries)

    def is_repeat(self, camera, current, now: float) -> bool:
        if self.rearm_seconds <= 0 or not current:
            return False
        entries = self._live(camera, now)
        return bool(entries) and all(self._known(entries, label, tuple(box)) for label, *box in current)

    def remember(self, camera, reported, now: float) -> None:
        if self.rearm_seconds <= 0:
            return
        entries = self._live(camera, now)
        for label, *box in reported:
            if not self._known(entries, label, tuple(box)):
                entries.append((label, tuple(box), now))
        self._entries[camera] = entries[-self.MAX_ENTRIES_PER_CAMERA:]
