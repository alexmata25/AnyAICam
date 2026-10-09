import main


def _command(monkeypatch, copy):
    seen = {}
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", copy)
    # 2026-10-08: copy also needs this camera's codec verified as H.264
    # (test_live_stream_copy_safety.py covers the probe and the fallback).
    import time
    monkeypatch.setattr(main, "_live_copy_probe", {2: (time.monotonic(), "h264")})
    monkeypatch.setattr(main, "_live_copy_disabled_until", {})
    monkeypatch.setattr(main, "camera_url", lambda n: "rtsp://cam/stream")
    monkeypatch.setattr(main.subprocess, "Popen", lambda cmd, **kw: seen.setdefault("cmd", cmd))
    main.start_live_stream(2)
    return seen["cmd"]


def test_default_is_the_existing_x264_encode(monkeypatch):
    cmd = _command(monkeypatch, False)
    assert cmd[cmd.index("-c:v") + 1] == "libx264" and "-maxrate" in cmd and "zerolatency" in cmd


def test_toggle_copies_video_and_keeps_audio_and_hls(monkeypatch):
    cmd = _command(monkeypatch, True)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "libx264" not in cmd and "-maxrate" not in cmd and "-g" not in cmd
    assert cmd[cmd.index("-c:a") + 1] == "aac" and cmd[cmd.index("-f") + 1] == "hls"
    assert cmd[-1].endswith("camera2.m3u8")


def test_toggle_is_off_unless_explicitly_true(monkeypatch):
    import os
    assert os.environ.get("ANYAICAM_LIVE_VIDEO_COPY") is None and main.LIVE_VIDEO_COPY is False
