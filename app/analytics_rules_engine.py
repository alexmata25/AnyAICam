"""Custom per-camera object tracker + analytics-rule evaluation engine.

Pure logic only -- no cv2, no ultralytics, no asyncio, no file I/O, no
network. Everything here operates on plain dicts/lists so it can be
unit-tested directly on this WSL host without Docker, unlike main.py
(which hardcodes /app/... paths at import time and requires the
project's Docker image). main.py is the only caller:
detect_objects_frame() feeds each cycle's raw pixel-coordinate
detections through update_tracker() to attach a stable track_id
before returning them, and ai_person_detector() calls evaluate_rules()
every cycle with the tracked detections and this camera's currently-
configured rules to get back zero or more rule-triggered events,
persisted through save_rule_events() (a sibling of save_yolo_events(),
using the same append_analytics_event() storage path).

Deliberately NOT Ultralytics' own model.track(persist=True): this
project's yolo_model is a single global instance shared across all
four cameras, called every ~5s in an interleaved order -- Ultralytics'
persistent-track state is tied to the model object, not cleanly
separable per camera in this single-frame-at-a-time calling pattern,
risking track-ID collisions/leaks across cameras. The small dedicated
per-camera tracker below avoids that entirely, at negligible extra
CPU cost for the handful of objects a single frame typically has, and
keeps every piece of state explicitly keyed by camera_number, matching
this file's own established per-camera-dict convention
(ai_detection_state[camera_number], ai_person_last_event[camera_number]).

Independent of AI_PERSON_COOLDOWN_SECONDS entirely: this module never
reads that constant, and main.py's wiring calls evaluate_rules() on
every detection cycle regardless of whether that cycle's ordinary
class-grouped events are cooldown-suppressed. Deduplication for
rule-triggered events is this module's own state-machine transitions
(a dwell threshold first crossed, a line side actually flipped) --
not a wall-clock cooldown -- so a real, distinct occurrence is never
silently dropped the way a second occurrence within
AI_PERSON_COOLDOWN_SECONDS's ~30s window would be under the ordinary
detection path. A small MIN_REFIRE_SECONDS floor exists only as a
defensive guard against same-cycle/boundary-jitter double-firing, not
as the primary suppression mechanism.

Zero configured rules for a camera is the common case (the feature not
in use) and is a fast no-op: evaluate_rules() returns [] immediately
without touching per-track state, so a camera with no rules behaves
identically to before this module existed.
"""

import uuid

# ---------------------------------------------------------------- tracker

# A track not matched for this many consecutive update_tracker() calls
# is evicted -- bounds memory from objects that left the frame, without
# discarding a track over one or two missed/occluded frames.
TRACK_MAX_MISSED_CYCLES = 3

# Minimum IoU (intersection-over-union, in pixel-box space) to consider
# a detection this cycle the continuation of an existing track from the
# previous cycle. Matching is scoped to the same class_name only -- a
# "car" box can never continue a "person" track, however much they
# overlap.
TRACK_IOU_MATCH_THRESHOLD = 0.3

_tracker_state: dict[int, dict[str, dict]] = {}  # camera_number -> {track_id: {"class_name", "box", "missed_cycles"}}


def _iou(box_a: dict, box_b: dict) -> float:
    """Standard intersection-over-union of two {x,y,width,height}
    pixel-space boxes (top-left origin). Returns 0.0 for
    non-overlapping or degenerate (zero-area) boxes rather than
    raising -- box dimensions come from YOLO's own output and are
    already validated there (main.py's detect_objects_frame() clamps
    width/height to at least 1), but this function never assumes that
    of its caller."""
    ax1, ay1 = box_a["x"], box_a["y"]
    ax2, ay2 = ax1 + box_a["width"], ay1 + box_a["height"]
    bx1, by1 = box_b["x"], box_b["y"]
    bx2, by2 = bx1 + box_b["width"], by1 + box_b["height"]
    inter_x1, inter_y1 = max(ax1, bx1), max(ay1, by1)
    inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
    inter_w, inter_h = max(0, inter_x2 - inter_x1), max(0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    area_a = box_a["width"] * box_a["height"]
    area_b = box_b["width"] * box_b["height"]
    union = area_a + area_b - inter_area
    return inter_area / union if union > 0 else 0.0


def update_tracker(camera_number: int, detections: list[dict]) -> list[dict]:
    """Matches this cycle's detections against this camera's live
    tracks from the previous cycle (same-class boxes only, greedy
    highest-IoU-first -- a deliberately simple assignment, not a full
    Hungarian solve, appropriate for the small handful of objects a
    single frame typically has), assigns each matched detection its
    existing track_id, assigns a fresh id to any unmatched detection,
    and ages/evicts tracks unmatched this cycle. Returns a NEW list of
    detection dicts (shallow copies of the input dicts plus a
    "track_id" key) -- never mutates the input list or its dicts, so a
    caller holding onto the original `detections` list is unaffected."""
    state = _tracker_state.setdefault(camera_number, {})
    unmatched_track_ids = set(state.keys())
    tracked_detections: list[dict] = []

    for detection in detections:
        best_track_id = None
        best_iou = 0.0
        for track_id in unmatched_track_ids:
            track = state[track_id]
            if track["class_name"] != detection["class_name"]:
                continue
            iou = _iou(track["box"], detection)
            if iou > best_iou and iou >= TRACK_IOU_MATCH_THRESHOLD:
                best_iou = iou
                best_track_id = track_id

        if best_track_id is not None:
            unmatched_track_ids.discard(best_track_id)
            track_id = best_track_id
        else:
            track_id = uuid.uuid4().hex[:12]

        state[track_id] = {
            "class_name": detection["class_name"],
            "box": {"x": detection["x"], "y": detection["y"], "width": detection["width"], "height": detection["height"]},
            "missed_cycles": 0,
        }
        tracked = dict(detection)
        tracked["track_id"] = track_id
        tracked_detections.append(tracked)

    for track_id in list(unmatched_track_ids):
        state[track_id]["missed_cycles"] += 1
        if state[track_id]["missed_cycles"] > TRACK_MAX_MISSED_CYCLES:
            del state[track_id]

    return tracked_detections


def reset_tracker(camera_number: int | None = None) -> None:
    """Test/diagnostic hook only -- production code never calls this.
    Clears tracker state for one camera, or every camera when
    camera_number is None."""
    if camera_number is None:
        _tracker_state.clear()
    else:
        _tracker_state.pop(camera_number, None)


# --------------------------------------------------------- coordinate space

def normalize_centroid(detection: dict, frame_width: int, frame_height: int) -> tuple[float, float]:
    """Converts a pixel-space detection box (as produced by
    detect_objects_frame(), top-left origin) into the same normalized
    0..1 coordinate space AnalyticsRuleModel.geometry is drawn/stored
    in (the canvas rule-builder's points are already fractions of the
    preview image's own width/height), so a rule's geometry and a
    detection's centroid can be compared directly. Fails safe to the
    frame's own center (0.5, 0.5) -- never (0, 0), which would sit
    exactly on a corner and could spuriously appear "inside" a
    corner-anchored zone -- when frame_width/height are non-positive,
    rather than dividing by zero or raising; a zero-size frame is
    already a stalled-camera signal detect_objects_frame() would have
    surfaced elsewhere, not something this function should crash on."""
    if frame_width <= 0 or frame_height <= 0:
        return (0.5, 0.5)
    cx = (detection["x"] + detection["width"] / 2) / frame_width
    cy = (detection["y"] + detection["height"] / 2) / frame_height
    return (cx, cy)


# ------------------------------------------------------------- geometry math

def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test, normalized-coordinate
    space. Requires >= 3 points to be meaningful -- callers only ever
    invoke this after _rule_is_evaluable() has already confirmed that,
    so this function itself does not re-validate polygon length."""
    x, y = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def _signed_distance_to_line(point: tuple[float, float], line_start: tuple[float, float], line_end: tuple[float, float]) -> float:
    """Signed perpendicular distance, in the same normalized 0..1 units
    as the input coordinates, from point to the directed line
    line_start->line_end. Positive on one side, negative on the other;
    magnitude is a real distance (not a scale-dependent raw cross
    product), which is what makes LINE_SIDE_EPSILON below a physically
    meaningful, easily-tested value. Returns 0.0 for a degenerate
    (zero-length) line rather than dividing by zero -- a
    zero-length line_crossing/people_count rule is already rejected by
    _rule_is_evaluable() before evaluation reaches here, but this
    function fails safe regardless of how it's called."""
    (x, y), (x1, y1), (x2, y2) = point, line_start, line_end
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return 0.0
    return ((x - x1) * dy - (y - y1) * dx) / length


# ----------------------------------------------------- rule validation

def _is_point(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("x"), (int, float)) and isinstance(value.get("y"), (int, float))


def _rule_is_evaluable(rule: dict) -> bool:
    """Fails safe (returns False, never raises) for any rule that's
    disabled, missing required keys, or has geometry that can't
    possibly be evaluated for its own analytic_type. A malformed or
    disabled rule must never crash detection and must never be
    silently treated as if it fired."""
    if not isinstance(rule, dict) or not rule.get("id"):
        return False
    if not rule.get("enabled", True):
        return False
    geometry = rule.get("geometry")
    if not isinstance(geometry, list):
        return False
    analytic_type = rule.get("analytic_type")
    if analytic_type == "intrusion":
        return len(geometry) >= 3 and all(_is_point(p) for p in geometry)
    if analytic_type in ("line_crossing", "people_count"):
        return len(geometry) == 2 and all(_is_point(p) for p in geometry)
    return False  # unknown analytic_type: fail safe, not an error -- a rule saved by a future UI this engine doesn't know yet must never crash detection


# --------------------------------------------------------- dwell / crossing state

# Keyed by (camera_number, rule_id, track_id). Kept module-level, per-
# camera/per-rule/per-track, matching this project's own established
# per-camera in-memory state convention -- see module docstring.
_dwell_entered_at: dict[tuple, float] = {}
_line_last_side: dict[tuple, str] = {}
_last_fired_at: dict[tuple, float] = {}

DEFAULT_DWELL_SECONDS = 5.0  # minimum continuous in-zone time before an intrusion rule fires -- roughly one confirmed cycle beyond entry, at this pipeline's existing ~5s detection cadence
LINE_SIDE_EPSILON = 0.01  # normalized-distance "dead zone" around a line: a centroid this close to the line is treated as ambiguous this cycle, not as a side, to avoid boundary-jitter false crossings
MIN_REFIRE_SECONDS = 0.5  # defensive floor only -- see module docstring; the real dedup is the state-machine transition itself


def _clear_track_state(camera_number: int, rule_id: str, track_id: str) -> None:
    key = (camera_number, rule_id, track_id)
    _dwell_entered_at.pop(key, None)
    _line_last_side.pop(key, None)
    _last_fired_at.pop(key, None)


def _evict_unseen_tracks(camera_number: int, rule_id: str, seen_track_ids: set) -> None:
    """Removes state for any track this rule saw before but did not see
    at all this cycle -- prevents unbounded growth from objects that
    left the frame entirely, and ensures a later different object
    (even one that happens to reuse an evicted track_id) starts with
    fresh state rather than inheriting another object's history."""
    for state_dict in (_dwell_entered_at, _line_last_side, _last_fired_at):
        stale_keys = [key for key in state_dict if key[0] == camera_number and key[1] == rule_id and key[2] not in seen_track_ids]
        for key in stale_keys:
            del state_dict[key]


def _box_from_detection(detection: dict) -> dict:
    return {"x": detection["x"], "y": detection["y"], "width": detection["width"], "height": detection["height"]}


def _evaluate_intrusion(camera_number: int, rule: dict, tracked_detections: list[dict], frame_width: int, frame_height: int, now: float) -> list[dict]:
    rule_id = rule["id"]
    polygon = [(p["x"], p["y"]) for p in rule["geometry"]]
    confidence_threshold = float(rule.get("confidence_threshold") or 0)
    fired: list[dict] = []
    seen_track_ids: set = set()

    for detection in tracked_detections:
        track_id = detection.get("track_id")
        if not track_id:
            continue
        seen_track_ids.add(track_id)
        key = (camera_number, rule_id, track_id)

        if detection.get("confidence", 0) < confidence_threshold:
            _clear_track_state(camera_number, rule_id, track_id)
            continue

        centroid = normalize_centroid(detection, frame_width, frame_height)
        if not _point_in_polygon(centroid, polygon):
            _clear_track_state(camera_number, rule_id, track_id)
            continue

        entry_time = _dwell_entered_at.get(key)
        if entry_time is None:
            _dwell_entered_at[key] = now  # just entered the zone -- dwell clock starts now, no event yet
            continue

        if now - entry_time < DEFAULT_DWELL_SECONDS:
            continue  # still dwelling, not long enough yet

        if key in _last_fired_at:
            continue  # already fired once for this continuous dwell -- see module docstring on dedup

        _last_fired_at[key] = now
        fired.append({
            "rule_id": rule_id,
            "analytic_type": "intrusion",
            "zone_name": rule.get("name"),
            "event_type": detection["class_name"],
            "confidence": detection["confidence"],
            "direction": None,
            "track_id": track_id,
            "box": _box_from_detection(detection),
        })

    _evict_unseen_tracks(camera_number, rule_id, seen_track_ids)
    return fired


def _evaluate_line_crossing(camera_number: int, rule: dict, tracked_detections: list[dict], frame_width: int, frame_height: int, now: float, counting: bool) -> list[dict]:
    rule_id = rule["id"]
    line_start = (rule["geometry"][0]["x"], rule["geometry"][0]["y"])
    line_end = (rule["geometry"][1]["x"], rule["geometry"][1]["y"])
    confidence_threshold = float(rule.get("confidence_threshold") or 0)
    # AnalyticsRuleModel.direction values are "both"/"inbound"/"outbound"
    # (see the rule-builder UI) -- "inbound"/"outbound" are also this
    # engine's own vocabulary for a fired crossing's direction, so no
    # translation layer/mismatch exists between the two.
    rule_direction = rule.get("direction") or "both"
    analytic_type = "people_count" if counting else "line_crossing"
    fired: list[dict] = []
    seen_track_ids: set = set()

    for detection in tracked_detections:
        track_id = detection.get("track_id")
        if not track_id:
            continue
        if counting and detection.get("class_name") != "person":
            continue
        seen_track_ids.add(track_id)
        key = (camera_number, rule_id, track_id)

        if detection.get("confidence", 0) < confidence_threshold:
            continue

        centroid = normalize_centroid(detection, frame_width, frame_height)
        distance = _signed_distance_to_line(centroid, line_start, line_end)
        if abs(distance) < LINE_SIDE_EPSILON:
            continue  # ambiguous/on-the-line this cycle -- don't update last-known-side, wait for a clear reading next cycle

        side = "a" if distance > 0 else "b"
        previous_side = _line_last_side.get(key)
        _line_last_side[key] = side
        if previous_side is None or previous_side == side:
            continue  # first clear reading for this track, or no change -- not a crossing

        if now - _last_fired_at.get(key, 0.0) < MIN_REFIRE_SECONDS:
            continue  # defensive floor against boundary-jitter double-fire -- see module docstring
        _last_fired_at[key] = now

        # Which physical direction "a"->"b" represents is defined by the
        # rule author's own two-point line orientation, not a fixed
        # compass direction -- documented behavior, not an inferred one.
        direction = "inbound" if side == "b" else "outbound"
        if rule_direction != "both" and rule_direction != direction:
            continue  # a genuine crossing, correctly tracked and deduped above, just not the direction this rule alerts on

        fired.append({
            "rule_id": rule_id,
            "analytic_type": analytic_type,
            "zone_name": rule.get("name"),
            "event_type": detection["class_name"],
            "confidence": detection["confidence"],
            "direction": direction,
            "track_id": track_id,
            "box": _box_from_detection(detection),
        })

    _evict_unseen_tracks(camera_number, rule_id, seen_track_ids)
    return fired


def evaluate_rules(camera_number: int, tracked_detections: list[dict], rules: list[dict], frame_width: int, frame_height: int, now: float | None = None) -> list[dict]:
    """Pure function: given this cycle's tracked detections (each with
    class_name/confidence/track_id/x/y/width/height, i.e. the output of
    update_tracker()) and this camera's currently-configured analytics
    rules (AnalyticsRuleModel-shaped dicts), returns zero or more
    rule-triggered event dicts, each shaped {rule_id, analytic_type,
    zone_name, event_type, confidence, direction, track_id, box}.
    Callers (save_rule_events()) stamp identity/timestamp/thumbnail
    fields -- this function has no wall-clock concerns beyond its own
    internal dedup timing.

    `now` is injectable purely for deterministic tests; production
    callers omit it (defaults to time.monotonic() semantics via the
    caller's own now value -- main.py passes time.monotonic()).

    An empty `rules` list -- the common case, feature not configured
    for this camera -- returns [] immediately without touching any
    per-track state: this is the concrete meaning of "zero configured
    rules preserves existing YOLO behavior". Malformed or disabled
    rules (see _rule_is_evaluable()) are skipped entirely, never
    raise, and never produce an event."""
    if not rules:
        return []
    if now is None:
        import time
        now = time.monotonic()

    events: list[dict] = []
    for rule in rules:
        if not _rule_is_evaluable(rule):
            continue
        analytic_type = rule.get("analytic_type")
        if analytic_type == "intrusion":
            events.extend(_evaluate_intrusion(camera_number, rule, tracked_detections, frame_width, frame_height, now))
        elif analytic_type == "line_crossing":
            events.extend(_evaluate_line_crossing(camera_number, rule, tracked_detections, frame_width, frame_height, now, counting=False))
        elif analytic_type == "people_count":
            events.extend(_evaluate_line_crossing(camera_number, rule, tracked_detections, frame_width, frame_height, now, counting=True))
    return events
