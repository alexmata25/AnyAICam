"""When did the frame an AI detection came from actually happen, and when did
the real event (e.g. a vehicle entering the scene) begin? Pure functions, no
ffmpeg / OpenCV / filesystem, so the timing rules are unit-testable.

Why this exists (2026-09-30): vehicle event clips started with the vehicle
already halfway through the frame or leaving. The detector reads its frame
by opening the camera's live HLS playlist with OpenCV/FFmpeg, and FFmpeg's
HLS demuxer starts a live playlist `live_start_index` = -3 segments back
from the newest one (CAP_PROP_BUFFERSIZE does not change that). With 2 s
segments that frame is already ~6 s old (more when a camera's keyframe
interval makes segments longer), and the event was then stamped with
datetime.now() only after YOLO and the LPR scan had run. The 5 s pre-roll
was therefore counted back from a moment several seconds AFTER the frame
that showed the vehicle, so the clip could begin after the vehicle had
already entered.

The fix has two parts, both here:
1. estimate_hls_frame_time(): the wall-clock time of the frame FFmpeg reads
   from the playlist -- the start of the segment it opens, derived from the
   newest segment's end time and the playlist's own #EXTINF durations.
2. detection_event_span(): the real event starts no later than the previous
   scanned frame (which did not trigger a detection), so the clip's
   pre-roll is counted back from there, not from the detection frame. A
   vehicle that entered between two scans is therefore always inside the
   clip."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

# FFmpeg's HLS demuxer default for live playlists (libavformat/hls.c).
FFMPEG_HLS_LIVE_START_INDEX = -3
# A frame-time estimate older than this is not trusted (clock or file
# problem); the caller falls back to "now", the previous behavior.
MAX_PLAUSIBLE_FRAME_AGE_SECONDS = 60
# Upper bound on how far before the detection frame the event may be taken
# to start (a very long gap since the previous scan -- e.g. the detector
# was restarting -- must not produce a multi-minute clip).
MAX_EVENT_LEAD_SECONDS = 30


def parse_hls_segments(manifest_text: str) -> list[tuple[str, float]]:
    """(segment uri, #EXTINF duration seconds) in playlist order."""
    segments: list[tuple[str, float]] = []
    duration: Optional[float] = None
    for raw in (manifest_text or "").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
            except ValueError:
                duration = None
        elif line and not line.startswith("#"):
            if duration is not None and duration > 0:
                segments.append((line, duration))
            duration = None
    return segments


def estimate_hls_frame_time(
    segments: list[tuple[str, float]],
    newest_segment_end: datetime,
    *,
    live_start_index: int = FFMPEG_HLS_LIVE_START_INDEX,
) -> Optional[datetime]:
    """Wall-clock time of the first frame FFmpeg returns when it opens this
    live playlist: the start of segment `live_start_index` (counted from the
    end). newest_segment_end is when the newest listed segment finished
    (its file's modification time). If the playlist gains a segment between
    reading it and opening it, FFmpeg starts one segment later than this
    estimate, so any error errs towards MORE pre-roll, never less."""
    if not segments:
        return None
    start_index = max(0, len(segments) + live_start_index) if live_start_index < 0 else min(live_start_index, len(segments) - 1)
    lead = sum(duration for _, duration in segments[start_index:])
    return newest_segment_end - timedelta(seconds=lead)


# L-2 segment freshness (Codex RC6 review, 2026-10-18): a playlist can be freshly rewritten while the segments it
# lists -- the ones FFmpeg actually decodes -- are stale (left over from before an outage) or future-dated (written
# before a clock step back). The playlist check alone let those frames reach YOLO/LPR, and an implausible segment time
# then silently became "now". The live analytics reader therefore validates the segments FFmpeg will open (the last
# -FFMPEG_HLS_LIVE_START_INDEX listed) and refuses the frame instead of guessing its time.
# How far the derived frame time may lie in the future (file-timestamp jitter); anything later is refused.
FRAME_TIME_FUTURE_SKEW_SECONDS = 2.0


def hls_live_segments_problem(
    folder,
    segments: list[tuple[str, float]],
    *,
    now: float,
    max_age: float,
    live_start_index: int = FFMPEG_HLS_LIVE_START_INDEX,
) -> tuple[Optional[datetime], Optional[str]]:
    """(frame_time, None) when every segment FFmpeg will decode from this live playlist exists and is current; else
    (None, reason). `now` is time.time(). A consumed segment is stale when it is older than max_age plus the duration
    of the segments listed after it (it finished that much earlier), and future-dated when its mtime is more than
    max_age ahead -- the same symmetric bound as the playlist (live_hls_demand.playlist_fresh) and L-1. frame_time is
    the estimated wall-clock time of the first frame FFmpeg returns; it must be plausible (at most
    FRAME_TIME_FUTURE_SKEW_SECONDS ahead, at most MAX_PLAUSIBLE_FRAME_AGE_SECONDS old) or the frame is refused --
    never replaced by "now"."""
    from pathlib import Path
    if not segments:
        return None, "the playlist lists no segments"
    start = max(0, len(segments) + live_start_index) if live_start_index < 0 else min(live_start_index, len(segments) - 1)
    consumed = segments[start:]
    newest_mtime = None
    for index, (name, _duration) in enumerate(consumed):
        if "/" in name or "\\" in name or ".." in name or ":" in name:
            return None, f"unexpected segment reference {name!r}"
        try:
            mtime = (Path(folder) / name).stat().st_mtime
        except OSError:
            return None, f"segment {name} is missing"
        later = sum(duration for _, duration in consumed[index + 1:])
        age = now - mtime
        if age < -max_age:
            return None, f"segment {name} is future-dated ({-age:.0f} s ahead)"
        if age > max_age + later:
            return None, f"segment {name} is stale ({age:.0f} s old)"
        newest_mtime = mtime
    frame_time = estimate_hls_frame_time(consumed, datetime.fromtimestamp(newest_mtime), live_start_index=0)
    now_dt = datetime.fromtimestamp(now)
    if frame_time is None or frame_time > now_dt + timedelta(seconds=FRAME_TIME_FUTURE_SKEW_SECONDS):
        return None, "the frame time would be in the future"
    if (now_dt - frame_time).total_seconds() > MAX_PLAUSIBLE_FRAME_AGE_SECONDS:
        return None, "the frame time would be implausibly old"
    return min(frame_time, now_dt), None


_log = logging.getLogger("anyaicam.detection_timing")
# Epoch seconds outside this window are not a camera frame time (e.g. a counter or milliseconds).
_EPOCH_MIN, _EPOCH_MAX = 946684800.0, 4102444800.0          # 2000-01-01 .. 2100-01-01 UTC


def normalize_frame_time(value: Any) -> Optional[datetime]:
    """The single input boundary for frame timestamps (Red 2026-10-18, after a str reached a datetime comparison).

    Every real producer passes a naive local datetime (_read_hls_analytics_frame, analytics_frames' wall clock,
    ai_last_frame_time). Anything else is normalized to that form or rejected -- never compared raw:
      * naive datetime -> unchanged; aware datetime -> converted to naive local time;
      * ISO-8601 text (a trailing 'Z' is UTC) -> parsed, then as above;
      * int/float epoch seconds within 2000..2100 -> naive local time;
      * anything else (bool, unparseable text, other types, out-of-range numbers) -> None, logged once per call.
    None means "unknown": callers fall back to `now`, exactly as for a missing frame time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            _log.warning("detection_timing.frame_time_rejected reason=unparseable_text")
            return None
        return normalize_frame_time(parsed)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if _EPOCH_MIN <= float(value) <= _EPOCH_MAX:
            return datetime.fromtimestamp(float(value))
        _log.warning("detection_timing.frame_time_rejected reason=epoch_out_of_range")
        return None
    _log.warning("detection_timing.frame_time_rejected reason=unsupported_type type=%s", type(value).__name__)
    return None


def plausible_frame_time(candidate: Any, now: datetime) -> Optional[datetime]:
    candidate = normalize_frame_time(candidate)
    if candidate is None or candidate > now:
        return None
    if (now - candidate).total_seconds() > MAX_PLAUSIBLE_FRAME_AGE_SECONDS:
        return None
    return candidate


def detection_event_span(
    frame_time: Any,
    previous_frame_time: Any,
    *,
    now: datetime,
    scan_interval_seconds: float,
) -> tuple[datetime, datetime]:
    """(event_start, detection_moment) for one AI detection.

    detection_moment is when the detection frame happened (falls back to
    `now` if unknown). event_start is the previous scanned frame's time --
    the object was not detected there, so it entered after it -- or, with
    no usable previous frame, one scan interval before the detection. It is
    never more than MAX_EVENT_LEAD_SECONDS before the detection."""
    previous_frame_time = normalize_frame_time(previous_frame_time)
    moment = plausible_frame_time(frame_time, now) or now
    earliest = moment - timedelta(seconds=MAX_EVENT_LEAD_SECONDS)
    if previous_frame_time is not None and earliest <= previous_frame_time < moment:
        return previous_frame_time, moment
    return max(earliest, moment - timedelta(seconds=max(0.0, scan_interval_seconds))), moment


def plate_event_span(
    frame_time: Optional[datetime],
    previous_frame_time: Optional[datetime],
    *,
    now: datetime,
    first_read_age_seconds: float,
    scan_interval_seconds: float,
) -> tuple[datetime, datetime, datetime]:
    """(event_start, first_read, confirmed_moment) for a confirmed plate.

    A plate is confirmed only after several agreeing reads, so the frame it
    was confirmed on can be well after the vehicle arrived. first_read is
    when it was first read (the event's time); the event starts one scan
    interval before that -- or at the previous scanned frame, if earlier --
    and never more than MAX_EVENT_LEAD_SECONDS before the confirmation."""
    start, moment = detection_event_span(frame_time, previous_frame_time, now=now,
                                         scan_interval_seconds=scan_interval_seconds)
    age = max(0.0, min(float(first_read_age_seconds or 0.0), MAX_EVENT_LEAD_SECONDS))
    first_read = moment - timedelta(seconds=age)
    earliest = moment - timedelta(seconds=MAX_EVENT_LEAD_SECONDS)
    start = max(earliest, min(start, first_read - timedelta(seconds=max(0.0, scan_interval_seconds))))
    return start, first_read, moment
