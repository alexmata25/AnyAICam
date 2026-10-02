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

Arming lifecycle (2026-10-01): crossings are only watched while armed, so
someone who walked in while the site was Disarmed must not alarm the
moment it is armed again. note_arm_state() resets a camera's crossing
state whenever it is Disarmed or changes mode (Stay <-> Away): after
arming, an alarm needs a NEW crossing -- seen outside, then confirmed on
the protected side. A track the tracker loses and re-acquires (or any
track after a restart) starts with no "outside" history, so it too needs
a fresh outside-to-inside crossing.

One alarm per physical intrusion (2026-10-01, owner decision): when the
same person (camera + track) crosses several overlapping security lines,
the first confirmed line raises the INTRUSION ALARM; any other line the
same track confirms within ALARM_DEDUP_SECONDS raises none, until the
person leaves the protected side of the line that raised it (that
intrusion is then over, and a new one alarms again). The extra
lines are kept on the alarm (matched_rule_ids / matched_rule_names) when
they confirm in the same pass, and logged either way, for audit.
"""
from __future__ import annotations

import logging
import os
import threading

RULE_TYPE = "security_line"
CONFIRM_READINGS = max(1, int(os.environ.get("ANYAICAM_SECURITY_CONFIRM_READINGS", "2")))
SIDE_MARGIN = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_SIDE_MARGIN", "0.02")))  # normalized distance past the line
MIN_CONFIDENCE = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_MIN_CONFIDENCE", "0.5")))
MIN_BOX_HEIGHT_FRACTION = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_MIN_BOX_HEIGHT", "0.06")))
DEFAULT_COOLDOWN_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_ALARM_COOLDOWN_SECONDS", "60")))
TRACK_TTL_SECONDS = 30.0
ALARM_DEDUP_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_SECURITY_ALARM_DEDUP_SECONDS", "120")))

logger = logging.getLogger("anyaicam.security_rules")

_lock = threading.Lock()
_state: dict = {}          # (camera, rule_id, track_id) -> {"outside": bool, "inside_run": int, "alarmed": bool, "seen": float}
_last_alarm_at: dict = {}  # (camera, rule_id) -> time
_arm_mode: dict = {}       # camera -> the armed mode its crossing state belongs to
_track_alarm: dict = {}    # (camera, track_id) -> {"at": time, "alarm": the alarm dict raised for it}


def reset_state() -> None:
    with _lock:
        _state.clear()
        _last_alarm_at.clear()
        _arm_mode.clear()
        _track_alarm.clear()
        _rule_signature.clear()


def reset_camera(camera_number) -> None:
    """Forget every crossing, cooldown and raised alarm for one camera."""
    with _lock:
        _reset_camera_locked(camera_number)


def _reset_camera_locked(camera_number) -> None:
    for store in (_state, _last_alarm_at, _track_alarm):
        for key in [key for key in store if key[0] == camera_number]:
            del store[key]


_rule_signature: dict = {}  # (camera, rule_id) -> the line configuration its state belongs to


def sync_rules(camera_number, rules: list) -> list:
    """Drop crossing state of security lines that were removed or whose
    geometry/protected side changed (2026-10-02). Returns reset rule ids."""
    import json
    current = {}
    for rule in rules or []:
        if isinstance(rule, dict) and rule.get("id"):
            current[rule["id"]] = json.dumps({k: rule.get(k) for k in ("geometry", "direction")}, sort_keys=True, default=str)
    reset = []
    with _lock:
        for (camera, rule_id) in [key for key in _rule_signature if key[0] == camera_number]:
            if current.get(rule_id) != _rule_signature[(camera, rule_id)]:
                for store in (_state, _last_alarm_at):
                    for key in [key for key in store if key[0] == camera_number and key[1] == rule_id]:
                        del store[key]
                for key in [key for key, v in _track_alarm.items() if key[0] == camera_number and v["alarm"]["rule_id"] == rule_id]:
                    del _track_alarm[key]
                del _rule_signature[(camera, rule_id)]
                reset.append(rule_id)
        for rule_id, signature in current.items():
            _rule_signature[(camera_number, rule_id)] = signature
    return reset


def note_arm_state(camera_number, mode: str | None) -> bool:
    """Tell this module whether the camera is armed right now (the armed
    mode, or None while Disarmed / not part of the mode). Any change --
    disarming, arming, Stay <-> Away -- and every Disarmed reading clears
    the camera's crossing state. Returns True when it was reset."""
    with _lock:
        previous = _arm_mode.get(camera_number)
        if mode is None:
            _arm_mode.pop(camera_number, None)
            had_state = any(key[0] == camera_number for store in (_state, _last_alarm_at, _track_alarm) for key in store)
            _reset_camera_locked(camera_number)
            return previous is not None or had_state
        if previous == mode:
            return False
        _reset_camera_locked(camera_number)
        _arm_mode[camera_number] = mode
        return True


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
                raised = _track_alarm.get((camera_number, track_id))
                if raised and raised["alarm"]["rule_id"] == rule_id:
                    del _track_alarm[(camera_number, track_id)]  # left the area it intruded into: that intrusion is over
                continue
            if not inside:
                continue  # straddling / within the margin: no evidence either way this reading
            track["inside_run"] += 1
            if track["alarmed"] or not track["outside"] or track["inside_run"] < CONFIRM_READINGS:
                continue
            track["alarmed"] = True
            if now - _last_alarm_at.get((camera_number, rule_id), float("-inf")) < cooldown:
                continue  # confirmed, but this rule alarmed moments ago
            raised = _track_alarm.get((camera_number, track_id))
            if raised and now - raised["at"] <= ALARM_DEDUP_SECONDS:
                # The same person already raised this intrusion on another
                # (overlapping) line: one urgent alarm, the extra line kept
                # for audit.
                primary = raised["alarm"]
                if rule_id not in primary["matched_rule_ids"]:
                    primary["matched_rule_ids"].append(rule_id)
                    primary["matched_rule_names"].append(rule.get("name"))
                logger.info("security_alarm.duplicate_suppressed camera=%s track=%s rule_id=%s primary_rule_id=%s",
                            camera_number, track_id, rule_id, primary["rule_id"])
                continue
            _last_alarm_at[(camera_number, rule_id)] = now
            alarm = {
                "rule_id": rule_id,
                "analytic_type": "intrusion_alarm",
                "zone_name": rule.get("name"),
                "event_type": "person",
                "track_id": track_id,
                "confidence": detection.get("confidence"),
                "direction": rule.get("direction") or "inbound",
                "box": {k: detection[k] for k in ("x", "y", "width", "height")},
                "matched_rule_ids": [rule_id],
                "matched_rule_names": [rule.get("name")],
            }
            _track_alarm[(camera_number, track_id)] = {"at": now, "alarm": alarm}
            alarms.append(alarm)
        for key in [k for k, v in _track_alarm.items() if k[0] == camera_number and now - v["at"] > ALARM_DEDUP_SECONDS]:
            del _track_alarm[key]
    return alarms
