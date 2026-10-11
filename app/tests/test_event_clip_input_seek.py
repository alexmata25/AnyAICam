"""video-pipeline P2c (2026-10-11): event clips with input seeking, scaled
into the measured profile, thread cap, H.264 + faststart.

Command-shape tests always run. Where ffmpeg is installed, real HEVC
recordings with real timestamps are generated and:
  * the frames the new input-seek command selects are compared, frame by
    frame (decoded MD5), with the previous output-seek command -- single
    file, a window spanning two files (concat), and an in-progress
    recording without an index;
  * build_motion_event_clip() is run end to end and the event's flash
    frame must sit at the pre-roll offset of the clip;
  * the clip is H.264 yuv420p with the moov atom first (browser playable)
    at the profile size."""
import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import clip_encode
import video_profile

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def _profile(width, height):
    return video_profile.build({"streams": [{"uri": "rtsp://cam/main", "role": "main", "width": width, "height": height, "codec": "H265"}]})


# ---------------------------------------------------------------- command shape

def test_single_covering_file_is_read_directly_with_input_seek():
    cmd = clip_encode.event_clip_command([Path("/r/camera1_a.mkv")], Path("/c/.e.txt"), 47.25, 10.0, Path("/c/.e.mp4"),
                                         _profile(3840, 2160), mode="optimized", scale="profile", threads=2)
    assert cmd.index("-ss") < cmd.index("-i") and cmd[cmd.index("-i") + 1] == "/r/camera1_a.mkv"
    assert "concat" not in cmd
    assert cmd[cmd.index("-ss") + 1] == "47.250" and cmd[cmd.index("-t") + 1] == "10.000"
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("scale='min(1920,iw)':'min(1080,ih)'") and vf.endswith("format=yuv420p")
    assert cmd[cmd.index("-c:v") + 1] == "libx264" and "+faststart" in cmd
    assert cmd.count("-threads") == 2 and cmd[cmd.index("-threads") + 1] == "2"   # decoder and encoder
    assert cmd[cmd.index("-maxrate") + 1] == f"{_profile(3840, 2160).clip_max_kbps}k"
    assert cmd[-1] == "/c/.e.mp4"


def test_multi_file_window_uses_concat_with_input_seek():
    cmd = clip_encode.event_clip_command([Path("/r/a.mkv"), Path("/r/b.mkv")], Path("/c/.e.txt"), 295.0, 10.0,
                                         Path("/c/.e.mp4"), _profile(2592, 1944), mode="optimized", scale="profile", threads=2)
    assert cmd.index("-f") < cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-i") + 1] == "/c/.e.txt" and "concat" in cmd


def test_legacy_mode_is_the_previous_command_exactly():
    old = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "/c/.e.txt", "-ss", "12.5", "-t", "10.0",
           "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart", "/c/.e.mp4"]
    assert clip_encode.event_clip_command([Path("/r/a.mkv")], Path("/c/.e.txt"), 12.5, 10.0, Path("/c/.e.mp4"),
                                          _profile(3840, 2160), mode="legacy") == old
    old_manual = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "/c/.j.txt", "-ss", "3", "-t", "60.0", "-an",
                  "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart", "/c/out.mp4"]
    assert clip_encode.manual_clip_command([Path("/r/a.mkv")], Path("/c/.j.txt"), 3, 60.0, Path("/c/out.mp4"),
                                           None, mode="legacy") == old_manual


def test_scale_full_and_unknown_profile():
    full = clip_encode.event_clip_command([Path("/r/a.mkv")], Path("/c/l.txt"), 1, 2, Path("/c/o.mp4"),
                                          _profile(3840, 2160), mode="optimized", scale="full", threads=0)
    assert "-vf" not in full and full[full.index("-pix_fmt") + 1] == "yuv420p" and "-threads" not in full
    unknown = clip_encode.event_clip_command([Path("/r/a.mkv")], Path("/c/l.txt"), 1, 2, Path("/c/o.mp4"),
                                             video_profile.build(None), mode="optimized", scale="profile", threads=2)
    assert "min(1920,iw)" in unknown[unknown.index("-vf") + 1]   # safe default: downscale-only


def test_manual_clip_keeps_full_resolution_by_default():
    cmd = clip_encode.manual_clip_command([Path("/r/a.mkv"), Path("/r/b.mkv")], Path("/c/j.txt"), 30, 60, Path("/c/o.mp4"),
                                          _profile(3840, 2160), mode="optimized", scale="full", threads=2)
    assert "-vf" not in cmd and "-an" in cmd and cmd.index("-ss") < cmd.index("-i")
    scaled = clip_encode.manual_clip_command([Path("/r/a.mkv")], Path("/c/j.txt"), 30, 60, Path("/c/o.mp4"),
                                             _profile(3840, 2160), mode="optimized", scale="profile", threads=2)
    assert "-vf" in scaled


def test_unknown_env_values_fall_back_safely(monkeypatch):
    assert clip_encode._mode("X_UNSET_FLAG", "optimized", clip_encode.ENCODE_MODES, "legacy") == "optimized"
    monkeypatch.setenv("X_BAD_FLAG", "turbo")
    assert clip_encode._mode("X_BAD_FLAG", "optimized", clip_encode.ENCODE_MODES, "legacy") == "legacy"


# ---------------------------------------------------------------- real recordings

def _make_recording(path: Path, seconds: float, *, flash_at=None, size="640x360", fps=20, start_number=0):
    """HEVC mkv like the recorder's (-c:v copy of an H.265 camera): GOP 2 s,
    timestamps starting at 0 (-reset_timestamps). The frame number is drawn
    as a brightness ramp so every frame decodes to a distinct image; a
    flash (white frame) marks the event."""
    flash = f",drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t\\,{flash_at}\\,{flash_at + 0.25})'" if flash_at is not None else ""
    # testsrc2 animates every frame (distinct decoded images); a second file
    # gets a different hue so a window landing in the wrong file shows up.
    source = f"testsrc2=size={size}:rate={fps}" + (f",hue=h={start_number % 360}" if start_number else "") + flash
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(seconds),
                    "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", f"keyint={2 * fps}:min-keyint={2 * fps}:log-level=error",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=300)


def _frame_md5s(cmd_input_side, offset, duration, *, input_seek):
    """Decoded frames the old (output seek) or new (input seek) selection yields."""
    if input_seek:
        cmd = ["ffmpeg", "-v", "error", *cmd_input_side[:-2], "-ss", f"{offset:.3f}", *cmd_input_side[-2:],
               "-t", f"{duration:.3f}", "-map", "0:v:0", "-f", "framemd5", "-"]
    else:
        cmd = ["ffmpeg", "-v", "error", *cmd_input_side, "-ss", str(offset), "-t", str(duration),
               "-map", "0:v:0", "-f", "framemd5", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=300).stdout
    return [line.rsplit(",", 1)[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("rec")
    a, b = root / "a.mkv", root / "b.mkv"
    _make_recording(a, 30, flash_at=28.5)
    _make_recording(b, 30, start_number=120)
    concat = root / "list.txt"
    concat.write_text(f"file '{a.as_posix()}'\nfile '{b.as_posix()}'\n", encoding="utf-8")
    # An in-progress recording: the segment muxer has not written its index
    # (Cues) yet -- simulated by cutting the finished file short.
    whole = a.read_bytes()
    partial = root / "partial.mkv"
    partial.write_bytes(whole[: int(len(whole) * 0.8)])
    return {"a": a, "b": b, "list": concat, "partial": partial}


@needs_ffmpeg
@pytest.mark.parametrize("offset,duration", [(3.3, 4.0), (17.05, 6.0), (0.0, 2.0)])
def test_input_seek_selects_exactly_the_same_frames_single_file(recordings, offset, duration):
    side = ["-i", str(recordings["a"])]
    old = _frame_md5s(side, offset, duration, input_seek=False)
    new = _frame_md5s(side, offset, duration, input_seek=True)
    assert abs(len(old) - round(duration * 20)) <= 1 and new == old


@needs_ffmpeg
def test_input_seek_selects_exactly_the_same_frames_across_two_files(recordings):
    side = ["-f", "concat", "-safe", "0", "-i", str(recordings["list"])]
    old = _frame_md5s(side, 25.0, 10.0, input_seek=False)
    new = _frame_md5s(side, 25.0, 10.0, input_seek=True)
    assert abs(len(old) - 200) <= 1 and new == old


@needs_ffmpeg
def test_input_seek_in_an_unindexed_in_progress_recording(recordings):
    side = ["-i", str(recordings["partial"])]
    old = _frame_md5s(side, 12.4, 5.0, input_seek=False)
    new = _frame_md5s(side, 12.4, 5.0, input_seek=True)
    assert abs(len(old) - 100) <= 1 and new == old


def _probe(path):
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                      "stream=codec_name,width,height,pix_fmt:format=duration", "-of", "json", str(path)],
                                     capture_output=True, text=True, check=True).stdout)


def _flash_time(path):
    """Seconds into the clip of the brightest frame."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                          "-f", "null", "-"], capture_output=True, text=True, check=True, timeout=300).stdout
    best, best_time, current_time = -1.0, None, None
    for line in out.splitlines():
        if line.startswith("frame:"):
            current_time = float(line.split("pts_time:")[1])
        elif "YAVG=" in line:
            value = float(line.split("=")[1])
            if value > best:
                best, best_time = value, current_time
    return best_time


@needs_ffmpeg
def test_build_motion_event_clip_covers_the_event_with_real_timestamps(tmp_path, monkeypatch):
    import main
    recordings_dir = tmp_path / "recordings"
    camera_dir = recordings_dir / "camera1"
    camera_dir.mkdir(parents=True)
    file_start = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
    first = camera_dir / f"camera1_{file_start.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    second = camera_dir / f"camera1_{(file_start + timedelta(seconds=30)).strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    _make_recording(first, 30, flash_at=28.5, size="1280x720")
    _make_recording(second, 30, start_number=120, size="1280x720")
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings_dir)
    monkeypatch.setattr(main, "CLIPS_FOLDER", tmp_path / "clips")
    monkeypatch.setattr(main, "_local_recording_settings", lambda n: {"mode": "continuous", "max_event_seconds": 300})

    class Fixed:
        def profile(self, n):
            return video_profile.build({"streams": [{"uri": "x", "role": "main", "width": 1280, "height": 720}]})

    monkeypatch.setattr(main, "camera_video_profiles", Fixed())
    monkeypatch.setattr(main.clip_encode, "EVENT_CLIP_ENCODE", "optimized")
    # The 10 s window (5 s pre-roll) spans both recording files (concat path).
    event = file_start + timedelta(seconds=28.5)
    url = asyncio.run(main.build_motion_event_clip("evspan", 1, event, event))
    assert url
    clip = tmp_path / "clips" / "motion" / "motion_evspan.mp4"
    info = _probe(clip)
    stream = info["streams"][0]
    assert (stream["codec_name"], stream["pix_fmt"]) == ("h264", "yuv420p")
    assert abs(float(info["format"]["duration"]) - 10.0) < 0.3
    flash = _flash_time(clip)
    assert flash is not None and abs(flash - 5.0) <= 0.1      # the event sits at the 5 s pre-roll
    head = clip.read_bytes()[:4096]
    assert b"moov" in head                                    # +faststart: playable before full download


@needs_ffmpeg
def test_profile_scaled_clip_is_browser_playable_at_profile_size(tmp_path):
    source = tmp_path / "camera_4mp.mkv"
    _make_recording(source, 6, size="2560x1440")
    out = tmp_path / "clip.mp4"
    cmd = clip_encode.event_clip_command([source], tmp_path / "l.txt", 1.0, 3.0, out, _profile(2560, 1440),
                                         mode="optimized", scale="profile", threads=2)
    subprocess.run(cmd + [], check=True, capture_output=True, timeout=300)
    stream = _probe(out)["streams"][0]
    assert (stream["codec_name"], stream["width"], stream["height"], stream["pix_fmt"]) == ("h264", 1920, 1080, "yuv420p")
    data = out.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")
