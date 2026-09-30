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

from datetime import datetime, timedelta
from typing import Optional

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


def plausible_frame_time(candidate: Optional[datetime], now: datetime) -> Optional[datetime]:
    if candidate is None or candidate > now:
        return None
    if (now - candidate).total_seconds() > MAX_PLAUSIBLE_FRAME_AGE_SECONDS:
        return None
    return candidate


def detection_event_span(
    frame_time: Optional[datetime],
    previous_frame_time: Optional[datetime],
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
    moment = plausible_frame_time(frame_time, now) or now
    earliest = moment - timedelta(seconds=MAX_EVENT_LEAD_SECONDS)
    if previous_frame_time is not None and earliest <= previous_frame_time < moment:
        return previous_frame_time, moment
    return max(earliest, moment - timedelta(seconds=max(0.0, scan_interval_seconds))), moment
