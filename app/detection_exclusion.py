"""Customer exclusion ("ignore detections in this area") zones, enforced on
the edge (2026-09-26).

A customer draws an exclusion zone in the same rule editor, and stores it
in the same customer_analytics_rules table, as intrusion zones and lines
(rule_type "exclusion"; customer_analytics_rules.py). It reaches the
appliance through the same configuration poll (edge_camera_sync.py) and
is removed there the moment the customer disables or deletes it.

Enforcement happens once, at the source, and only for a camera that has at
least one enabled exclusion zone:

- detect_objects_frame() (main.py) -- the single YOLO call behind person /
  vehicle / Smart Motion events and their clips, PPE, Facial Recognition,
  LPR, People Counting and Intrusion / Line Crossing -- drops every
  detection whose centre lies inside an exclusion zone, so none of those
  analytics ever see it. The centre is the same reference point intrusion
  zones use (analytics_rules_engine.normalize_centroid()), so a person is
  "in" an exclusion zone exactly when they would be "in" an intrusion zone
  drawn over the same area.
- Pixel motion (motion_detector()'s 160x90 comparison) ignores changes
  inside exclusion zones, so a swaying tree or a busy road the customer
  excluded no longer creates Motion events either.

Cameras without an exclusion zone are untouched: no filtering, no mask.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

import analytics_rules_engine
import recording_uploader

logger = logging.getLogger("anyaicam.detection_exclusion")

RULE_TYPE = "exclusion"
CACHE_SECONDS = 10.0  # a newly synced/removed zone takes effect within this
MOTION_GRID = (160, 90)  # motion_detector()'s own comparison frame
SUPPRESSION_LOG_SECONDS = 60.0  # at most one "suppressed" log line per camera per minute

_cache_lock = threading.Lock()
_zone_cache: dict[int, tuple[float, tuple]] = {}
_mask_cache: dict[int, tuple[tuple, object]] = {}
# Per-camera suppression evidence (2026-09-27). A suppressed detection
# produces no event at all -- by design -- which left nothing to check
# during a physical zone walk test short of re-running YOLO on footage.
# These counters (surfaced by /api/ai/status) show that people WERE seen
# inside the zone and dropped: how many, when, which classes, and where
# (normalized centres, never image data).
_stats_lock = threading.Lock()
_stats: dict[int, dict] = {}
_last_log: dict[int, float] = {}


def reset_cache() -> None:
    with _cache_lock:
        _zone_cache.clear()
        _mask_cache.clear()


def reset_stats() -> None:
    with _stats_lock:
        _stats.clear()
        _last_log.clear()


def _record_suppression(camera_number: int, dropped: list[tuple[dict, tuple[float, float]]]) -> None:
    now = time.monotonic()
    with _stats_lock:
        entry = _stats.setdefault(camera_number, {"suppressed_total": 0, "suppressed_by_class": {}})
        entry["suppressed_total"] += len(dropped)
        for detection, _ in dropped:
            name = str(detection.get("class_name") or "unknown")
            entry["suppressed_by_class"][name] = entry["suppressed_by_class"].get(name, 0) + 1
        entry["last_suppressed_at"] = datetime.now().isoformat()
        entry["last_suppressed"] = [
            {"class_name": d.get("class_name"), "confidence": d.get("confidence"),
             "centre": [round(c[0], 3), round(c[1], 3)]}
            for d, c in dropped[:5]
        ]
        should_log = now - _last_log.get(camera_number, -SUPPRESSION_LOG_SECONDS) >= SUPPRESSION_LOG_SECONDS
        if should_log:
            _last_log[camera_number] = now
        total = entry["suppressed_total"]
    if should_log:
        logger.info(
            "detection_exclusion.suppressed camera=%s count=%s total=%s classes=%s",
            camera_number, len(dropped), total,
            ",".join(sorted({str(d.get("class_name")) for d, _ in dropped})),
        )


def status(camera_number: int) -> dict:
    """What /api/ai/status reports for one camera's exclusion zones."""
    zones = zones_for_camera(camera_number)
    with _stats_lock:
        entry = dict(_stats.get(camera_number) or {})
    entry.setdefault("suppressed_total", 0)
    entry["suppressed_by_class"] = dict(entry.get("suppressed_by_class") or {})
    entry.setdefault("last_suppressed_at", None)
    entry.setdefault("last_suppressed", [])
    entry["zones_active"] = len(zones)
    entry["zones"] = [[[round(x, 4), round(y, 4)] for x, y in zone] for zone in zones]
    return entry


def _load_zones(camera_id: str) -> tuple:
    from partner_db import connection
    with connection() as db:
        rows = db.execute(
            "SELECT geometry_json FROM customer_analytics_rules WHERE camera_id=? AND rule_type=? AND enabled=1",
            (camera_id, RULE_TYPE),
        ).fetchall()
    zones = []
    for row in rows:
        try:
            points = json.loads(row["geometry_json"])
            polygon = tuple((float(p["x"]), float(p["y"])) for p in points)
        except (TypeError, ValueError, KeyError):
            continue  # a malformed zone excludes nothing rather than everything
        if len(polygon) >= 3:
            zones.append(polygon)
    return tuple(zones)


def zones_for_camera(camera_number: int) -> tuple:
    """This camera's enabled exclusion polygons (normalized 0..1 points)."""
    now = time.monotonic()
    with _cache_lock:
        cached = _zone_cache.get(camera_number)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    identity = recording_uploader._camera_identity(camera_number)
    camera_id = (identity or {}).get("camera_id")
    try:
        zones = _load_zones(camera_id) if camera_id else ()
    except Exception:
        zones = ()  # never let a rule-store problem stop detection itself
    with _cache_lock:
        _zone_cache[camera_number] = (now, zones)
    return zones


def is_excluded(point: tuple[float, float], zones) -> bool:
    return any(analytics_rules_engine._point_in_polygon(point, list(zone)) for zone in zones)


def filter_detections(camera_number: int, detections: list[dict], frame) -> list[dict]:
    """The detections outside every exclusion zone (all of them when the
    camera has none, or the frame size is unknown)."""
    if not detections:
        return detections
    zones = zones_for_camera(camera_number)
    if not zones or frame is None or getattr(frame, "shape", None) is None:
        return detections
    height, width = frame.shape[0], frame.shape[1]
    kept, dropped = [], []
    for detection in detections:
        centre = analytics_rules_engine.normalize_centroid(detection, width, height)
        if is_excluded(centre, zones):
            dropped.append((detection, centre))
        else:
            kept.append(detection)
    if dropped:
        _record_suppression(camera_number, dropped)
    return kept


def motion_exclusion_mask(camera_number: int):
    """Boolean array over motion_detector()'s 160x90 grid (row-major, same
    layout as main._motion_zone_mask()), True where changes are ignored; or
    None when the camera has no exclusion zone."""
    zones = zones_for_camera(camera_number)
    if not zones:
        return None
    with _cache_lock:
        cached = _mask_cache.get(camera_number)
        if cached and cached[0] == zones:
            return cached[1]
    import numpy as np
    columns, rows = MOTION_GRID
    # Pixel centres, so a zone edge splits the grid the way it splits the image.
    xs = (np.arange(columns * rows) % columns + 0.5) / columns
    ys = (np.arange(columns * rows) // columns + 0.5) / rows
    mask = np.zeros(columns * rows, dtype=bool)
    for zone in zones:
        inside = np.zeros_like(mask)
        count = len(zone)
        for index in range(count):  # vectorized even-odd rule
            x1, y1 = zone[index]
            x2, y2 = zone[(index + 1) % count]
            crosses = (y1 > ys) != (y2 > ys)
            with np.errstate(divide="ignore", invalid="ignore"):
                x_at = (x2 - x1) * (ys - y1) / (y2 - y1) + x1
            inside ^= crosses & (xs < x_at)
        mask |= inside
    with _cache_lock:
        _mask_cache[camera_number] = (zones, mask)
    return mask
