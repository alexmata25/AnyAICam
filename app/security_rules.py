"""Security-grade line crossing -> INTRUSION ALARM (2026-09-28).

The general-purpose line_crossing rule (analytics_rules_engine) fires
when any tracked object's box CENTRE changes side of a line. That is fine
for counting and informational alerts, but far too eager for an alarm: a
person standing on the line, leaning over it, a dog, a car or a shadow
must never become an intrusion alarm. This evaluator is for the
"security_line" rule type and is deliberately conservative:

- people only, above a confidence and a minimum on-screen size;
- the reference is where the person stands -- the bottom of the box --
  not the box centre, and by default the WHOLE footprint (both bottom
  corners) must be past the line by a margin ("full"); "majority" uses
  the bottom-centre point with the margin instead;
- the rule has a PROTECTED side (rule.direction: "inbound" = the side the
  line's A->B orientation calls "b", "outbound" = "a"). A crossing needs
  the track to have been clearly OUTSIDE first, then confirmed on the
  protected side for CONFIRM_READINGS consecutive readings -- someone
  walking out, or first seen already inside, never alarms;
- one alarm per track per entry (it must leave the protected side before
  it can alarm again), and a per-rule alarm cooldown so a person pacing
  back and forth does not flood the homeowner.

Pure logic: no I/O. The caller (customer_analytics_rule_worker) gates it
on the camera actually being armed (security_modes.camera_is_armed).
"""
from __future__ import annotations

import os
import threading

RULE_TYPE = "security_line"
CONFIRM_READINGS = max(1, int(os.environ.get("ANYAICAM_SECURITY_CONFIRM_READINGS", "2")))
SIDE_MARGIN = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_SIDE_MARGIN", "0.02")))  # normalized distance past the line
MIN_CONFIDENCE = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_MIN_CONFIDENCE", "0.5")))
MIN_BOX_HEIGHT_FRACTION = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_MIN_BOX_HEIGHT", "0.06")))
DEFAULT_COOLDOWN_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_ALARM_COOLDOWN_SECONDS", "60")))
TRACK_TTL_SECONDS = 30.0

_lock = threading.Lock()
_state: dict = {}          # (camera, rule_id, track_id) -> {"outside": bool, "inside_run": int, "alarmed": bool, "seen": float}
_last_alarm_at: dict = {}  # (camera, rule_id) -> time


def reset_state() -> None:
    with _lock:
        _state.clear()
        _last_alarm_at.clear()


def _signed_distance(point, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return 0.0
    return ((px - ax) * dy - (py - ay) * dx) / length


def _protected_sign(rule: dict) -> int:
    """+1 when the protected side is where _signed_distance is negative
    ("b" in the general engine's vocabulary, rule.direction "inbound")."""
    return 1 if (rule.get("direction") or "inbound") == "inbound" else -1


def _footprint(detection: dict, width: int, height: int):
    x, y, w, h = (float(detection[k]) for k in ("x", "y", "width", "height"))
    bottom = (y + h) / height
    return ((x / width, bottom), ((x + w / 2) / width, bottom), ((x + w) / width, bottom))


def rule_is_valid(rule: dict) -> bool:
    geometry = rule.get("geometry") or []
    return (
        rule.get("rule_type") == RULE_TYPE and len(geometry) == 2
        and all(isinstance(p, dict) and "x" in p and "y" in p for p in geometry)
        and (rule.get("direction") or "inbound") in ("inbound", "outbound")
    )


def evaluate(camera_number, rule: dict, tracked_detections: list, frame_width: int, frame_height: int, now: float) -> list:
    """Alarms confirmed THIS reading, as dicts (rule_id, zone_name, track_id,
    confidence, box, direction, analytic_type="intrusion_alarm")."""
    if not rule_is_valid(rule) or frame_width <= 0 or frame_height <= 0:
        return []
    rule_id = rule["id"]
    a = (float(rule["geometry"][0]["x"]), float(rule["geometry"][0]["y"]))
    b = (float(rule["geometry"][1]["x"]), float(rule["geometry"][1]["y"]))
    sign = _protected_sign(rule)
    mode = rule.get("crossing_mode") or "full"
    cooldown = float(rule.get("alarm_cooldown_seconds") or DEFAULT_COOLDOWN_SECONDS)
    alarms = []
    with _lock:
        for key in [k for k, v in _state.items() if k[0] == camera_number and k[1] == rule_id and now - v["seen"] > TRACK_TTL_SECONDS]:
            del _state[key]
        for detection in tracked_detections:
            track_id = detection.get("track_id")
            if not track_id or detection.get("class_name") != "person":
                continue
            if float(detection.get("confidence") or 0) < max(MIN_CONFIDENCE, float(rule.get("confidence_threshold") or 0)):
                continue
            if float(detection.get("height") or 0) / frame_height < MIN_BOX_HEIGHT_FRACTION:
                continue
            left, centre, right = _footprint(detection, frame_width, frame_height)
            # positive = on the protected side
            distances = [sign * -_signed_distance(p, a, b) for p in (left, centre, right)]
            if mode == "majority":
                inside = distances[1] > SIDE_MARGIN
                outside = distances[1] < -SIDE_MARGIN
            else:
                inside = min(distances[0], distances[2]) > 0 and distances[1] > SIDE_MARGIN
                outside = max(distances[0], distances[2]) < 0 and distances[1] < -SIDE_MARGIN
            key = (camera_number, rule_id, track_id)
            track = _state.setdefault(key, {"outside": False, "inside_run": 0, "alarmed": False, "seen": now})
            track["seen"] = now
            if outside:
                track["outside"] = True
                track["inside_run"] = 0
                track["alarmed"] = False  # left the protected side: a later entry may alarm again
                continue
            if not inside:
                continue  # straddling / within the margin: no evidence either way this reading
            track["inside_run"] += 1
            if track["alarmed"] or not track["outside"] or track["inside_run"] < CONFIRM_READINGS:
                continue
            track["alarmed"] = True
            if now - _last_alarm_at.get((camera_number, rule_id), float("-inf")) < cooldown:
                continue  # confirmed, but this rule alarmed moments ago
            _last_alarm_at[(camera_number, rule_id)] = now
            alarms.append({
                "rule_id": rule_id,
                "analytic_type": "intrusion_alarm",
                "zone_name": rule.get("name"),
                "event_type": "person",
                "track_id": track_id,
                "confidence": detection.get("confidence"),
                "direction": rule.get("direction") or "inbound",
                "box": {k: detection[k] for k in ("x", "y", "width", "height")},
            })
    return alarms
