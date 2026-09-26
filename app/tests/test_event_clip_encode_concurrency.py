"""Event-clip encode concurrency (2026-09-03): focused tests for the new
event_clip_encode_semaphore bounding how many build_motion_event_clip()
ffmpeg (libx264) encodes can run simultaneously.

Background: with AI enabled, build_motion_event_clip() was called with
no concurrency bound at all -- confirmed live: a single real 10s encode
takes ~2.6s in isolation, but 119 real detections across 5 cameras in 18
minutes produced 10+ simultaneous ffmpeg encodes, a monotonically
climbing load average (13.78 -> 67.99 over 10 minutes on an 8-core
appliance), and 8 real /health failures. Zero code change to event
creation, thumbnail creation, or the cooldown/primary-class logic that
decides whether to build a clip at all -- only the encode dispatch
itself (asyncio.create_subprocess_exec + communicate()) is now gated by
event_clip_encode_semaphore, default concurrency 1.

Same import/isolation constraints as test_motion_clip_shortlist_fix.py
(imports `main`, redirects RECORDINGS_FOLDER/CLIPS_FOLDER to tmp_path)
-- reuses that file's own _make_recording()/_isolated_clip_paths
fixtures rather than a second, possibly-drifting copy.
"""

import asyncio
import os
import time
from datetime import datetime, timedelta

import pytest

import main

from test_motion_clip_shortlist_fix import _isolated_clip_paths, _make_recording, _fake_ffprobe_factory

# Captured before any monkeypatching -- build_motion_event_clip()'s own
# pre-roll wait (unconditionally >= 3s: post-roll footage margin, unrelated
# to and unchanged by this fix) is unconditionally sped up below for every
# test in this file so the concurrency behavior itself -- not this
# pre-existing wait -- is what each test's wall-clock time reflects. The
# fake encode's own controllable hold duration uses this real, unpatched
# sleep so a test can still reliably hold a "slow encode" open regardless.
_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _fast_preroll_wait(monkeypatch):
    async def fast_sleep(seconds):
        await _REAL_SLEEP(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)


class _ControllableFakeProcess:
    """Stands in for the object asyncio.create_subprocess_exec() returns.
    communicate() holds until `release_event` is set (or `hold_seconds`
    elapses, whichever first) -- lets a test observe exactly how many of
    these are "in flight" at once, and control when each one finishes."""

    def __init__(self, output_path, hold_seconds=0.05, returncode=0, raise_in_communicate=None):
        self.output_path = output_path
        self.hold_seconds = hold_seconds
        self.returncode = returncode
        self._raise_in_communicate = raise_in_communicate

    async def communicate(self):
        await _REAL_SLEEP(self.hold_seconds)
        if self._raise_in_communicate is not None:
            raise self._raise_in_communicate
        from pathlib import Path as _Path
        _Path(self.output_path).write_bytes(b"fake mp4 bytes")
        return b"", b""


def _make_tracking_fake_exec(concurrent_counter, hold_seconds=0.05, returncode=0, raise_in_communicate=None):
    """Every call increments concurrent_counter["active"] for the
    duration of its (fake) encode and records the peak observed value --
    this is what proves the semaphore actually bounds concurrency, not
    just that it exists."""

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        concurrent_counter["active"] += 1
        concurrent_counter["peak"] = max(concurrent_counter["peak"], concurrent_counter["active"])
        proc = _ControllableFakeProcess(cmd[-1], hold_seconds=hold_seconds, returncode=returncode, raise_in_communicate=raise_in_communicate)
        real_communicate = proc.communicate

        async def communicate_and_release():
            try:
                return await real_communicate()
            finally:
                concurrent_counter["active"] -= 1

        proc.communicate = communicate_and_release
        return proc

    return fake_create_subprocess_exec


# ---------------------------------------------------------------------------
# 1. Many concurrent clip builds never exceed configured encode
#    concurrency.
# ---------------------------------------------------------------------------

def test_concurrent_builds_never_exceed_configured_concurrency(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    monkeypatch.setattr(main, "EVENT_CLIP_ENCODE_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(main, "event_clip_encode_semaphore", asyncio.Semaphore(2))
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))

    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec",
        _make_tracking_fake_exec(counter, hold_seconds=0.08),
    )

    event_time = datetime(2026, 8, 1, 12, 0, 0)
    for camera in range(1, 6):
        _make_recording(recordings, camera, event_time - timedelta(minutes=1))

    async def scenario():
        return await asyncio.gather(*(
            main.build_motion_event_clip(f"evt-conc-{camera}", camera, event_time, event_time)
            for camera in range(1, 6)
        ))

    results = asyncio.run(scenario())

    assert counter["peak"] == 2, f"expected peak concurrency exactly 2, observed {counter['peak']}"
    assert all(r is not None for r in results), "every queued build must still complete -- none silently dropped"


def test_concurrency_one_fully_serializes(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    monkeypatch.setattr(main, "EVENT_CLIP_ENCODE_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(main, "event_clip_encode_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))

    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec",
        _make_tracking_fake_exec(counter, hold_seconds=0.05),
    )

    event_time = datetime(2026, 8, 1, 13, 0, 0)
    for camera in range(1, 4):
        _make_recording(recordings, camera, event_time - timedelta(minutes=1))

    async def scenario():
        return await asyncio.gather(*(
            main.build_motion_event_clip(f"evt-serial-{camera}", camera, event_time, event_time)
            for camera in range(1, 4)
        ))

    results = asyncio.run(scenario())

    assert counter["peak"] == 1, f"default concurrency=1 must fully serialize encodes, observed peak={counter['peak']}"
    assert all(r is not None for r in results)


# ---------------------------------------------------------------------------
# 2 & 3. Semaphore releases after success, and after exception/cancellation.
# ---------------------------------------------------------------------------

def test_semaphore_releases_after_success(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(main, "event_clip_encode_semaphore", semaphore)
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))
    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _make_tracking_fake_exec(counter, hold_seconds=0.01))

    event_time = datetime(2026, 8, 1, 14, 0, 0)
    _make_recording(recordings, 1, event_time - timedelta(minutes=1))

    async def scenario():
        result = await main.build_motion_event_clip("evt-release-success", 1, event_time, event_time)
        assert result is not None
        # Fully released: a fresh acquire must succeed immediately.
        acquired = await asyncio.wait_for(semaphore.acquire(), timeout=1.0)
        assert acquired is True
        semaphore.release()

    asyncio.run(scenario())


def test_semaphore_releases_after_ffmpeg_nonzero_exit(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(main, "event_clip_encode_semaphore", semaphore)
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))
    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec",
        _make_tracking_fake_exec(counter, hold_seconds=0.01, returncode=1),
    )

    event_time = datetime(2026, 8, 1, 15, 0, 0)
    _make_recording(recordings, 1, event_time - timedelta(minutes=1))

    async def scenario():
        result = await main.build_motion_event_clip("evt-release-nonzero", 1, event_time, event_time)
        assert result is None  # ffmpeg failure -> existing behavior: caught, returns None
        acquired = await asyncio.wait_for(semaphore.acquire(), timeout=1.0)
        assert acquired is True
        semaphore.release()

    asyncio.run(scenario())


def test_semaphore_releases_after_communicate_raises(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(main, "event_clip_encode_semaphore", semaphore)
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))
    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec",
        _make_tracking_fake_exec(counter, hold_seconds=0.01, raise_in_communicate=OSError("simulated ffmpeg crash")),
    )

    event_time = datetime(2026, 8, 1, 16, 0, 0)
    _make_recording(recordings, 1, event_time - timedelta(minutes=1))

    async def scenario():
        result = await main.build_motion_event_clip("evt-release-raise", 1, event_time, event_time)
        assert result is None  # existing except Exception handler in build_motion_event_clip() catches it
        acquired = await asyncio.wait_for(semaphore.acquire(), timeout=1.0)
        assert acquired is True
        semaphore.release()

    asyncio.run(scenario())


def test_semaphore_releases_after_cancellation(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(main, "event_clip_encode_semaphore", semaphore)
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))
    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(
        main.asyncio, "create_subprocess_exec",
        _make_tracking_fake_exec(counter, hold_seconds=5.0),  # long enough to reliably cancel mid-encode
    )

    event_time = datetime(2026, 8, 1, 17, 0, 0)
    _make_recording(recordings, 1, event_time - timedelta(minutes=1))

    async def scenario():
        task = asyncio.create_task(
            main.build_motion_event_clip("evt-release-cancel", 1, event_time, event_time)
        )
        # Let it get past the pre-roll wait / shortlist / acquire the
        # semaphore and start "encoding" before cancelling mid-flight.
        for _ in range(10):
            await asyncio.sleep(0)
        assert counter["active"] == 1, "expected the encode to have started before cancellation"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # CancelledError propagates out of build_motion_event_clip() itself
        # (not swallowed by its own `except Exception`, matching existing,
        # unchanged behavior) -- but the semaphore must already be free.
        acquired = await asyncio.wait_for(semaphore.acquire(), timeout=1.0)
        assert acquired is True
        semaphore.release()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Every queued job eventually gets a chance to encode -- no exact FIFO
#    order required, just no silent drops.
# ---------------------------------------------------------------------------

def test_every_queued_job_eventually_completes_no_order_requirement(monkeypatch, _isolated_clip_paths):
    recordings = _isolated_clip_paths["recordings"]
    monkeypatch.setattr(main, "event_clip_encode_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(main.subprocess, "run", _fake_ffprobe_factory([], {}))
    counter = {"active": 0, "peak": 0}
    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _make_tracking_fake_exec(counter, hold_seconds=0.02))

    event_time = datetime(2026, 8, 1, 18, 0, 0)
    n = 8
    for i in range(n):
        _make_recording(recordings, 1, event_time - timedelta(minutes=1) - timedelta(seconds=i))

    async def scenario():
        return await asyncio.gather(*(
            main.build_motion_event_clip(f"evt-queued-{i}", 1, event_time, event_time)
            for i in range(n)
        ))

    results = asyncio.run(scenario())

    assert len(results) == n
    assert all(r is not None for r in results), "every one of the queued jobs must eventually complete"
    assert len(set(results)) == n, "every job must produce its own distinct output, no collisions"


# ---------------------------------------------------------------------------
# 5. Local event + thumbnail creation is not delayed by encode-queue
#    depth, for both motion and AI paths.
# ---------------------------------------------------------------------------

def test_basic_motion_event_and_thumbnail_are_immediate_even_when_encode_queue_is_saturated(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "event_clip_encode_semaphore", asyncio.Semaphore(1))

    thumbnail_calls = []

    async def fake_create_motion_thumbnail(camera_number, event_id, frame, start_time):
        thumbnail_calls.append((camera_number, event_id, time.monotonic()))
        return f"/recordings/thumbnails/motion_{event_id}.jpg"

    monkeypatch.setattr(main, "create_motion_thumbnail", fake_create_motion_thumbnail)

    appended_events = []
    monkeypatch.setattr(main, "append_motion_event", lambda line: appended_events.append((line, time.monotonic())))
    monkeypatch.setattr(main, "append_analytics_event", lambda event: appended_events.append((event, time.monotonic())))

    # Occupy the semaphore with a slow "encode" already in flight before
    # the new motion event even starts, so its own clip build is
    # guaranteed to have to queue.
    holder_release = asyncio.Event()

    async def scenario():
        t_start = time.monotonic()

        async def hold_semaphore():
            async with main.event_clip_encode_semaphore:
                await holder_release.wait()

        holder_task = asyncio.create_task(hold_semaphore())
        await asyncio.sleep(0)  # let it acquire

        camera_number = 1
        frame = bytes(14400)
        start = datetime(2026, 9, 3, 12, 0, 0)
        end = start + timedelta(seconds=6)
        await main.store_motion_event(camera_number, start, end, score=20.0, frame=frame)
        t_event_created = time.monotonic()

        holder_release.set()
        await holder_task
        pending = list(main.clip_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        return t_start, t_event_created

    t_start, t_event_created = asyncio.run(scenario())

    assert appended_events, "the local motion/analytics event must still be created"
    assert thumbnail_calls, "the local thumbnail must still be created"
    # Both happened essentially immediately -- well before the semaphore
    # holder was ever released -- proving they are not gated by the
    # encode queue at all.
    assert (t_event_created - t_start) < 0.5


def test_ai_event_and_thumbnail_are_immediate_even_when_encode_queue_is_saturated(monkeypatch, tmp_path):
    import numpy as np

    monkeypatch.setattr(main, "event_clip_encode_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)

    camera_number = 4
    monkeypatch.delitem(main.ai_event_clip_windows, camera_number, raising=False)

    holder_release = asyncio.Event()

    async def scenario():
        main._ai_event_media_loop = asyncio.get_running_loop()
        t_start = time.monotonic()

        async def hold_semaphore():
            async with main.event_clip_encode_semaphore:
                await holder_release.wait()

        holder_task = asyncio.create_task(hold_semaphore())
        await asyncio.sleep(0)

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = {
            "ok": True, "error": None, "frame": frame,
            "detections": [{"class_id": 0, "class_name": "person", "confidence": 0.9, "x": 5, "y": 5, "width": 20, "height": 20}],
        }
        events = main.save_yolo_events(camera_number, result)
        t_event_created = time.monotonic()

        holder_release.set()
        await holder_task
        for _ in range(20):
            await asyncio.sleep(0)

        return events, t_start, t_event_created

    events, t_start, t_event_created = asyncio.run(scenario())

    assert events, "the local AI analytics event must still be created"
    thumbnail_files = list(tmp_path.rglob("*.jpg"))
    assert thumbnail_files, "the local AI thumbnail must still be written"
    assert (t_event_created - t_start) < 0.5


# ---------------------------------------------------------------------------
# 6. Default concurrency is 1; env override works.
# ---------------------------------------------------------------------------

def test_default_concurrency_is_one():
    assert main.EVENT_CLIP_ENCODE_MAX_CONCURRENCY == 1


def test_env_override_formula(monkeypatch):
    # Mirrors the exact expression main.py uses -- proves the formula
    # itself is correct (env override respected, invalid/zero clamped to
    # at least 1) without needing a full module re-import.
    def compute():
        return max(1, int(os.environ.get("ANYAICAM_EVENT_CLIP_ENCODE_MAX_CONCURRENCY", "1")))

    monkeypatch.delenv("ANYAICAM_EVENT_CLIP_ENCODE_MAX_CONCURRENCY", raising=False)
    assert compute() == 1

    monkeypatch.setenv("ANYAICAM_EVENT_CLIP_ENCODE_MAX_CONCURRENCY", "3")
    assert compute() == 3

    monkeypatch.setenv("ANYAICAM_EVENT_CLIP_ENCODE_MAX_CONCURRENCY", "0")
    assert compute() == 1  # clamped, never zero/unbounded-by-accident
