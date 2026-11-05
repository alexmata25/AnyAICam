"""Windows 1.3.4.1 Sandbox (signed e4c7bdc, 2026-10-30): with the buffer muxer flushing every packet, a 19.1 s event
still came out 13.9 s -- the Matroska muxer writes a cluster only when it closes at a keyframe, so the open buffer
segment trails real time by up to one GOP. persist_event_recording() now rebuilds the clip until it covers the event
window (bounded), retries a concat that fails on a not-yet-readable open segment, and still reports a clip that
stays short (duration_mismatch) instead of passing it silently."""
import asyncio
import logging
import subprocess
import time
from datetime import datetime, timedelta

import pytest

import main

T0 = datetime(2026, 10, 30, 10, 5, 30)


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    buffer_folder = recordings / "camera1" / main.EVENT_BUFFER_SUBFOLDER_NAME
    buffer_folder.mkdir(parents=True)
    (buffer_folder / f"buf1_{T0:%Y-%m-%d_%H-%M-%S}.mkv").write_bytes(b"segment")
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {
        "pre_roll_seconds": 5, "post_roll_seconds": 5, "merge_gap_seconds": 10, "max_event_seconds": 300})
    monkeypatch.setattr(main, "_keyframe_times", lambda path: [0.0])

    async def no_wait(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", no_wait)
    state = {"calls": 0, "returncodes": [], "durations": [], "timeouts": [], "probe_timeouts": [], "hang": None}

    class Completed:
        def __init__(self, returncode):
            self.returncode, self.stdout, self.stderr = returncode, b"", b""

    def fake_run(command, **kwargs):
        # Each build writes its own content (clip-1, clip-2, ...) so a test can tell which build was kept. A failed
        # build leaves a partial file, as ffmpeg -y does after truncating its output.
        state["calls"] += 1
        state["timeouts"].append(kwargs.get("timeout"))
        if state["hang"] is not None and state["calls"] >= state["hang"]:
            time.sleep(kwargs["timeout"])          # what subprocess.run(timeout=...) does before killing ffmpeg
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        returncode = state["returncodes"].pop(0) if state["returncodes"] else 0
        open(command[-1], "wb").write(f"clip-{state['calls']}".encode() if returncode == 0 else b"partial")
        return Completed(returncode)

    def fake_probe(path):
        return state["durations"].pop(0) if state["durations"] else state["last"]

    def fake_attempt_probe(path, timeout):
        state["probe_timeouts"].append(timeout)
        return fake_probe(path)

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_probe_recording_duration_seconds", fake_probe)
    # raising=False: the same tests also run against the uncorrected 01a493c code to show they fail there.
    monkeypatch.setattr(main, "_probe_event_clip_attempt_seconds", fake_attempt_probe, raising=False)
    main._open_event_recordings.pop(1, None)
    main._event_recording_locks.pop(1, None)
    yield state
    main._open_event_recordings.pop(1, None)


def _persist():
    # event 10:05:40 -> 10:05:45 with 5 s pre/post roll: the window is 10:05:35 -> 10:05:50; the only keyframe is
    # the segment start 10:05:30, so the clip starts there and must cover 20 s.
    asyncio.run(main.persist_event_recording(1, T0 + timedelta(seconds=10), T0 + timedelta(seconds=15),
                                             detector="basic_motion", trigger_id="t1"))


def test_a_short_build_is_rebuilt_until_the_clip_covers_the_window(harness, caplog):
    harness["durations"] = [14.4, 20.0, 20.0]    # first build trails by a GOP, the second covers the window
    harness["last"] = 20.0
    with caplog.at_level(logging.INFO):
        _persist()
    assert harness["calls"] == 2
    assert "event_recording.persisted" in caplog.text and "duration_mismatch" not in caplog.text


def test_a_concat_that_fails_on_the_open_segment_is_retried(harness, caplog):
    harness["returncodes"] = [3199971767, 0]     # "Invalid data" while the open segment is not yet readable
    harness["durations"] = [20.0, 20.0]
    harness["last"] = 20.0
    with caplog.at_level(logging.INFO):
        _persist()
    assert harness["calls"] == 2
    assert "event_recording.persisted" in caplog.text and "concat_failed" not in caplog.text


def test_a_clip_that_stays_short_is_kept_and_reported_never_passed_silently(harness, caplog, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.0)   # no time left: one build only
    harness["durations"] = [9.8]
    harness["last"] = 9.8
    with caplog.at_level(logging.INFO):
        _persist()
    assert harness["calls"] == 1
    assert "event_recording.persisted" in caplog.text and "event_recording.duration_mismatch" in caplog.text


# 2026-11-05, Codex FAIL on 01a493c: (1) the 15 s retry deadline was checked only between builds, so a rebuild started
# just before it still got ffmpeg's 60 s timeout and an unbounded duration probe while holding the per-camera lock;
# (2) every build wrote the same temp file, so a later failed rebuild destroyed the best clip built so far.

def _camera_clips(harness):
    return sorted(path.name for path in (main.RECORDINGS_FOLDER / "camera1").glob("*.mkv"))


def _kept_clip():
    (clip,) = (main.RECORDINGS_FOLDER / "camera1").glob("camera1_*.mkv")
    return clip.read_bytes()


def test_a_rebuild_that_hangs_is_killed_at_the_deadline_and_the_lock_is_released(harness, caplog, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.5)
    harness["durations"] = [14.4]                # first build short: one rebuild is due
    harness["last"] = 14.4
    harness["hang"] = 2                          # ...and that rebuild's ffmpeg never finishes
    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        _persist()
    elapsed = time.monotonic() - started
    assert harness["timeouts"][0] == 60.0        # the first build keeps the single build's own bound
    assert 0 < harness["timeouts"][1] <= 0.5     # a rebuild gets only the time left before the deadline
    assert elapsed < 0.5 + 1.0
    assert not main._event_recording_locks[1].locked()
    assert _kept_clip() == b"clip-1"             # the killed rebuild did not cost the first clip
    assert "event_recording.persisted" in caplog.text and "duration_mismatch" in caplog.text


def test_every_duration_probe_gets_only_the_time_left_before_the_deadline(harness, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    harness["durations"] = []
    harness["last"] = 14.4                       # every build short: rebuilds until the deadline
    started = time.monotonic()
    _persist()
    assert time.monotonic() - started < 0.3 + 1.0
    assert harness["calls"] >= 2
    assert all(timeout <= 0.3 for timeout in harness["probe_timeouts"])
    assert all(timeout <= 0.3 for timeout in harness["timeouts"][1:])


def test_the_attempt_probe_gives_up_when_the_shared_ffprobe_slots_stay_busy(tmp_path, monkeypatch):
    # The real bounded probe, with every appliance-wide ffprobe slot held by someone else: it must return within its
    # timeout instead of waiting for a slot while the caller holds the per-camera lock.
    launched = []
    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: launched.append(args))
    held = 0
    try:
        while main._ffprobe_semaphore.acquire(blocking=False):
            held += 1
        started = time.monotonic()
        assert main._probe_event_clip_attempt_seconds(tmp_path / "clip.mkv", 0.3) is None
        assert time.monotonic() - started < 0.3 + 0.5
    finally:
        for _ in range(held):
            main._ffprobe_semaphore.release()
    assert held >= 1 and launched == []
    assert main._probe_event_clip_attempt_seconds(tmp_path / "clip.mkv", 0.0) is None


def test_a_later_failed_rebuild_never_destroys_the_earlier_clip(harness, caplog, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    harness["returncodes"] = [0] + [3199971767] * 100000   # first build short but valid, every rebuild fails
    harness["durations"] = [14.4]
    harness["last"] = 14.4
    with caplog.at_level(logging.INFO):
        _persist()
    assert harness["calls"] >= 2
    assert _kept_clip() == b"clip-1"
    assert _camera_clips(harness) == [_camera_clips(harness)[0]]   # no .tmp/.attempt file left behind
    assert "event_recording.persisted" in caplog.text and "concat_failed" not in caplog.text
    assert "duration_mismatch" in caplog.text


def test_a_later_shorter_rebuild_does_not_replace_a_longer_clip(harness, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    harness["durations"] = [14.4, 9.0]           # the rebuild comes out shorter; later ones too
    harness["last"] = 9.0
    _persist()
    assert harness["calls"] >= 2
    assert _kept_clip() == b"clip-1"


def test_a_longer_rebuild_replaces_a_shorter_clip_even_when_still_short(harness, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    harness["returncodes"] = [0, 0] + [3199971767] * 100000
    harness["durations"] = [9.0, 14.4]
    harness["last"] = 14.4
    _persist()
    assert _kept_clip() == b"clip-2"


def test_with_no_valid_build_the_event_is_still_reported_concat_failed(harness, caplog, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    harness["returncodes"] = [3199971767] * 100000
    with caplog.at_level(logging.INFO):
        _persist()
    assert "event_recording.concat_failed" in caplog.text and "event_recording.persisted" not in caplog.text
    assert _camera_clips(harness) == []


def test_an_unmeasurable_rebuild_does_not_replace_a_measured_clip(harness, monkeypatch):
    # e.g. the rebuild's probe ran out of time: nothing shows that clip is better, so the measured one stays.
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    harness["durations"] = [14.4, None]
    harness["last"] = 14.4
    _persist()
    assert harness["calls"] == 2
    assert _kept_clip() == b"clip-1"


def test_a_temp_file_left_by_an_interrupted_run_is_never_promoted(harness, monkeypatch):
    monkeypatch.setattr(main, "EVENT_BUFFER_COVERAGE_WAIT_SECONDS", 0.3)
    stale = main.RECORDINGS_FOLDER / "camera1" / f"camera1_{T0:%Y-%m-%d_%H-%M-%S}.tmp.mkv"
    stale.write_bytes(b"stale")
    harness["returncodes"] = [3199971767] * 100000
    _persist()
    assert not stale.exists() and _camera_clips(harness) == []
