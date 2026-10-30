"""Windows 1.3.4.1 Sandbox (signed e4c7bdc, 2026-10-30): with the buffer muxer flushing every packet, a 19.1 s event
still came out 13.9 s -- the Matroska muxer writes a cluster only when it closes at a keyframe, so the open buffer
segment trails real time by up to one GOP. persist_event_recording() now rebuilds the clip until it covers the event
window (bounded), retries a concat that fails on a not-yet-readable open segment, and still reports a clip that
stays short (duration_mismatch) instead of passing it silently."""
import asyncio
import logging
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
    state = {"calls": 0, "returncodes": [], "durations": []}

    class Completed:
        def __init__(self, returncode):
            self.returncode, self.stdout, self.stderr = returncode, b"", b""

    def fake_run(command, **kwargs):
        state["calls"] += 1
        returncode = state["returncodes"].pop(0) if state["returncodes"] else 0
        if returncode == 0:
            open(command[-1], "wb").write(b"clip")
        return Completed(returncode)

    def fake_probe(path):
        return state["durations"].pop(0) if state["durations"] else state["last"]

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_probe_recording_duration_seconds", fake_probe)
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
