"""Focused tests for the build_motion_event_clip() event-loop-blocking fix.

Background: build_motion_event_clip() used to call subprocess.run(ffprobe)
synchronously, once per .mkv file in sorted(camera_folder.glob("*.mkv")) --
i.e. every recording a camera has ever retained, unfiltered. Confirmed
live on the Samsung production appliance via repeated py-spy captures:
with ~1,048 retained recordings for one camera, a single motion event's
clip build kept the FastAPI event loop's own MainThread pinned inside
this exact call chain for 3.7-3.8 minutes at a time, back-to-back,
reproducing the HTTP outage (/health, /playback, /events, dashboard all
timing out) for the full duration.

The fix adds a cheap filename-timestamp shortlist (recording_start(),
no subprocess) before any ffprobe call, bounded by
2 * RECORDING_SEGMENT_SECONDS on the early side and window.end on the
late side, and moves the remaining (now small) ffprobe work off the
event loop via asyncio.to_thread() -- the same pattern already used for
delete_expired_recordings() and _refresh_camera_map() elsewhere in this
codebase.

This file proves:
  1. 1,000 historical recordings do not cause 1,000 ffprobe calls.
  2. Only recordings around the event timestamp are actually probed.
  3. An event whose window crosses a 5-minute recording-segment boundary
     still finds and uses both neighboring segments.
  4. The remaining ffprobe work runs off the asyncio event loop (proven
     by thread-id capture, matching the technique already used for the
     motion-event-persistence and retention-worker fixes this session).
  5. Existing motion-event clip/media behavior (event id, output paths,
     concat list, candidate-selection/"covering file" logic) is
     unchanged for a simple single-segment case.
  6. A heartbeat coroutine keeps ticking on its own schedule while a
     motion clip is being built -- direct, timing-based proof the event
     loop is not blocked during the (now off-loop) ffprobe work.

Same import/isolation constraints as the other fix-verification test
files in this suite: imports `main` (must run inside the deployed
container's Python), and every test redirects RECORDINGS_FOLDER and
CLIPS_FOLDER to tmp_path locations before running -- nothing here ever
touches real production recordings or event data.
"""

import asyncio
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import main
from event_clips import compute_clip_window


@pytest.fixture(autouse=True)
def _isolated_clip_paths(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    monkeypatch.setattr(main, "CLIPS_FOLDER", clips)
    return {"recordings": recordings, "clips": clips}


def _make_recording(recordings_folder, camera_number, start, minutes=5, content=b"x" * 64):
    folder = recordings_folder / f"camera{camera_number}"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{start.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = folder / name
    path.write_bytes(content)
    return path


def _fake_ffprobe_factory(call_log, duration_by_path):
    """A subprocess.run() replacement: records every invocation's target
    path, returns a canned duration for known files (default 300s), and
    never actually shells out to real ffprobe."""

    class _Result:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0

    def fake_run(cmd, **kwargs):
        path = cmd[-1]
        call_log.append(path)
        duration = duration_by_path.get(path, 300.0)
        return _Result(stdout=f"{duration:.6f}")

    return fake_run


# ---------------------------------------------------------------------------
# 1 & 2. 1,000 historical recordings -> only a handful of ffprobe calls,
#        and only the ones actually near the event timestamp.
# ---------------------------------------------------------------------------

def test_large_history_does_not_probe_every_file(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    base = datetime(2026, 1, 1, tzinfo=None)

    # 1,000 old, irrelevant 5-minute segments, days before the event.
    for i in range(1000):
        _make_recording(recordings, 1, base + timedelta(minutes=5 * i))

    # The one segment that actually covers the event, far in the future
    # relative to the 1,000 historical files above.
    event_time = base + timedelta(days=10)
    segment_start = event_time - timedelta(minutes=2)
    real_segment = _make_recording(recordings, 1, segment_start)

    call_log = []
    monkeypatch.setattr(
        main.subprocess, "run", _fake_ffprobe_factory(call_log, {})
    )

    result = asyncio.run(
        main.build_motion_event_clip("evt-large-history", 1, event_time, event_time)
    )

    assert len(call_log) < 10, (
        f"expected only a handful of ffprobe calls, got {len(call_log)} "
        f"-- the full 1,000-file history was probed"
    )
    assert str(real_segment) in call_log


def test_only_recordings_near_event_are_probed(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    event_time = datetime(2026, 6, 1, 12, 0, 0)

    near = _make_recording(recordings, 2, event_time - timedelta(minutes=1))
    far_before = _make_recording(recordings, 2, event_time - timedelta(hours=6))
    far_after = _make_recording(recordings, 2, event_time + timedelta(hours=6))

    call_log = []
    monkeypatch.setattr(
        main.subprocess, "run", _fake_ffprobe_factory(call_log, {})
    )

    asyncio.run(main.build_motion_event_clip("evt-near", 2, event_time, event_time))

    assert str(near) in call_log
    assert str(far_before) not in call_log
    assert str(far_after) not in call_log


# ---------------------------------------------------------------------------
# 3. An event spanning a recording-segment boundary still works.
# ---------------------------------------------------------------------------

def test_event_crossing_segment_boundary_uses_both_segments(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    boundary = datetime(2026, 3, 1, 10, 5, 0)  # segment 2 starts exactly here

    segment1_start = boundary - timedelta(minutes=5)
    segment1 = _make_recording(recordings, 3, segment1_start)
    segment2 = _make_recording(recordings, 3, boundary)

    # Event straddles the boundary: starts 10s before it, ends 10s after.
    event_start = boundary - timedelta(seconds=10)
    event_end = boundary + timedelta(seconds=10)

    call_log = []
    # segment1 genuinely runs right up to the boundary; segment2 is still
    # being actively written (duration N/A -> "recently modified" path).

    def fake_run(cmd, **kwargs):
        path = cmd[-1]
        call_log.append(path)

        class _Result:
            returncode = 0

        if path == str(segment1):
            _Result.stdout = "300.000000"
        else:
            _Result.stdout = "N/A"
        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    # segment2 must look "recently modified" for the N/A path to apply.
    now = time.time()
    os.utime(segment2, (now, now))

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        from pathlib import Path as _Path

        _Path(cmd[-1]).write_bytes(b"fake mp4 bytes")

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return _Proc()

    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(
        main.build_motion_event_clip("evt-boundary", 3, event_start, event_end)
    )

    assert str(segment1) in call_log
    assert str(segment2) in call_log
    assert result is not None


# ---------------------------------------------------------------------------
# 4. Remaining ffprobe work runs off the event loop.
# ---------------------------------------------------------------------------

def test_ffprobe_work_runs_off_event_loop_thread(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    event_time = datetime(2026, 4, 1, 8, 0, 0)
    _make_recording(recordings, 4, event_time - timedelta(minutes=1))

    caller_thread_id = threading.get_ident()
    seen_thread_ids = []

    real_probe = main._probe_motion_clip_candidates

    def spying_probe(shortlist, window_start, window_end):
        seen_thread_ids.append(threading.get_ident())
        return real_probe(shortlist, window_start, window_end)

    monkeypatch.setattr(main, "_probe_motion_clip_candidates", spying_probe)
    monkeypatch.setattr(
        main.subprocess, "run", _fake_ffprobe_factory([], {})
    )

    asyncio.run(
        main.build_motion_event_clip("evt-offloop", 4, event_time, event_time)
    )

    assert seen_thread_ids, "_probe_motion_clip_candidates() was never called"
    assert seen_thread_ids[0] != caller_thread_id, (
        "ffprobe candidate-probing ran on the calling thread -- the "
        "asyncio.to_thread() offload is missing or was bypassed, which "
        "reintroduces the event-loop stall."
    )


# ---------------------------------------------------------------------------
# 5. Existing clip/media behavior is unchanged for a simple case.
# ---------------------------------------------------------------------------

def test_existing_clip_output_and_schema_preserved(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    clips = _isolated_clip_paths["clips"]
    event_time = datetime(2026, 5, 1, 9, 0, 0)
    segment = _make_recording(recordings, 5, event_time - timedelta(minutes=1))

    monkeypatch.setattr(
        main.subprocess, "run", _fake_ffprobe_factory([], {})
    )

    captured_ffmpeg_cmd = {}

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured_ffmpeg_cmd["cmd"] = cmd
        # Capture the concat list file's content here -- build_motion_
        # event_clip()'s own `finally` block unlinks it right after this
        # call returns (pre-existing, unchanged behavior), so it won't
        # exist any more by the time this test function gets control back.
        list_file = clips / "motion" / f".{event_id}.txt"
        captured_ffmpeg_cmd["list_file_content"] = list_file.read_text(encoding="utf-8")
        # Real ffmpeg would have written the temp output file by the time
        # communicate() returns; build_motion_event_clip()'s own
        # temp_path.replace(output_path) requires that file to exist.
        from pathlib import Path as _Path

        _Path(cmd[-1]).write_bytes(b"fake mp4 bytes")

        class _Proc:
            returncode = 0
            stderr = None

            async def communicate(self):
                return b"", b""

        return _Proc()

    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    event_id = "evt-schema-preserved"
    result = asyncio.run(
        main.build_motion_event_clip(event_id, 5, event_time, event_time)
    )

    expected_output = clips / "motion" / f"motion_{event_id}.mp4"
    list_file = clips / "motion" / f".{event_id}.txt"
    # The list file itself is unlinked in build_motion_event_clip()'s own
    # `finally` block (pre-existing behavior, unchanged by this fix) --
    # what matters here is that it was written with the right content
    # before that cleanup, which the captured ffmpeg command's presence
    # and the final result together already prove indirectly. Assert on
    # the ffmpeg invocation and the final, real preserved behavior instead.
    assert "cmd" in captured_ffmpeg_cmd
    assert captured_ffmpeg_cmd["cmd"][0] == "ffmpeg"
    assert str(segment) in captured_ffmpeg_cmd["list_file_content"]
    assert result == f"/recordings/clips/motion/{expected_output.name}"
    assert expected_output.exists()
    assert expected_output.read_bytes() == b"fake mp4 bytes"
    assert not list_file.exists(), "concat list file should be cleaned up (pre-existing behavior)"


# ---------------------------------------------------------------------------
# 6. A heartbeat coroutine keeps ticking while a clip is being built.
# ---------------------------------------------------------------------------

def test_heartbeat_keeps_ticking_during_clip_build(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    event_time = datetime(2026, 7, 1, 6, 0, 0)
    _make_recording(recordings, 1, event_time - timedelta(minutes=1))

    def slow_fake_run(cmd, **kwargs):
        # Simulate a slow-but-real ffprobe: this blocks its OWN thread
        # (the to_thread worker), which is fine and expected -- the
        # point of this test is that it must NOT block the event loop.
        time.sleep(0.4)

        class _Result:
            stdout = "300.000000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", slow_fake_run)

    heartbeat_ticks = []

    async def heartbeat():
        for _ in range(40):
            heartbeat_ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def run_both():
        heartbeat_task = asyncio.create_task(heartbeat())
        await main.build_motion_event_clip(
            "evt-heartbeat", 1, event_time, event_time
        )
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    start = time.monotonic()
    asyncio.run(run_both())
    elapsed = time.monotonic() - start

    # The slow ffprobe call held its worker thread for ~0.4s. If the
    # event loop were blocked for that whole window (the original bug),
    # the heartbeat would tick close to zero times during it. Off the
    # loop, it should tick roughly once every 0.01s throughout.
    ticks_during_probe = sum(1 for t in heartbeat_ticks if t - start < elapsed)
    assert ticks_during_probe >= 10, (
        f"heartbeat only ticked {ticks_during_probe} times while the clip "
        f"was being built -- the event loop appears to have been blocked"
    )
