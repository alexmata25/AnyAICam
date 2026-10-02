"""Local Event-mode recording policy: pure, dependency-free decision
functions -- no ffmpeg, no filesystem, no FastAPI -- for exactly two
questions the actual recording pipeline (app/main.py) needs answered:

1. Is a short rolling-buffer segment still needed, or safe to delete?
   Event-mode cameras write a short (tens-of-seconds) rolling buffer
   instead of app/main.py's existing unconditional 5-minute continuous
   segments (start_recording()), purely so real motion has recent
   source footage to extract a pre-roll from. A buffer segment is only
   ever kept long enough to cover the configured pre-roll lookback (or
   an event actively being built from it) -- this is the mechanism
   that actually stops idle time from accumulating on disk, not a UI
   filter over already-recorded footage.

2. Does a new detection extend the event recording currently being
   written, or does it start a brand new one? Reuses
   event_clips.should_merge() unchanged for the actual merge-gap rule
   (adjacent/overlapping motion becomes one recording, not many tiny
   ones) and adds the one thing that rule doesn't cover on its own: a
   maximum-length SAFETY CAP so one long unbroken activity period
   (e.g. a camera pointed at a busy street) still rolls over into a
   new file eventually, rather than growing without bound. This is a
   ceiling for a pathological case, never the normal clip duration --
   normal event recordings are sized by compute_clip_window() in
   event_clips.py, exactly as today.

Continuous mode is completely unaffected by this module -- it is never
imported or consulted anywhere in that code path."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_BUFFER_SEGMENT_SECONDS = 30
# How far back a buffer segment must still be reachable to cover the
# largest configured pre-roll a customer could ask for, plus a fixed
# safety margin so a segment isn't deleted right as an event build
# starts reading it.
DEFAULT_BUFFER_SAFETY_MARGIN_SECONDS = 10
DEFAULT_MAX_EVENT_RECORDING_SECONDS = 300
# An AI event's clip starts before the moment it was scheduled: the
# detection frame is a few seconds old, the event is taken to start at the
# previous scan (detection_timing.py, up to MAX_EVENT_LEAD_SECONDS), and
# the recording is cut only after the post-roll. Buffer segments must stay
# reachable that far back, beyond the pre-roll itself.
DETECTION_LOOKBACK_SECONDS = 60


@dataclass(frozen=True)
class BufferSegment:
    """A short rolling-buffer segment already on disk -- [start, end),
    real (ffprobed) times, not the segmenter's nominal duration."""
    start: datetime
    end: datetime


def buffer_retention_cutoff(
    now: datetime,
    *,
    pre_roll_seconds: int,
    safety_margin_seconds: int = DEFAULT_BUFFER_SAFETY_MARGIN_SECONDS,
    lookback_seconds: int = 0,
) -> datetime:
    """The oldest a buffer segment might still be needed to satisfy a
    fresh event's pre-roll, right now. Any segment that already ended
    before this cutoff cannot contribute pre-roll to any event detected
    from this moment forward."""
    return now - timedelta(seconds=pre_roll_seconds + safety_margin_seconds + lookback_seconds)


def is_buffer_segment_still_needed(
    segment: BufferSegment,
    *,
    now: datetime,
    pre_roll_seconds: int,
    safety_margin_seconds: int = DEFAULT_BUFFER_SAFETY_MARGIN_SECONDS,
    in_flight_event_windows: tuple[tuple[datetime, datetime], ...] = (),
    lookback_seconds: int = 0,
) -> bool:
    """False means the janitor may delete this segment now -- this is
    the actual mechanism that keeps an idle Event-mode camera from
    accumulating hours of footage: a segment is kept only long enough
    to cover a possible pre-roll, or while an event currently being
    extracted still overlaps it (a build can legitimately take a few
    seconds, and must never race the janitor for the same file)."""
    cutoff = buffer_retention_cutoff(
        now, pre_roll_seconds=pre_roll_seconds, safety_margin_seconds=safety_margin_seconds,
        lookback_seconds=lookback_seconds,
    )
    if segment.end >= cutoff:
        return True
    for window_start, window_end in in_flight_event_windows:
        if segment.start < window_end and segment.end > window_start:
            return True
    return False


def keyframe_cut(request_start: datetime, segment_start: datetime, keyframes: list[float]) -> tuple[float, datetime]:
    """Where an Event-mode recording cut from one buffer segment really
    starts: (inpoint seconds into that segment, true wall-clock start).

    A stream-copy cut can only begin on a keyframe, so it begins on the last
    keyframe at or before the requested start -- never after it (that would
    lose the beginning of the event). The returned true start is what the
    recording must be labelled with: every consumer (event clips, linked
    recordings, Playback) maps time to position from that label.

    Found 2026-10-01 (Ryzen, Living Room): neither previous cut honoured
    this. An output-side -ss began on the NEXT keyframe while the file was
    labelled with the requested time (content up to one keyframe interval
    late); an input-side -ss on the concat demuxer ignored the seek and
    began at the start of the first buffer file (content 6-8 s early). Both
    shifted every event clip cut from the recording."""
    local = max(0.0, (request_start - segment_start).total_seconds())
    inpoint = max([k for k in keyframes if 0.0 <= k <= local + 1e-3] or [0.0])
    return inpoint, segment_start + timedelta(seconds=inpoint)


def recording_name_time(true_start: datetime) -> datetime:
    """The whole second an Event-mode recording is named by (its filename
    carries seconds only). Rounded UP: the label may then be up to a second
    later than the first frame, so a clip cut by label starts slightly
    early -- never late."""
    whole = true_start.replace(microsecond=0)
    return whole if whole == true_start else whole + timedelta(seconds=1)


def should_start_new_event_recording(
    *,
    current_recording_start: datetime,
    current_recording_end: datetime,
    detection_start: datetime,
    merge_gap_seconds: int,
    max_event_recording_seconds: int = DEFAULT_MAX_EVENT_RECORDING_SECONDS,
) -> bool:
    """True: close the currently-open event recording and start a new
    one for this detection. False: extend the current recording to
    cover this detection too (the same file, a longer clip).

    Two independent reasons to start fresh, either is sufficient:
    - the gap since the last detection exceeds merge_gap_seconds (the
      unchanged event_clips.should_merge() rule -- this is genuinely a
      separate, later activity period, not the same one continuing);
    - honoring the merge would grow the current recording past
      max_event_recording_seconds -- the safety cap. This only ever
      fires during one long, literally unbroken activity period; it is
      not how a normal, human-scale event's length is decided."""
    from event_clips import should_merge

    if not should_merge(current_recording_end, detection_start, merge_gap_seconds=merge_gap_seconds):
        return True
    prospective_length = (detection_start - current_recording_start).total_seconds()
    return prospective_length > max_event_recording_seconds
