"""W-1 (Windows 1.3.4 parity, 2026-10-27): LPR's L1 freshness check must read
the buffer segment's real metadata, not the directory entry.

On NTFS the directory index entry of a file that ANOTHER process keeps open
and appends to (FFmpeg writing the in-progress buffer segment) is not updated
until the writer closes it. `os.scandir()` entries carry that index data, so
`DirEntry.stat().st_mtime` stays at the segment's creation time while the
file grows. Measured on the Dell (Windows 11, 2026-10-27): with a separate
writer appending every 0.5 s, the directory entry reported an age of 61 -> 70 s
and a frozen size, while `os.stat(path)` reported 0.4 s and the true size.
With the 10 s L1 bound the live segment was rejected about 10 s after it
started, so LPR lost its full-resolution frame on Windows for most of every
30 s segment. `os.stat(path)` opens the file and reads current metadata.

The L1 rules themselves are unchanged (test_lpr_stale_buffer.py): a stopped
camera is never decoded, a future mtime is as stale as an old one.
"""
import os
import subprocess
import sys
import textwrap
import time

import cv2
import numpy as np
import pytest

import lpr


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    lpr._full_frame_cache.clear()
    monkeypatch.setattr(lpr, "LPR_FULL_RES_FRAMES", True)
    monkeypatch.setattr(lpr, "LPR_FULL_RES_MAX_AGE_SECONDS", 10.0)
    yield
    lpr._full_frame_cache.clear()


def _bmp():
    ok, data = cv2.imencode(".bmp", np.full((1080, 1920, 3), 90, np.uint8))
    assert ok
    return data.tobytes()


class _Ffmpeg:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(os.path.basename(args[args.index("-i") + 1]))
        return type("R", (), {"stdout": _bmp()})()


class _StaleEntry:
    """A DirEntry whose cached stat is the directory index's stale view."""

    def __init__(self, entry, stale_mtime):
        self._entry, self.name, self.path = entry, entry.name, entry.path
        self._stale = stale_mtime

    def stat(self, *args, **kwargs):
        return os.stat_result((0, 0, 0, 0, 0, 0, 3, self._stale, self._stale, self._stale))

    def is_file(self, *args, **kwargs):
        return True


def test_a_growing_segment_is_fresh_even_when_the_directory_entry_is_stale(monkeypatch, tmp_path):
    """Portable: the directory entry claims 60 s old, the file itself was
    just written. L1 must believe the file."""
    now = time.time()
    live = tmp_path / "buf2_2026-10-27_10-00-00.mkv"
    live.write_bytes(b"mkv")
    os.utime(live, (now - 0.5, now - 0.5))
    real_scandir = os.scandir

    class _Scan:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._it.close()

        def __iter__(self):
            return (_StaleEntry(entry, now - 60) for entry in self._it)

    monkeypatch.setattr(lpr.os, "scandir", _Scan)
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    frame = lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=now)
    assert frame is not None
    assert ffmpeg.calls == [live.name]


def test_a_really_stopped_camera_is_still_never_decoded(monkeypatch, tmp_path):
    """The fix must not weaken L1: a file whose own mtime is old stays stale."""
    now = time.time()
    old = tmp_path / "buf3_2026-10-27_09-00-00.mkv"
    old.write_bytes(b"mkv")
    os.utime(old, (now - 600, now - 600))
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=1.0, wall_now=now) is None
    assert ffmpeg.calls == []


_WRITER = textwrap.dedent(
    """
    import sys, time
    with open(sys.argv[1], "ab", buffering=0) as f:
        sys.stdout.write("ready\\n"); sys.stdout.flush()
        end = time.time() + float(sys.argv[2])
        while time.time() < end:
            f.write(b"y" * 20000)
            time.sleep(0.25)
    """
)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS directory-entry staleness is Windows-specific")
def test_windows_segment_held_open_by_another_process_is_fresh(monkeypatch, tmp_path):
    """Real NTFS: a separate process (as FFmpeg) keeps appending to a segment
    created 60 s ago. Its directory entry still says 60 s; L1 must pick it."""
    segment = tmp_path / "buf2_2026-10-27_10-00-00.mkv"
    segment.write_bytes(b"x" * 1000)
    created = time.time() - 60
    os.utime(segment, (created, created))
    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(segment), "6"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert writer.stdout.readline().strip() == "ready"
        time.sleep(1.5)
        entry_age = time.time() - next(os.scandir(tmp_path)).stat().st_mtime
        ffmpeg = _Ffmpeg()
        monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
        frame = lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=time.time())
    finally:
        writer.wait(timeout=30)
    # Precondition shows the NTFS behaviour this test exists for; if a future
    # Windows build updates the index live, the assertion below still holds.
    print(f"directory-entry age while open: {entry_age:.1f} s")
    assert frame is not None
    assert ffmpeg.calls == [segment.name]
