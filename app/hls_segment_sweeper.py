"""Removes live-HLS segments orphaned by FFmpeg restarts (2026-09-25).

Root cause (reproduced with FFmpeg 6.1 and confirmed against the Ryzen's
7.1 data): start_live_stream() runs FFmpeg with
`-hls_list_size 5 -hls_flags delete_segments+append_list`. With
delete_segments, FFmpeg keeps a segment that has just left the playlist in
its own pending-delete list (hls_delete_threshold, default 1) and deletes
it one rotation later. When the process exits for ANY reason -- RTSP drop,
watchdog restart, SIGTERM, SIGKILL, crash -- that pending segment is in no
playlist and remembered by no process: append_list lets the next
generation inherit only the playlist's own 5 segments. Exactly one
orphaned camera<N>_<seq>.ts per FFmpeg generation, forever. On the Ryzen:
11,432 single-segment orphans, ~30 GB in 12 days, most on the cameras
whose streams restart most (~140 generations/day).

The sweep deletes a file only when ALL of these hold:
  - its name is exactly camera<N>_<digits>.ts (never a playlist, a temp
    file, or anything else in the folder);
  - it is not listed in camera<N>.m3u8 -- the live window is never touched;
  - it is older than min_age_seconds (default 10 minutes; live segments
    are 2 s and FFmpeg deletes its own within ~12 s, so nothing that any
    reader could still be using is that old);
  - that camera's playlist is readable, or does not exist at all (a
    camera no longer streaming). An unreadable playlist skips the camera.
It is camera-count agnostic: cameras are whatever camera<N> prefixes are
on disk. Recording, clips/media, MediaMTX/P2P and FFmpeg's own flags are
untouched.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

SEGMENT_NAME = re.compile(r"^(camera\d+)_\d+\.ts$")
SWEEP_ENABLED = os.environ.get("ANYAICAM_HLS_ORPHAN_SWEEP_ENABLED", "true").strip().lower() == "true"
MIN_AGE_SECONDS = max(120, int(os.environ.get("ANYAICAM_HLS_ORPHAN_MIN_AGE_SECONDS", "600")))
SWEEP_INTERVAL_SECONDS = max(60, int(os.environ.get("ANYAICAM_HLS_ORPHAN_SWEEP_INTERVAL_SECONDS", "300")))
# Bounds one pass's disk I/O; a large backlog is cleared over a few passes.
MAX_DELETIONS_PER_SWEEP = max(100, int(os.environ.get("ANYAICAM_HLS_ORPHAN_MAX_DELETIONS", "3000")))

sweeper_state: dict = {"last_sweep_at": None, "last_result": None, "total_deleted": 0, "total_bytes": 0}


def referenced_segments(playlist: Path) -> set[str] | None:
    """Basenames of the segments a playlist lists; an empty set when the
    playlist does not exist; None when it exists but cannot be read."""
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    return {Path(line.strip()).name for line in text.splitlines() if line.strip() and not line.startswith("#")}


def sweep(hls_folder: Path, *, min_age_seconds: int = MIN_AGE_SECONDS, max_deletions: int = MAX_DELETIONS_PER_SWEEP,
          now: float | None = None) -> dict:
    """One pass. Returns {"deleted", "bytes", "skipped_cameras", "cameras"}; never raises."""
    now = time.time() if now is None else now
    result = {"deleted": 0, "bytes": 0, "skipped_cameras": [], "cameras": {}}
    try:
        entries = list(os.scandir(hls_folder))
    except OSError:
        return result
    by_camera: dict[str, list[os.DirEntry]] = {}
    for entry in entries:
        match = SEGMENT_NAME.match(entry.name)
        if match and entry.is_file(follow_symlinks=False):
            by_camera.setdefault(match.group(1), []).append(entry)
    for camera, segments in sorted(by_camera.items()):
        live = referenced_segments(Path(hls_folder) / f"{camera}.m3u8")
        if live is None:
            result["skipped_cameras"].append(camera)
            continue
        for entry in segments:
            if result["deleted"] >= max_deletions:
                return result
            if entry.name in live:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
                if now - stat.st_mtime < min_age_seconds:
                    continue
                os.unlink(entry.path)
            except FileNotFoundError:
                continue  # FFmpeg removed it first
            except OSError:
                continue
            result["deleted"] += 1
            result["bytes"] += stat.st_size
            result["cameras"][camera] = result["cameras"].get(camera, 0) + 1
    return result


async def hls_segment_sweeper_worker(hls_folder: Path, log=print) -> None:
    """Sweeps once at start (clearing any backlog over a few passes), then
    every SWEEP_INTERVAL_SECONDS. Idles forever when disabled."""
    import asyncio
    if not SWEEP_ENABLED:
        while True:
            await asyncio.sleep(3600)
    while True:
        try:
            result = await asyncio.to_thread(sweep, Path(hls_folder))
            sweeper_state.update(last_sweep_at=time.time(), last_result=result)
            sweeper_state["total_deleted"] += result["deleted"]
            sweeper_state["total_bytes"] += result["bytes"]
            if result["deleted"] or result["skipped_cameras"]:
                log(f"hls_segment_sweeper: removed {result['deleted']} orphaned segment(s), "
                    f"{result['bytes'] / 1e6:.1f} MB, per camera {result['cameras']}, skipped {result['skipped_cameras']}")
            backlog = result["deleted"] >= MAX_DELETIONS_PER_SWEEP
        except asyncio.CancelledError:
            raise
        except Exception as error:  # never let housekeeping take the VMS down
            log(f"hls_segment_sweeper: pass failed: {error}")
            backlog = False
        await asyncio.sleep(10 if backlog else SWEEP_INTERVAL_SECONDS)
