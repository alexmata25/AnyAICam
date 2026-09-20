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
