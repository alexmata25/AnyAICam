"""Regression coverage for a real, second contributor to the anyaicam-vms
restart loop found live on Ryzen (2026-09-22), after the camera-supervisor
startup stagger fix (see test_camera_supervisor_startup_stagger.py) closed
the FIRST trigger.

This appliance's baseline CPU already sits close to its 8-core ceiling
(live-HLS transcoding alone measured at ~407% of that, more than half the
box's total capacity). Two separate Docker healthcheck-timeout episodes
each correlated with a burst of 2-3 concurrent ffprobe processes landing
on top of that already-saturated baseline. _probe_motion_clip_candidates()
(called from build_motion_event_clip(), itself reachable from both the
basic-motion and every AI-detection event path) had no bound at all on how
many real ffprobe subprocesses could run at once -- unlike the ffmpeg
ENCODE step in the same file (event_clip_encode_semaphore), which was
already bounded after an earlier, similar incident.

Two changes, both scoped to _probe_motion_clip_candidates():
  1. FFPROBE_MAX_CONCURRENCY / _ffprobe_semaphore -- a plain
     threading.Semaphore (this function always runs inside a real OS
     worker thread via asyncio.to_thread(), never on the event loop
     itself) bounding real ffprobe subprocess launches appliance-wide,
     mirroring event_clip_encode_semaphore's own already-proven shape.
  2. _ffprobe_duration_cache, keyed by (path, mtime) -- a burst of
     near-simultaneous events across different cameras (or the motion
     vs. AI-detection paths on the same camera) can each independently
     want to probe the SAME already-completed source file; every probe
     after the first is now a dict lookup, not a second real subprocess.
     Only a real, final duration is ever cached -- the N/A ("still being
     actively written") case is deliberately never cached, matching the
     existing semantics for an in-progress file exactly.
"""
import subprocess
import threading
import time

import main


def _make_recording(tmp_path, name="camera1_2026-09-22_00-00-00.mkv", content=b"x" * 64):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_default_concurrency_matches_the_existing_encode_semaphore_convention():
    """1 is the same conservative default event_clip_encode_semaphore
    already established for the analogous ffmpeg-encode bound."""
    assert main.FFPROBE_MAX_CONCURRENCY == 1


def test_concurrent_probes_are_serialized_by_the_semaphore(monkeypatch, tmp_path):
    """The real bug this closes: without the semaphore, N threads calling
    _probe_motion_clip_candidates() at once would each launch a real
    ffprobe subprocess simultaneously. Proves the observed-concurrent
    count never exceeds FFPROBE_MAX_CONCURRENCY, using a fake
    subprocess.run() that sleeps just long enough to make a real race
    observable."""
    monkeypatch.setattr(main, "FFPROBE_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_ffprobe_semaphore", threading.Semaphore(1))
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})

    lock = threading.Lock()
    state = {"current": 0, "max_seen": 0}

    def fake_run(cmd, **kwargs):
        with lock:
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1

        class _Result:
            stdout = "300.000000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    now = main.datetime(2026, 9, 22, 0, 5, 0)
    threads = []
    for i in range(5):
        # A DISTINCT file per thread -- proves the semaphore itself is
        # what serializes execution, not the (path, mtime) cache below
        # coincidentally deduplicating identical probe targets.
        path = _make_recording(tmp_path, name=f"camera1_2026-09-22_00-0{i}-00.mkv")
        shortlist = [(now, path)]
        thread = threading.Thread(
            target=main._probe_motion_clip_candidates,
            args=(shortlist, now, now),
        )
        threads.append(thread)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert state["max_seen"] == 1, (
        f"expected at most 1 concurrent ffprobe subprocess, observed {state['max_seen']} at once"
    )


def test_a_file_already_probed_is_served_from_cache_not_reprobed(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})
    path = _make_recording(tmp_path)
    now = main.datetime(2026, 9, 22, 0, 5, 0)
    shortlist = [(now, path)]

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(cmd[-1])

        class _Result:
            stdout = "300.000000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    first = main._probe_motion_clip_candidates(shortlist, now, now)
    second = main._probe_motion_clip_candidates(shortlist, now, now)

    assert len(call_log) == 1, (
        f"expected exactly one real ffprobe subprocess call across two probes of the "
        f"same (path, mtime), got {len(call_log)}"
    )
    assert first == second


def test_a_rewritten_file_with_a_changed_mtime_is_reprobed_not_stale(monkeypatch, tmp_path):
    """Correctness guard, not an expected real-world case in this
    pipeline: a file whose mtime changes must never return a cached
    duration for its OLD content."""
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})
    path = _make_recording(tmp_path)
    now = main.datetime(2026, 9, 22, 0, 5, 0)
    shortlist = [(now, path)]

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(cmd[-1])

        class _Result:
            stdout = "300.000000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main._probe_motion_clip_candidates(shortlist, now, now)

    # Simulate a rewrite: bump mtime forward.
    new_time = path.stat().st_mtime + 5
    import os
    os.utime(path, (new_time, new_time))

    main._probe_motion_clip_candidates(shortlist, now, now)

    assert len(call_log) == 2, "a changed mtime must force a fresh real probe, not reuse the stale cache entry"


def test_na_duration_from_an_in_progress_file_is_never_cached(monkeypatch, tmp_path):
    """An actively-written file's N/A duration must never be cached --
    unlike a completed file's real duration, it can only get more
    accurate on a later probe, exactly matching pre-existing behavior."""
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})
    path = _make_recording(tmp_path)
    now = main.datetime(2026, 9, 22, 0, 5, 0)
    shortlist = [(now, path)]

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(cmd[-1])

        class _Result:
            stdout = "N/A"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    # Keep the file looking "recently modified" so the N/A path is taken
    # (not skipped as stale) on both probes.
    fresh_time = time.time()
    import os
    os.utime(path, (fresh_time, fresh_time))

    main._probe_motion_clip_candidates(shortlist, now, now)
    main._probe_motion_clip_candidates(shortlist, now, now)

    assert len(call_log) == 2, "an in-progress (N/A) file must be re-probed every time, never cached"


def test_full_build_motion_event_clip_still_works_with_the_semaphore_and_cache(monkeypatch, tmp_path):
    """End-to-end sanity: the fix must not change build_motion_event_clip()'s
    own observable output for the ordinary single-segment case."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    monkeypatch.setattr(main, "CLIPS_FOLDER", clips)
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})

    event_time = main.datetime(2026, 2, 1, 6, 0, 0)
    folder = recordings / "camera1"
    folder.mkdir(parents=True, exist_ok=True)
    segment = folder / "camera1_2026-02-01_05-59-00.mkv"
    segment.write_bytes(b"x" * 64)

    def fake_run(cmd, **kwargs):
        class _Result:
            stdout = "300.000000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        from pathlib import Path as _Path

        _Path(cmd[-1]).write_bytes(b"fake mp4 bytes")

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return _Proc()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    import asyncio as real_asyncio

    result = real_asyncio.run(
        main.build_motion_event_clip("evt-ffprobe-fix-sanity", 1, event_time, event_time)
    )

    assert result == "/recordings/clips/motion/motion_evt-ffprobe-fix-sanity.mp4"
