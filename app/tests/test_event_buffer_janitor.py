"""Regression coverage for the confirmed-live Ryzen bug (2026-09-20):
event_buffer_janitor() called recording_start() -- which expects the
REAL recording filename convention (camera{N}_YYYY-MM-DD_HH-MM-SS.mkv)
-- directly against Event-mode buffer segments, whose actual filename
convention (start_event_recording_buffer()'s own output_pattern) is
buf{N}_YYYY-MM-DD_HH-MM-SS.mkv instead. str.removeprefix() is a no-op
when the prefix doesn't match, so the resulting strptime() call always
raised ValueError, recording_start() always returned None for every
buffer segment, and the janitor's own `if segment_start is None:
continue` treated every single segment as unparseable -- never deleting
anything. A real Driveway Right pilot camera accumulated 2,800 buffer
segments (~33GB) over 24+ hours of continuous idle-footage retention as
a direct, confirmed-live result -- exactly the disk-accumulation
failure mode this whole Local Event-mode feature exists to prevent.

Fixed with a dedicated _buffer_segment_start() using the correct
buf{N}_ prefix, used by event_buffer_janitor() instead of
recording_start().
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


def test_buffer_segment_start_parses_the_real_buffer_filename_convention():
    path = Path("/app/recordings/camera2/_event_buffer/buf2_2026-09-19_22-58-24.mkv")
    result = main._buffer_segment_start(path, camera_number=2)
    assert result == datetime(2026, 9, 19, 22, 58, 24)


def test_buffer_segment_start_is_scoped_to_the_right_camera_number():
    path = Path("/app/recordings/camera2/_event_buffer/buf2_2026-09-19_22-58-24.mkv")
    # A different camera's prefix must not accidentally match.
    assert main._buffer_segment_start(path, camera_number=3) is None


def test_recording_start_never_parses_a_buffer_segment_filename():
    """The exact confirmed-live bug: recording_start() (built for the
    real camera{N}_ recording convention) always returns None for a
    buf{N}_-prefixed buffer segment -- proving why event_buffer_janitor()
    calling it directly could never delete anything, regardless of how
    old a segment actually was."""
    path = Path("/app/recordings/camera2/_event_buffer/buf2_2026-09-19_22-58-24.mkv")
    assert main.recording_start(path, camera_number=2) is None


def test_janitor_uses_the_dedicated_buffer_parser_not_recording_start():
    """Guards against a future refactor silently reintroducing the bug
    by swapping the call back to recording_start()."""
    import inspect
    source = inspect.getsource(main.event_buffer_janitor)
    assert "_buffer_segment_start(" in source
    assert "recording_start(" not in source


def test_persist_event_recording_also_uses_the_dedicated_buffer_parser():
    """The second, more severe occurrence of the exact same bug,
    confirmed live on Ryzen (2026-09-20): persist_event_recording() --
    the function that actually builds the customer-facing event clip --
    ALSO called recording_start() (the camera{N}_ parser) directly
    against buffer segments (buf{N}_) when selecting which segments to
    concatenate. Since recording_start() always returned None for a
    buf{N}_ filename, `sources` was always empty and the function
    silently no-op'd via its own `if not sources: return` -- on every
    single real motion event, for the entire pilot, despite motion
    being correctly detected multiple times on Camera 2. This is a
    strictly worse instance of the same root cause already fixed in
    event_buffer_janitor() above, missed in that same pass because it's
    a second, independent call site."""
    import inspect
    source = inspect.getsource(main.persist_event_recording)
    assert "_buffer_segment_start(" in source
    assert "recording_start(" not in source


# --------------------------------------------------------------- post-roll wait (2026-09-21)
#
# Confirmed live on Ryzen, after the fix above: a real motion event on
# Camera 2 was correctly detected, but persist_event_recording() still
# produced nothing -- no exception, no log, the function's own designed-
# quiet "nothing to do yet" shape. Root cause: it is scheduled via
# asyncio.create_task() the moment a detection fires, with nothing
# waiting for window.end (event_end + post_roll_seconds, which extends
# into the FUTURE relative to that moment) to actually elapse in wall-
# clock time first -- the post-roll buffer segment genuinely didn't
# exist on disk yet. By the time anything else ran, the janitor had
# already aged the relevant pre-roll segments out, permanently losing
# the event. build_motion_event_clip() (used by every camera regardless
# of mode) already has this exact wait, for this exact reason -- this
# was the one place that pattern was never mirrored.


def test_persist_event_recording_waits_for_the_full_window_to_elapse_first(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta

    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    now = datetime.now()
    main._open_event_recordings.pop(1, None)  # test isolation: module-level state
    asyncio.run(main.persist_event_recording(1, now, now))

    assert len(sleep_calls) == 1
    # post_roll_seconds defaults to 5 (event_clips.DEFAULT_POST_ROLL_
    # SECONDS) plus the same +3.0 safety margin build_motion_event_clip()
    # already uses -- at least ~8s, never skipped entirely.
    assert sleep_calls[0] >= 3.0


def test_persist_event_recording_still_creates_the_clip_after_waiting(tmp_path, monkeypatch):
    """Proves the wait doesn't break the success path: with the (mocked,
    instant) wait satisfied and the needed buffer segments already on
    disk, a real concatenated clip is still produced."""
    import asyncio
    from datetime import datetime, timedelta

    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    now = datetime.now()
    buffer_folder = tmp_path / "recordings" / "camera1" / main.EVENT_BUFFER_SUBFOLDER_NAME
    buffer_folder.mkdir(parents=True, exist_ok=True)
    # Covers pre-roll through post-roll for an event_start==event_end==now
    # with default 5s pre/post-roll -- a single 30s segment starting
    # 15s before now comfortably spans the whole window.
    segment_start = now - timedelta(seconds=15)
    segment_path = buffer_folder / f"buf1_{segment_start:%Y-%m-%d_%H-%M-%S}.mkv"
    segment_path.write_bytes(b"fake video bytes")

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            # Stand-in for the post-success duration probe (also added
            # 2026-09-21, for the "event_recording.persisted" log line).
            return type("Result", (), {"returncode": 0, "stdout": "1.0", "stderr": ""})()
        # Stand-in for the real ffmpeg concat subprocess -- writes the
        # temp output (the command's last argument) the real call would
        # produce, so .replace() below has something to promote.
        Path(args[-1]).write_bytes(b"concatenated clip")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main._open_event_recordings.pop(1, None)  # test isolation: module-level state
    asyncio.run(main.persist_event_recording(1, now, now, detector="basic_motion", trigger_id="evt-1"))

    produced = list((tmp_path / "recordings" / "camera1").glob("camera1_*.mkv"))
    assert len(produced) == 1


# --------------------------------------------------------------- detector/trigger observability (2026-09-21)
#
# Requested directly after the Driveway Right post-roll fix above: that bug
# was invisible in every log for the entire pilot because persist_event_
# recording()'s "nothing to do yet" paths were silent. These tests prove a
# caller's detector name and trigger id now surface on both the no-op and
# the success path, so the next silent-seeming failure is diagnosable from
# logs alone instead of requiring a live file-timestamp investigation.


def test_persist_event_recording_logs_no_sources_with_detector_and_trigger_id(tmp_path, monkeypatch, caplog):
    import asyncio
    from datetime import datetime

    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    now = datetime.now()
    main._open_event_recordings.pop(1, None)
    with caplog.at_level("WARNING", logger="anyaicam.event_recording"):
        asyncio.run(
            main.persist_event_recording(
                1, now, now, detector="ai_detection", trigger_id="group-42",
            )
        )

    assert "event_recording.no_buffer_folder" in caplog.text
    assert "detector=ai_detection" in caplog.text
    assert "trigger_id=group-42" in caplog.text


def test_persist_event_recording_logs_persisted_clip_path_and_duration(tmp_path, monkeypatch, caplog):
    import asyncio
    from datetime import datetime, timedelta

    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    now = datetime.now()
    buffer_folder = tmp_path / "recordings" / "camera1" / main.EVENT_BUFFER_SUBFOLDER_NAME
    buffer_folder.mkdir(parents=True, exist_ok=True)
    segment_start = now - timedelta(seconds=15)
    segment_path = buffer_folder / f"buf1_{segment_start:%Y-%m-%d_%H-%M-%S}.mkv"
    segment_path.write_bytes(b"fake video bytes")

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            return type("Result", (), {"returncode": 0, "stdout": "12.3", "stderr": ""})()
        Path(args[-1]).write_bytes(b"concatenated clip")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main._open_event_recordings.pop(1, None)
    with caplog.at_level("INFO", logger="anyaicam.event_recording"):
        asyncio.run(
            main.persist_event_recording(
                1, now, now, detector="basic_motion", trigger_id="evt-99",
            )
        )

    assert "event_recording.triggered" in caplog.text
    assert "detector=basic_motion" in caplog.text
    assert "event_recording.persisted" in caplog.text
    assert "trigger_id=evt-99" in caplog.text
    assert "duration_seconds=12.3" in caplog.text
    produced = list((tmp_path / "recordings" / "camera1").glob("camera1_*.mkv"))
    assert len(produced) == 1
    assert str(produced[0]) in caplog.text
