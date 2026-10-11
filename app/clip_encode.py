"""Event / manual clip FFmpeg commands (2026-10-11, video-pipeline P2c).

Measured on the Samsung appliance (8MP H.265 main stream): every event
clip build took 1.25-1.7 cores for its whole run, because

  * "-ss" came AFTER "-i" (output seeking): FFmpeg decoded, and threw
    away, every 4K HEVC frame from the start of the 300 s recording
    segment up to the event -- up to 5 minutes of 4K decode per clip;
  * the clip was re-encoded with libx264 at the camera's full 4K size.

The "optimized" command (the default) instead

  * seeks on the INPUT ("-ss" before "-i"): the demuxer jumps to the
    keyframe before the offset and FFmpeg's default accurate_seek trims
    the decoded frames to the exact offset, so the clip starts on the same
    frame as before (tests compare the selected frames frame-by-frame);
    a window inside ONE recording file reads that file directly (no concat
    demuxer);
  * scales into the camera's measured profile box (video_profile.py;
    4K -> 1920x1080, 5MP 4:3 -> 1440x1080, never upscaled, aspect kept)
    as H.264 yuv420p with +faststart -- playable in every browser -- and a
    bitrate cap from the output size;
  * caps FFmpeg at ANYAICAM_EVENT_CLIP_THREADS threads (2) so a clip build
    no longer competes for every core with live video and recording.

ANYAICAM_EVENT_CLIP_ENCODE=legacy restores the previous command exactly.
ANYAICAM_EVENT_CLIP_SCALE=full keeps input seeking and the thread cap but
not the downscale. Customer-requested (manual) clips keep the camera's
full resolution unless ANYAICAM_MANUAL_CLIP_SCALE=profile."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

ENCODE_MODES = ("optimized", "legacy")


def _mode(name: str, default: str, allowed: Sequence[str], fallback: str) -> str:
    value = (os.environ.get(name, default) or "").strip().lower()
    return value if value in allowed else fallback


EVENT_CLIP_ENCODE = _mode("ANYAICAM_EVENT_CLIP_ENCODE", "optimized", ENCODE_MODES, "legacy")
EVENT_CLIP_SCALE = _mode("ANYAICAM_EVENT_CLIP_SCALE", "profile", ("profile", "full"), "profile")
MANUAL_CLIP_SCALE = _mode("ANYAICAM_MANUAL_CLIP_SCALE", "full", ("profile", "full"), "full")
try:
    CLIP_THREADS = max(0, int(os.environ.get("ANYAICAM_EVENT_CLIP_THREADS", "2") or 0))
except ValueError:
    CLIP_THREADS = 2


def _seconds(value: float) -> str:
    return f"{max(0.0, float(value)):.3f}"


def legacy_event_command(list_file: Path, offset: float, duration: float, output: Path) -> list[str]:
    """build_motion_event_clip()'s command before 2026-10-11, unchanged."""
    return [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ss", str(offset), "-t", str(duration),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", str(output),
    ]


def legacy_manual_command(list_file: Path, offset: float, duration: float, output: Path) -> list[str]:
    """build_manual_clip()'s command before 2026-10-11, unchanged."""
    return [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ss", str(offset), "-t", str(duration), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart",
        str(output),
    ]


def seek_input_args(sources: Sequence[Path], list_file: Path, offset: float, threads: int) -> list[str]:
    """Input seeking: the single covering file directly, else the concat list."""
    thread_args = ["-threads", str(threads)] if threads else []
    if len(sources) == 1:
        return [*thread_args, "-ss", _seconds(offset), "-i", str(sources[0])]
    return [*thread_args, "-f", "concat", "-safe", "0", "-ss", _seconds(offset), "-i", str(list_file)]


def video_encode_args(scale_filter: Optional[str], max_kbps: Optional[int], threads: int) -> list[str]:
    args = []
    args += ["-vf", scale_filter] if scale_filter else ["-pix_fmt", "yuv420p"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if max_kbps:
        args += ["-maxrate", f"{int(max_kbps)}k", "-bufsize", f"{int(max_kbps) * 2}k"]
    if threads:
        args += ["-threads", str(threads)]
    return args


def event_clip_command(sources: Sequence[Path], list_file: Path, offset: float, duration: float, output: Path,
                       profile=None, *, mode: str = None, scale: str = None, threads: int = None) -> list[str]:
    mode = EVENT_CLIP_ENCODE if mode is None else mode
    if mode != "optimized":
        return legacy_event_command(list_file, offset, duration, output)
    scale = EVENT_CLIP_SCALE if scale is None else scale
    threads = CLIP_THREADS if threads is None else threads
    use_profile = scale == "profile" and profile is not None
    return [
        "ffmpeg", "-y",
        *seek_input_args(sources, list_file, offset, threads),
        "-t", _seconds(duration),
        "-map", "0:v:0", "-map", "0:a:0?",
        *video_encode_args(profile.clip_filter() if use_profile else None,
                           profile.clip_max_kbps if use_profile else None, threads),
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", str(output),
    ]


def manual_clip_command(sources: Sequence[Path], list_file: Path, offset: float, duration: float, output: Path,
                        profile=None, *, mode: str = None, scale: str = None, threads: int = None) -> list[str]:
    mode = EVENT_CLIP_ENCODE if mode is None else mode
    if mode != "optimized":
        return legacy_manual_command(list_file, offset, duration, output)
    scale = MANUAL_CLIP_SCALE if scale is None else scale
    threads = CLIP_THREADS if threads is None else threads
    use_profile = scale == "profile" and profile is not None
    return [
        "ffmpeg", "-y",
        *seek_input_args(sources, list_file, offset, threads),
        "-t", _seconds(duration), "-an",
        *video_encode_args(profile.clip_filter() if use_profile else None,
                           profile.clip_max_kbps if use_profile else None, threads),
        "-movflags", "+faststart", str(output),
    ]
