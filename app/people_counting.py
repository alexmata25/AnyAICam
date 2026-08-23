"""People Counting: a lightweight, deterministic tracker + counting-line
crossing detector built ENTIRELY on top of the existing YOLO person
detections already produced by detect_objects_frame() (main.py) --
no new AI model, no new inference cost beyond a configurable, faster
polling cadence for cameras that are actually entitled+configured for
this feature (see PEOPLE_COUNTING_INTERVAL_SECONDS in main.py's
integration layer).

Pure Python, zero dependency on main.py/cv2/YOLO/the app container or
any database -- every difficult counting case required by this
engagement (crossing both directions, people crossing close together,
approach-then-turn-around, standing on the line, disappear-and-return,
occlusion, duplicate same-frame detections, and counter-restart/reset
behavior) is proven deterministically in tests/test_people_counting.py
against this module directly, with no live camera or container needed.

Scope, deliberately narrow per explicit instruction: this module
implements ONLY a straight counting LINE and IN/OUT crossing counts.
It does not implement polygon/zone evaluation, intrusion detection, or
any other rule type the existing (currently-unenforced) rule-builder
UI can draw -- see the module docstring note in main.py's integration
point for exactly how a stored line_crossing AnalyticsRuleModel's
`geometry` (its first two points) is reused as this counting line,
rather than inventing a second, parallel configuration concept.

Coordinate system: matches the existing rule-builder's stored geometry
exactly -- normalized 0.0-1.0 coordinates within the camera frame
(the same {"x":..., "y":...} points a line_crossing rule already
stores), so no unit conversion is needed between what an admin draws
and what this module consumes.
"""

import itertools
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CountingLine:
    """A straight counting line, reusing exactly the first two points of
    an existing line_crossing AnalyticsRuleModel's `geometry` field --
    no new geometry concept is introduced. `direction` mirrors that same
    model's own field ('both' | 'inbound' | 'outbound') and is used
    purely as a FILTER on which crossings count, never to change which
    side of the line is considered "in" vs "out"."""

    x1: float
    y1: float
    x2: float
    y2: float
    direction: str = "both"

    @classmethod
    def from_rule_geometry(cls, geometry: list[dict], direction: str = "both") -> "CountingLine":
        """Builds a CountingLine from a stored rule's geometry list --
        the exact shape analytics_rules.json already persists. Only the
        first two points are used; a line_crossing rule is only ever
        drawn with exactly two, but this tolerates extra points rather
        than raising, in case a rule was ever saved with more."""
        if len(geometry) < 2:
            raise ValueError("A counting line requires at least 2 geometry points.")
        p1, p2 = geometry[0], geometry[1]
        return cls(x1=float(p1["x"]), y1=float(p1["y"]), x2=float(p2["x"]), y2=float(p2["y"]), direction=direction)


def _line_cross_value(line: CountingLine, x: float, y: float) -> float:
    """Raw signed cross-product distance of (x, y) from the line -- the
    sign alone (ignoring magnitude) is what _side_of_line() below
    classifies into -1/0/+1. Exposed separately because the tracker's
    anti-jitter buffer (see PeopleCounter.line_buffer) needs the actual
    magnitude, not just the sign, to tell "clearly crossed" apart from
    "noise right at the line"."""
    return (line.x2 - line.x1) * (y - line.y1) - (line.y2 - line.y1) * (x - line.x1)


def _side_of_line(line: CountingLine, x: float, y: float) -> int:
    """Cross-product sign test: -1, 0, or +1 depending which side of the
    line (x, y) falls on. Which geometric side maps to "in" vs "out" is
    a fixed, documented convention (negative side = inbound) determined
    by the order the line's two points were drawn -- a known,
    disclosed limitation: if IN/OUT come out backwards in practice for
    a given camera, the fix is redrawing the line in the opposite point
    order, not a code change."""
    cross = _line_cross_value(line, x, y)
    if cross > 1e-9:
        return 1
    if cross < -1e-9:
        return -1
    return 0


def _distance(ax: float, ay: float, bx: float, by: float) -> float:
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _predicted_position(track: "Track") -> tuple[float, float]:
    """Where this track is expected to be THIS frame, extrapolated
    linearly from its last real step (cx - prev_cx, cy - prev_cy). A
    track with fewer than two real observations (prev_cx is still None)
    has no established velocity -- predicts its own current position,
    i.e. zero assumed movement, which reproduces the original fixed-
    distance-from-last-position behavior exactly for that case."""
    if track.prev_cx is None or track.prev_cy is None:
        return track.cx, track.cy
    return track.cx + (track.cx - track.prev_cx), track.cy + (track.cy - track.prev_cy)


def _box_of(cx: float, cy: float, w: float, h: float) -> tuple[float, float, float, float]:
    """A (x1, y1, x2, y2) box centered on (cx, cy) with the given
    normalized width/height. w/h of 0 (no box info available for this
    detection/track) produces a zero-area box -- _iou() below then
    always returns 0.0 for it, which is exactly the correct fallback:
    no box signal means the match decision falls back to pure centroid
    distance, unchanged from before this feature existed."""
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _iou(box_a: tuple[float, float, float, float], box_b: tuple[float, float, float, float]) -> float:
    """Standard intersection-over-union of two (x1, y1, x2, y2) boxes in
    the same normalized 0.0-1.0 frame space detections/tracks already
    use. Returns 0.0 for non-overlapping or degenerate (zero-area)
    boxes -- never raises, never divides by zero."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def _scale_penalty(area_a: float, area_b: float) -> float:
    """How inconsistent two box areas are, symmetric in both directions
    (a candidate half the size or double the size of the track's last
    known box is penalized equally). 0.0 when identical or when either
    area is unavailable (0) -- again, no box info means no penalty,
    the pure-centroid fallback is preserved exactly."""
    if area_a <= 0.0 or area_b <= 0.0:
        return 0.0
    ratio = area_a / area_b
    if ratio <= 0.0:
        return 0.0
    return abs(math.log(ratio))


# Matching-cost weighting: NEW additive signals on top of the existing,
# unchanged centroid-distance gate (max_match_distance itself is never
# widened by these -- see PeopleCounter.update()'s own docstring for
# why that's a deliberate, explicit constraint). Kept as small, fixed
# module constants rather than PeopleCounter constructor parameters for
# now -- these are new, not yet field-tuned; making them adjustable
# per-counter can follow once real walk-test data says they should be.
SCALE_PENALTY_WEIGHT = 0.15
IOU_BONUS_WEIGHT = 0.35
# A candidate detection may match a track EITHER because its centroid
# lands within max_match_distance of the predicted position (the
# original, unchanged rule) OR because its box clearly overlaps the
# predicted box even if the raw centroid distance is larger -- real,
# meaningful overlap (not a token sliver) is required for this second
# path, which is exactly what lets a bigger, closer (larger-box) person
# bridge a bigger normalized-distance jump without max_match_distance
# itself ever being widened for everyone.
MIN_IOU_FOR_MATCH = 0.15


def _dedupe_same_frame(detections: list[dict], merge_distance: float) -> list[dict]:
    """Collapses near-duplicate detections within a SINGLE frame (two
    boxes close enough together to plausibly be the same person
    detected twice) into one. YOLO already applies its own per-class
    NMS internally, so this is a cheap, deliberately conservative extra
    safeguard, not a replacement for real NMS -- merge_distance is kept
    small so two genuinely distinct, close-together people are never
    merged into one (see test_two_people_crossing_close_together_are_
    both_counted for the boundary this must respect)."""
    kept: list[dict] = []
    for det in detections:
        if any(_distance(det["x"], det["y"], k["x"], k["y"]) < merge_distance for k in kept):
            continue
        kept.append(det)
    return kept


@dataclass
class Track:
    track_id: int
    cx: float
    cy: float
    last_seen_frame: int
    side: int | None  # None until first classified off the line (0 = exactly on it)
    missed_frames: int = 0
    last_crossed_frame: int | None = None
    # prev_cx/prev_cy: this track's position ONE real match before its
    # current (cx, cy) -- None until a track has been matched at least
    # twice. Used only to derive a simple per-step velocity estimate for
    # predictive matching (see PeopleCounter.update()'s docstring) --
    # never used for anything else (not exposed in crossing events, not
    # part of the counting decision itself).
    prev_cx: float | None = None
    prev_cy: float | None = None
    # w/h: this track's most recently observed box size (normalized,
    # same units as cx/cy). 0.0 (the default) means "no box info seen
    # yet for this track" -- every box-based signal (_iou, scale
    # consistency) treats a zero-area box as "unavailable" and
    # contributes nothing, so a track that only ever received plain
    # {"x","y"} detections (no "w"/"h" keys) behaves EXACTLY as before
    # this feature existed.
    w: float = 0.0
    h: float = 0.0


@dataclass(frozen=True)
class CrossingEvent:
    """One real, counted crossing. Carries everything the caller needs
    to build a real AnalyticsEventModel/upload payload -- camera_id and
    a real datetime are attached by the integration layer in main.py,
    not here, since this module has no concept of which physical
    camera or wall-clock time it's running against (it only counts
    frames)."""

    track_id: int
    direction: str  # 'in' | 'out'
    frame_index: int
    x: float
    y: float


@dataclass
class CounterState:
    """Everything needed to persist and restore a PeopleCounter across
    a process restart WITHOUT silently resetting cumulative counts to
    zero -- addresses "camera reconnect/restart" and "analytics process
    restart" as first-class, not-just-a-cold-reset scenarios. Active
    in-flight tracks are deliberately NOT persisted (a track that was
    mid-crossing at the moment of a restart is lost and may be
    undercounted -- the same disclosed limitation as any occlusion
    longer than max_missed_frames; only the cumulative in/out totals,
    which are what actually matter for occupancy, survive a restart)."""

    in_count: int
    out_count: int
    frame_index: int


class PeopleCounter:
    """Stateful, one instance per camera. Call update() once per
    detection cycle with that frame's person-detection centroids.
    Deliberately simple (nearest-centroid greedy matching, not a
    Kalman/Hungarian-algorithm tracker) -- appropriate for a first,
    narrow implementation with no new inference cost; see the module
    docstring and the accompanying report for the specific tracking
    limitations this simplicity accepts."""

    def __init__(
        self,
        line: CountingLine,
        max_match_distance: float = 0.20,
        max_missed_frames: int = 3,
        recross_cooldown_frames: int = 2,
        dedupe_distance: float = 0.06,
        line_buffer: float = 0.03,
    ):
        self.line = line
        self.max_match_distance = max_match_distance
        self.max_missed_frames = max_missed_frames
        self.recross_cooldown_frames = recross_cooldown_frames
        self.dedupe_distance = dedupe_distance
        # The primary anti-jitter protection ("person stands on/near the
        # line" must never be double-counted): a track's CONFIRMED side
        # only changes when it moves clearly past this buffer distance
        # from the line -- raw sign noise within the buffer leaves the
        # track's recorded side untouched, so it can never flip-flop a
        # crossing event purely from standing still near the boundary.
        # recross_cooldown_frames remains a secondary safety net for a
        # person who genuinely, quickly crosses back and forth.
        self.line_buffer = line_buffer
        self.tracks: dict[int, Track] = {}
        self._next_id = itertools.count(1)
        self.frame_index = 0
        self.in_count = 0
        self.out_count = 0

    @property
    def occupancy(self) -> int:
        return self.in_count - self.out_count

    def state(self) -> CounterState:
        """Serializable snapshot for persistence across a restart."""
        return CounterState(in_count=self.in_count, out_count=self.out_count, frame_index=self.frame_index)

    def restore(self, state: CounterState) -> None:
        """Restores cumulative counts (never in-flight tracks -- see
        CounterState's own docstring) after a process restart."""
        self.in_count = state.in_count
        self.out_count = state.out_count
        self.frame_index = state.frame_index

    def reset_counts(self, occupancy: int = 0) -> None:
        """Explicit, admin-triggered recalibration -- e.g. "we know
        there are actually 3 people on site right now." Sets in_count
        to the given value and out_count to 0, which makes
        `occupancy` read back exactly `occupancy` immediately, while
        leaving future in/out deltas to move it normally from there.
        Never called automatically -- a silent, unexplained count reset
        would be worse than a wrong-but-explainable count."""
        self.in_count = max(0, occupancy)
        self.out_count = 0

    def update(self, detections: list[dict], debug: bool = False):
        """`detections`: this frame's person-class boxes, already
        reduced to centroids -- [{"x": cx, "y": cy}, ...] in the same
        normalized 0.0-1.0 space as the counting line. Returns the
        list of crossings counted THIS frame (usually empty).

        `debug=False` (the default): behavior and return value are
        EXACTLY as before this parameter was added -- returns only
        `events`. Every one of the 26 existing tests calls update()
        this way and is unaffected.

        `debug=True`: returns `(events, debug_entries)` instead --
        `debug_entries` is a list of per-detection dicts capturing
        exactly what the counting decision was and why, WITHOUT
        changing a single line of the actual counting math below (the
        debug entries are appended alongside the existing logic, never
        substituted for it). This is a pure, additive observability
        seam for diagnosing a real-world walk-test, not an algorithm
        change.

        Matching is velocity-aware AND box-aware. A track with at least
        two prior real observations predicts its next CENTER position by
        extrapolating its most recent per-step movement (`cx + (cx -
        prev_cx)`), exactly as before. On top of that unchanged center-
        distance signal, when box width/height are present on both the
        detection (`det["w"]`/`det["h"]`) and the track (from its own
        last real observation), two more signals are combined into the
        match cost: IoU between the track's PREDICTED box (predicted
        center + its last known size) and the candidate detection's box,
        and a symmetric box-AREA-consistency penalty (how different the
        candidate's size is from the track's last known size). Neither
        `max_match_distance` NOR the underlying velocity-prediction math
        is changed by any of this -- see the module-level SCALE_PENALTY_
        WEIGHT/IOU_BONUS_WEIGHT/MIN_IOU_FOR_MATCH constants for exactly
        how these NEW signals are weighted, all additive on top of the
        existing, unchanged gate.

        Acceptance gate (whether a detection-track pair may be matched
        at all): `center_distance <= max_match_distance` (the original,
        unwidened rule) **OR** `iou >= MIN_IOU_FOR_MATCH` (a real,
        meaningful box overlap, not a token sliver) -- a big, close/
        large-boxed person can bridge a bigger normalized-distance jump
        purely because their predicted and actual boxes still clearly
        overlap, without max_match_distance itself ever being loosened
        for every other, smaller/farther person. Detections or tracks
        with no box info (`w`/`h` missing or 0) always produce IoU=0.0
        and scale-penalty=0.0, so a pure-centroid caller (any of this
        module's earlier tests, none of which pass "w"/"h") sees
        IDENTICAL behavior to before this feature existed.

        Assignment itself is GLOBAL greedy, not per-detection-in-list-
        order: every detection-track pair that passes the gate is
        scored, then pairs are claimed lowest-cost-first across the
        WHOLE frame (a claimed detection or track is removed from
        further consideration). This -- not just the richer cost
        function -- is what actually prevents two nearby people from
        being able to steal each other's track purely because of which
        order they happened to appear in the detections list."""
        self.frame_index += 1
        events: list[CrossingEvent] = []
        debug_entries: list[dict] = []

        deduped = _dedupe_same_frame(detections, self.dedupe_distance)

        # Score every (detection, track) pair that passes the gate,
        # then assign globally by lowest cost first -- see the
        # docstring above for exactly what each signal contributes and
        # why this is safe for callers with no box info at all.
        candidates: list[tuple[float, int, int]] = []  # (cost, detection_index, track_id)
        for i, det in enumerate(deduped):
            det_w, det_h = det.get("w", 0.0), det.get("h", 0.0)
            det_area = det_w * det_h
            det_box = _box_of(det["x"], det["y"], det_w, det_h)
            for tid, t in self.tracks.items():
                predicted_x, predicted_y = _predicted_position(t)
                dist = _distance(det["x"], det["y"], predicted_x, predicted_y)
                predicted_box = _box_of(predicted_x, predicted_y, t.w, t.h)
                iou = _iou(predicted_box, det_box)
                if dist > self.max_match_distance and iou < MIN_IOU_FOR_MATCH:
                    continue  # neither signal accepts this pair
                scale_pen = _scale_penalty(det_area, t.w * t.h)
                cost = dist + SCALE_PENALTY_WEIGHT * scale_pen - IOU_BONUS_WEIGHT * iou
                candidates.append((cost, i, tid))
        candidates.sort(key=lambda c: c[0])

        assignment: dict[int, int] = {}  # detection index -> track id
        claimed_tracks: set[int] = set()
        claimed_dets: set[int] = set()
        for cost, i, tid in candidates:
            if i in claimed_dets or tid in claimed_tracks:
                continue
            assignment[i] = tid
            claimed_dets.add(i)
            claimed_tracks.add(tid)

        matched_ids: set[int] = set()
        for i, det in enumerate(deduped):
            x, y = det["x"], det["y"]
            raw_cross = _line_cross_value(self.line, x, y)
            if i in assignment:
                tid = assignment[i]
                track = self.tracks[tid]
                prev_side = track.side
                # Buffered reclassification: only move off the track's
                # previously CONFIRMED side once the raw signed distance
                # clearly exceeds line_buffer on the other side -- noise
                # within the buffer leaves `new_side` equal to prev_side,
                # so it can never register as a crossing.
                if raw_cross > self.line_buffer:
                    new_side = 1
                elif raw_cross < -self.line_buffer:
                    new_side = -1
                else:
                    new_side = prev_side if prev_side is not None else 0
                crossed = False
                crossing_reason = None
                if (
                    prev_side is not None
                    and prev_side != 0
                    and new_side != 0
                    and prev_side != new_side
                ):
                    cooldown_ok = track.last_crossed_frame is None or (self.frame_index - track.last_crossed_frame) >= self.recross_cooldown_frames
                    if not cooldown_ok:
                        crossing_reason = "side_changed_but_recross_cooldown_active"
                    else:
                        direction = "in" if new_side < 0 else "out"
                        if self.line.direction in ("both", direction):
                            events.append(CrossingEvent(track_id=tid, direction=direction, frame_index=self.frame_index, x=x, y=y))
                            if direction == "in":
                                self.in_count += 1
                            else:
                                self.out_count += 1
                            crossed = True
                            crossing_reason = f"counted_{direction}"
                        else:
                            crossing_reason = f"side_changed_but_direction_filter_excludes_{direction}"
                        track.last_crossed_frame = self.frame_index
                elif prev_side is None:
                    crossing_reason = "no_prior_confirmed_side_yet"
                elif prev_side == new_side:
                    crossing_reason = "side_unchanged"
                elif new_side == 0:
                    crossing_reason = "within_line_buffer_no_reclassification"
                if debug:
                    debug_entries.append({
                        "detection_x": x, "detection_y": y, "raw_cross_value": raw_cross,
                        "track_id": tid, "track_status": "matched",
                        "prev_side": prev_side, "new_side": new_side,
                        "crossed": crossed, "crossing_reason": crossing_reason,
                    })
                # Shift the position history by one step BEFORE
                # overwriting cx/cy -- prev_cx/prev_cy becomes what cx/cy
                # was, so the next call's velocity estimate is always
                # derived from the two most recent real observations.
                track.prev_cx, track.prev_cy = track.cx, track.cy
                track.cx, track.cy = x, y
                # Only overwrite the track's known box size when this
                # detection actually carries one -- a caller that never
                # sends "w"/"h" must never have a track's size silently
                # decay to 0 (which would poison future scale-penalty/
                # IoU comparisons for no reason).
                if "w" in det and "h" in det:
                    track.w, track.h = det["w"], det["h"]
                track.side = new_side if new_side != 0 else prev_side
                track.last_seen_frame = self.frame_index
                track.missed_frames = 0
                matched_ids.add(tid)
            else:
                tid = next(self._next_id)
                initial_side = _side_of_line(self.line, x, y)  # no previous side to buffer against yet
                self.tracks[tid] = Track(
                    track_id=tid, cx=x, cy=y, last_seen_frame=self.frame_index,
                    side=initial_side if initial_side != 0 else None,
                    w=det.get("w", 0.0), h=det.get("h", 0.0),
                )
                matched_ids.add(tid)
                if debug:
                    debug_entries.append({
                        "detection_x": x, "detection_y": y, "raw_cross_value": raw_cross,
                        "track_id": tid, "track_status": "new_track",
                        "prev_side": None, "new_side": initial_side,
                        "crossed": False, "crossing_reason": "new_track_no_prior_side",
                    })

        expired_tracks = []
        for tid in list(self.tracks):
            if tid not in matched_ids:
                track = self.tracks[tid]
                track.missed_frames += 1
                if track.missed_frames > self.max_missed_frames:
                    expired_tracks.append(tid)
                    del self.tracks[tid]
                elif debug:
                    debug_entries.append({
                        "detection_x": None, "detection_y": None, "raw_cross_value": None,
                        "track_id": tid, "track_status": "missed_this_frame",
                        "prev_side": track.side, "new_side": track.side,
                        "crossed": False, "crossing_reason": f"missed_frame_{track.missed_frames}_of_{self.max_missed_frames}_grace",
                    })
        if debug and expired_tracks:
            for tid in expired_tracks:
                debug_entries.append({
                    "detection_x": None, "detection_y": None, "raw_cross_value": None,
                    "track_id": tid, "track_status": "expired_pruned",
                    "prev_side": None, "new_side": None,
                    "crossed": False, "crossing_reason": "exceeded_max_missed_frames",
                })

        if debug:
            return events, debug_entries
        return events
