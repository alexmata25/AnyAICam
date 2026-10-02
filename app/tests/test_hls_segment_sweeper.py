"""Orphaned live-HLS segment cleanup (hls_segment_sweeper.py, 2026-09-25).

Every FFmpeg generation of start_live_stream() leaves exactly one segment
behind (its pending-delete segment), reproduced with real FFmpeg:
generations stopped by TERM/TERM/KILL/TERM left camera7_...005, 016, 026,
037 -- isolated single sequence numbers, exactly the Ryzen's pattern."""
import asyncio
import os
import time
from pathlib import Path

import pytest

import hls_segment_sweeper as sweeper

NOW = 1_800_000_000.0
OLD = NOW - 3600          # well past the 10-minute threshold
FRESH = NOW - 30          # a segment FFmpeg itself may still be about to delete


def _segment(folder: Path, name: str, mtime: float, size: int = 1000) -> Path:
    path = folder / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def _playlist(folder: Path, camera: str, seqs, mtime: float = NOW) -> Path:
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{seqs[0]}"]
    for seq in seqs:
        lines += ["#EXTINF:2.000000,", f"{camera}_{seq:09d}.ts"]
    path = folder / f"{camera}.m3u8"
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()}


def test_the_ffmpeg_restart_pattern_is_cleaned_and_the_live_window_is_kept(tmp_path):
    """Four generations: one orphan each (5, 16, 26, 37); live window 38-42."""
    for seq in (5, 16, 26, 37):
        _segment(tmp_path, f"camera7_{seq:09d}.ts", OLD)
    live = list(range(38, 43))
    for seq in live:
        _segment(tmp_path, f"camera7_{seq:09d}.ts", OLD)  # even an OLD referenced segment is kept
    _playlist(tmp_path, "camera7", live)
    result = sweeper.sweep(tmp_path, now=NOW)
    assert result["deleted"] == 4 and result["cameras"] == {"camera7": 4} and result["bytes"] == 4000
    assert _names(tmp_path) == {"camera7.m3u8"} | {f"camera7_{seq:09d}.ts" for seq in live}


def test_any_number_of_cameras_each_against_its_own_playlist(tmp_path):
    for number in range(1, 13):
        camera = f"camera{number}"
        _playlist(tmp_path, camera, [100, 101, 102, 103, 104])
        for seq in (100, 101, 102, 103, 104):
            _segment(tmp_path, f"{camera}_{seq:09d}.ts", NOW - 5)
        _segment(tmp_path, f"{camera}_{50 + number:09d}.ts", OLD)  # one orphan each
    result = sweeper.sweep(tmp_path, now=NOW)
    assert result["deleted"] == 12
    for number in range(1, 13):
        assert f"camera{number}_{50 + number:09d}.ts" not in _names(tmp_path)
        assert f"camera{number}_000000104.ts" in _names(tmp_path)
    # camera1's playlist never protects camera10's segments (prefix-exact matching)
    _playlist(tmp_path, "camera1", [7])
    _segment(tmp_path, "camera10_000000007.ts", OLD)
    sweeper.sweep(tmp_path, now=NOW)
    assert "camera10_000000007.ts" not in _names(tmp_path)


def test_a_fresh_unreferenced_segment_is_left_for_ffmpeg(tmp_path):
    _playlist(tmp_path, "camera2", [10, 11, 12, 13, 14])
    _segment(tmp_path, "camera2_000000009.ts", FRESH)
    assert sweeper.sweep(tmp_path, now=NOW)["deleted"] == 0
    assert "camera2_000000009.ts" in _names(tmp_path)
    assert sweeper.sweep(tmp_path, now=FRESH + sweeper.MIN_AGE_SECONDS + 1)["deleted"] == 1


def test_only_camera_segment_files_are_ever_touched(tmp_path):
    for name in ("camera3.m3u8.tmp", "notes.txt", "camera3_abc.ts", "camera_000000001.ts", "cam3_000000001.ts",
                 "camera3_000000001.ts.part", "thumbnail.jpg"):
        _segment(tmp_path, name, OLD)
    (tmp_path / "camera3_000000002.ts").mkdir()  # a directory that looks like a segment
    _playlist(tmp_path, "camera3", [9])
    assert sweeper.sweep(tmp_path, now=NOW)["deleted"] == 0
    assert "camera3.m3u8" in _names(tmp_path)


def test_an_unreadable_playlist_skips_that_camera_only(tmp_path, monkeypatch):
    _playlist(tmp_path, "camera4", [20])
    _playlist(tmp_path, "camera5", [20])
    _segment(tmp_path, "camera4_000000001.ts", OLD)
    _segment(tmp_path, "camera5_000000001.ts", OLD)
    real = sweeper.referenced_segments
    monkeypatch.setattr(sweeper, "referenced_segments", lambda p: None if p.name == "camera4.m3u8" else real(p))
    result = sweeper.sweep(tmp_path, now=NOW)
    assert result["skipped_cameras"] == ["camera4"] and result["cameras"] == {"camera5": 1}
    assert "camera4_000000001.ts" in _names(tmp_path)


def test_a_camera_that_no_longer_streams_has_its_old_segments_removed(tmp_path):
    _segment(tmp_path, "camera9_000000001.ts", OLD)
    _segment(tmp_path, "camera9_000000002.ts", FRESH)
    assert sweeper.sweep(tmp_path, now=NOW)["deleted"] == 1
    assert _names(tmp_path) == {"camera9_000000002.ts"}


def test_a_large_backlog_is_bounded_per_pass(tmp_path):
    _playlist(tmp_path, "camera6", [999])
    for seq in range(250):
        _segment(tmp_path, f"camera6_{seq:09d}.ts", OLD, size=10)
    assert sweeper.sweep(tmp_path, now=NOW, max_deletions=100)["deleted"] == 100
    assert sweeper.sweep(tmp_path, now=NOW, max_deletions=100)["deleted"] == 100
    assert sweeper.sweep(tmp_path, now=NOW, max_deletions=100)["deleted"] == 50


def test_a_missing_folder_or_racing_deletion_never_raises(tmp_path, monkeypatch):
    assert sweeper.sweep(tmp_path / "nope", now=NOW)["deleted"] == 0
    _playlist(tmp_path, "camera8", [5])
    _segment(tmp_path, "camera8_000000001.ts", OLD)
    real_unlink = os.unlink

    def racing_unlink(path):
        real_unlink(path)
        raise FileNotFoundError(path)  # FFmpeg got there first
    monkeypatch.setattr(sweeper.os, "unlink", racing_unlink)
    assert sweeper.sweep(tmp_path, now=NOW)["deleted"] == 0


def test_the_worker_sweeps_at_start_and_keeps_going_on_a_backlog(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sweeper, "SWEEP_ENABLED", True)
    monkeypatch.setattr(sweeper, "MAX_DELETIONS_PER_SWEEP", 1)

    def fake_sweep(folder):
        calls.append(folder)
        return {"deleted": 1 if len(calls) == 1 else 0, "bytes": 5, "skipped_cameras": [], "cameras": {"camera1": 1}}
    monkeypatch.setattr(sweeper, "sweep", fake_sweep)
    real_sleep = asyncio.sleep
    sleeps = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    logs = []
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sweeper.hls_segment_sweeper_worker(tmp_path, log=logs.append))
    assert calls == [tmp_path, tmp_path]  # immediately at start, then again
    assert sleeps == [10, sweeper.SWEEP_INTERVAL_SECONDS]  # quick follow-up while a backlog remains
    assert "removed 1 orphaned segment" in logs[0]


def test_the_live_hls_encode_and_its_window_are_unchanged():
    import main
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert '"-hls_list_size", "5"' in source and "delete_segments+append_list+omit_endlist+independent_segments" in source
    assert "hls_segment_sweeper.hls_segment_sweeper_worker(HLS_FOLDER)" in source
