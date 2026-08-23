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
        change."""
        self.frame_index += 1
        events: list[CrossingEvent] = []
        debug_entries: list[dict] = []

        deduped = _dedupe_same_frame(detections, self.dedupe_distance)

        # Greedy nearest-neighbor matching: for each detection, find the
        # closest still-unclaimed existing track within max_match_distance.
        unmatched_track_ids = set(self.tracks)
        assignment: dict[int, int] = {}  # detection index -> track id
        for i, det in enumerate(deduped):
            best_id, best_dist = None, None
            for tid in unmatched_track_ids:
                t = self.tracks[tid]
                dist = _distance(det["x"], det["y"], t.cx, t.cy)
                if dist <= self.max_match_distance and (best_dist is None or dist < best_dist):
                    best_id, best_dist = tid, dist
            if best_id is not None:
                assignment[i] = best_id
                unmatched_track_ids.discard(best_id)

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
                track.cx, track.cy = x, y
                track.side = new_side if new_side != 0 else prev_side
                track.last_seen_frame = self.frame_index
                track.missed_frames = 0
                matched_ids.add(tid)
            else:
                tid = next(self._next_id)
                initial_side = _side_of_line(self.line, x, y)  # no previous side to buffer against yet
                self.tracks[tid] = Track(track_id=tid, cx=x, cy=y, last_seen_frame=self.frame_index, side=initial_side if initial_side != 0 else None)
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
