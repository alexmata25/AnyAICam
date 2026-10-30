"""Windows 1.3.4.1 Sandbox F-L (2026-10-29): persist_event_recording() concats the event-buffer segment ffmpeg is
still writing. With the Matroska muxer's default buffering, a low-bitrate camera's open segment stayed 0 bytes on disk
for 40+ s, so 5 of 8 motion events failed to concat ("Invalid data", returncode 3199971767) and the rest were a
fraction of the event. The buffer writer must flush every packet and close a cluster at least every second."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import main


def _launched(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        return object()

    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "camera_url", lambda camera_number: "rtsp://camera.test/stream")
    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
    main.start_event_recording_buffer(1)
    return seen["command"]


def test_the_buffer_writer_flushes_packets_and_closes_clusters_every_second(monkeypatch, tmp_path):
    command = _launched(monkeypatch, tmp_path)
    index = command.index("-segment_format_options")
    assert command[index + 1] == main.EVENT_BUFFER_MUXER_OPTIONS == "flush_packets=1:cluster_time_limit=1000"
    # The options belong to the inner muxer, so they come after "-f segment" and before the output pattern.
    assert command.index("segment") < index < len(command) - 2
    assert command[-1].endswith(".mkv") and "_event_buffer" in command[-1]


def _ffmpeg():
    explicit = os.environ.get("ANYAICAM_TEST_FFMPEG")
    return explicit if explicit and Path(explicit).is_file() else shutil.which("ffmpeg")


@pytest.mark.skipif(_ffmpeg() is None, reason="needs a real ffmpeg (PATH or ANYAICAM_TEST_FFMPEG)")
@pytest.mark.parametrize("muxer_options, expect_readable", [(None, False), ("flush_packets=1:cluster_time_limit=1000", True)])
def test_a_low_bitrate_open_segment_is_readable_only_with_the_flush_options(tmp_path, muxer_options, expect_readable):
    ffmpeg = _ffmpeg()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10",
               "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-g", "56",
               "-b:v", "60k", "-maxrate", "60k", "-bufsize", "60k",
               "-f", "segment", "-segment_time", "30", "-reset_timestamps", "1"]
    if muxer_options:
        command += ["-segment_format_options", muxer_options]
    command.append(str(tmp_path / "buf_%03d.mkv"))
    writer = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    try:
        time.sleep(8)
        segment = sorted(tmp_path.glob("buf_*.mkv"))[-1]
        listing = tmp_path / "list.txt"
        listing.write_text(f"file '{segment}'\n")
        clip = tmp_path / "clip.mkv"
        concat = subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                                 "-t", "8", "-c", "copy", str(clip)], capture_output=True, timeout=60)
    finally:
        writer.terminate()
        writer.wait(10)
    readable = concat.returncode == 0 and clip.exists() and clip.stat().st_size > 0
    assert readable is expect_readable, (concat.returncode, concat.stderr[-200:])
