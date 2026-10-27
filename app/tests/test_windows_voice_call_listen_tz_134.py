"""W-2 (Windows 1.3.4 parity, 2026-10-27): Voice Call listening must read the
buffer segment names in LOCAL time.

The buffer recorder names segments with FFmpeg's `-strftime 1`
(`buf<N>_%Y-%m-%d_%H-%M-%S.mkv`), which formats with the C library's
localtime(). main.py's recorder janitor and event extraction parse those
names as local time (naive datetimes compared with datetime.now()). The
listener parsed them as UTC. The Linux container has no TZ, so local time
is UTC there and nothing showed; a Windows PC normally is not on UTC (the
Dell is UTC-5), so the listening window selected segments hours away from
the visitor's answer, or none.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import aac_voice_call_listen as listen

INSTANT = datetime(2026, 10, 27, 15, 13, 25, tzinfo=timezone.utc)


def _ffmpeg_name(camera, moment):
    """The name FFmpeg -strftime gives a segment starting at `moment` (local time)."""
    return f"buf{camera}_" + time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(moment.timestamp())) + ".mkv"


def _runner(calls):
    def runner(cmd, **kwargs):
        with open(cmd[cmd.index("-i") + 1], encoding="utf-8") as listing_file:
            calls.append((cmd, listing_file.read()))
        return type("R", (), {"stdout": b"\x01\x00" * 16000 * 8})()

    return runner


def test_a_segment_name_is_read_back_as_the_instant_ffmpeg_wrote_it():
    from pathlib import Path

    start = INSTANT.replace(second=0)
    parsed = listen._segment_start(Path(_ffmpeg_name(5, start)))
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.timestamp() == start.timestamp()


def test_the_listening_window_selects_the_segments_around_the_answer(tmp_path):
    first = INSTANT.replace(second=0)
    names = [_ffmpeg_name(5, first + timedelta(seconds=30 * i)) for i in range(-2, 3)]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    calls = []
    pcm = listen.capture_window(tmp_path, INSTANT, 8.0, runner=_runner(calls))
    assert pcm
    cmd, listing = calls[0]
    # 15:13:25-15:13:33 UTC: the segments starting 15:13:00 and 15:13:30 UTC.
    assert [name for name in names if name in listing] == [names[2], names[3]]
    assert cmd[cmd.index("-ss") + 1] == "25.00"


def _local_offset_hours():
    return (datetime.fromtimestamp(INSTANT.timestamp()) - INSTANT.replace(tzinfo=None)).total_seconds() / 3600


@pytest.mark.skipif(_local_offset_hours() == 0, reason="needs a non-UTC host zone (the Windows case)")
def test_on_a_non_utc_host_the_window_is_not_shifted_by_the_zone_offset(tmp_path):
    """On the Dell (UTC-5): a segment named with the UTC clock time but
    actually written hours later must not be chosen for this window."""
    utc_named_wrong = "buf5_" + INSTANT.replace(second=0).strftime("%Y-%m-%d_%H-%M-%S") + ".mkv"
    right = _ffmpeg_name(5, INSTANT.replace(second=0))
    assert utc_named_wrong != right
    for name in (utc_named_wrong, right):
        (tmp_path / name).write_bytes(b"x")
    calls = []
    assert listen.capture_window(tmp_path, INSTANT, 8.0, runner=_runner(calls))
    listing = calls[0][1]
    assert right in listing and utc_named_wrong not in listing


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
@pytest.mark.parametrize("zone", ["Etc/GMT+5", "Etc/GMT-2"])  # UTC-5 and UTC+2
def test_posix_zones_round_trip(monkeypatch, zone):
    from pathlib import Path

    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        start = INSTANT.replace(second=0)
        assert listen._segment_start(Path(_ffmpeg_name(5, start))).timestamp() == start.timestamp()
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
