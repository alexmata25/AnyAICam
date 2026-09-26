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
import threading
import time

import analytics_rules_engine
import recording_uploader

RULE_TYPE = "exclusion"
CACHE_SECONDS = 10.0  # a newly synced/removed zone takes effect within this
MOTION_GRID = (160, 90)  # motion_detector()'s own comparison frame

_cache_lock = threading.Lock()
_zone_cache: dict[int, tuple[float, tuple]] = {}
_mask_cache: dict[int, tuple[tuple, object]] = {}


def reset_cache() -> None:
    with _cache_lock:
        _zone_cache.clear()
        _mask_cache.clear()


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
    return [d for d in detections
            if not is_excluded(analytics_rules_engine.normalize_centroid(d, width, height), zones)]


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
