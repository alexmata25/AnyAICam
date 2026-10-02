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

Second occurrence, found live minutes after deploying the fix above:
_probe_recording_duration_seconds() (used by _catalog_local_recordings_
for_camera(), reachable synchronously from the customer-facing GET
/api/customer/recordings/{camera_id} route) is a SEPARATE ffprobe call
site -- confirmed live on Ryzen, 4 concurrent ffprobe processes were
observed with FFPROBE_MAX_CONCURRENCY already deployed and correctly
bounding _probe_motion_clip_candidates() alone. A Playback/Events
request landing while several cameras each have a small backlog of
undiscovered local files can launch one real ffprobe subprocess per
file with no bound of its own. Fixed by reusing the SAME appliance-wide
_ffprobe_semaphore/_ffprobe_duration_cache this function already
established, rather than a second, independent bound -- there is only
one real appliance-wide ffprobe-concurrency budget to protect.
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


# --------------------------- _probe_recording_duration_seconds() (second call site)


def test_recording_duration_probe_shares_the_same_semaphore(monkeypatch, tmp_path):
    """The real second occurrence: this is a SEPARATE ffprobe call site
    from _probe_motion_clip_candidates()'s own, reachable independently
    from the customer-facing recordings-catalog path. It must be bound
    by the SAME appliance-wide semaphore, not left unprotected."""
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
            stdout = "18.500000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    paths = [_make_recording(tmp_path, name=f"camera1_2026-09-22_00-1{i}-00.mkv") for i in range(5)]
    threads = [
        threading.Thread(target=main._probe_recording_duration_seconds, args=(path,))
        for path in paths
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert state["max_seen"] == 1, (
        f"expected at most 1 concurrent ffprobe subprocess from the recording-catalog "
        f"probe path, observed {state['max_seen']} at once"
    )


def test_recording_duration_probe_and_motion_clip_probe_share_one_budget_not_two(monkeypatch, tmp_path):
    """The real bug this whole fix closes: two DIFFERENT call sites
    (the catalog path and the motion-clip-candidate path) must draw
    from the SAME concurrency budget, not each get their own -- 4
    concurrent ffprobe processes were observed live even with the
    first fix alone deployed, precisely because this second call site
    had an independent, unbounded path."""
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

    now = main.datetime(2026, 9, 22, 0, 20, 0)
    catalog_path = _make_recording(tmp_path, name="camera1_2026-09-22_00-20-00.mkv")
    clip_path = _make_recording(tmp_path, name="camera1_2026-09-22_00-21-00.mkv")

    threads = [
        threading.Thread(target=main._probe_recording_duration_seconds, args=(catalog_path,)),
        threading.Thread(
            target=main._probe_motion_clip_candidates,
            args=([(now, clip_path)], now, now),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert state["max_seen"] == 1, (
        f"the catalog probe and the motion-clip probe must share ONE concurrency budget, "
        f"observed {state['max_seen']} concurrent ffprobe subprocesses across both call sites"
    )


def test_recording_duration_probe_uses_the_shared_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})
    path = _make_recording(tmp_path)

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(cmd[-1])

        class _Result:
            stdout = "18.500000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    first = main._probe_recording_duration_seconds(path)
    second = main._probe_recording_duration_seconds(path)

    assert len(call_log) == 1, "a second probe of the same (path, mtime) must be served from cache"
    assert first == second == 18.5


def test_recording_duration_probe_populates_the_cache_for_a_later_motion_clip_probe(monkeypatch, tmp_path):
    """Cross-call-site cache reuse: a file already cataloged (probed via
    the recordings route) must not be re-probed a second time just
    because a motion event later wants to consider it as a clip
    candidate for the SAME still-unmodified file."""
    monkeypatch.setattr(main, "_ffprobe_duration_cache", {})
    now = main.datetime(2026, 9, 22, 0, 25, 0)
    path = _make_recording(tmp_path, name="camera1_2026-09-22_00-25-00.mkv")

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(cmd[-1])

        class _Result:
            stdout = "300.000000"
            returncode = 0

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main._probe_recording_duration_seconds(path)
    main._probe_motion_clip_candidates([(now, path)], now, now)

    assert len(call_log) == 1, "the motion-clip probe must reuse the catalog probe's already-cached duration"
