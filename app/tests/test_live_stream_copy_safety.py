"""Ryzen VMS hotfix integration (2026-10-08, Red on Orange's patch).

* Live stream copy is used per camera, only after ffprobe confirmed the
  camera sends H.264, and falls back to the x264 encode when the probe
  fails, the codec is anything else, or copied streams keep failing.
* The codec probe never blocks the event loop and never logs the camera URL.
* Camera passwords are masked in ffmpeg output whatever characters they hold.
* Every ffmpeg worker's stderr is drained (a piped, undrained stderr would
  stall ffmpeg once the pipe fills).
* The container log is bounded.
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

import main

ROOT = Path(__file__).resolve().parents[2]
ENCODE = ["-c:v", "libx264"]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(main, "_live_copy_probe", {})
    monkeypatch.setattr(main, "_live_copy_quick_failures", {})
    monkeypatch.setattr(main, "_live_copy_disabled_until", {})


def _video(camera=1):
    args = main.live_video_args(camera)
    return args[:2]


# ------------------------------------------------------------------ copy only when verified
def test_off_by_default_encodes_even_an_h264_camera(monkeypatch):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", False)
    main._live_copy_probe[1] = (time.monotonic(), "h264")
    assert _video() == ENCODE


@pytest.mark.parametrize("codec, expected", [("h264", ["-c:v", "copy"]), ("hevc", ENCODE), ("mjpeg", ENCODE), (None, ENCODE)])
def test_copy_only_for_a_verified_h264_camera(monkeypatch, codec, expected):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    main._live_copy_probe[1] = (time.monotonic(), codec)
    assert _video() == expected


def test_an_unprobed_camera_is_encoded_and_cameras_are_independent(monkeypatch):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    main._live_copy_probe[1] = (time.monotonic(), "h264")
    assert _video(1) == ["-c:v", "copy"]
    assert _video(2) == ENCODE
    assert main.live_video_args() [:2] == ENCODE  # no camera named: never copy


def test_the_encode_keeps_the_existing_settings(monkeypatch):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    args = main.live_video_args(5)
    for item in ("veryfast", "zerolatency", "-g", "56", "-keyint_min", "28", "-sc_threshold", "-maxrate"):
        assert item in args


# ------------------------------------------------------------------ fallback
def test_two_quick_failures_fall_back_to_encode_for_an_hour(monkeypatch, capsys):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    main._live_copy_probe[1] = (time.monotonic(), "h264")
    main.note_live_copy_run(1, True, 5.0, False)
    assert _video() == ["-c:v", "copy"]               # one failure: still copy
    main.note_live_copy_run(1, True, 5.0, False)
    assert _video() == ENCODE                          # two in a row: x264
    assert main._live_copy_disabled_until[1] - time.monotonic() > 3500
    assert "using x264 encode for 60 minutes" in capsys.readouterr().out
    main._live_copy_disabled_until[1] = time.monotonic() - 1
    assert _video() == ["-c:v", "copy"]                # after the period: copy again


def test_a_stalled_copied_stream_counts_as_a_failure(monkeypatch):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    main._live_copy_probe[1] = (time.monotonic(), "h264")
    main.note_live_copy_run(1, True, 900.0, True)
    main.note_live_copy_run(1, True, 900.0, True)
    assert _video() == ENCODE


def test_a_healthy_run_resets_the_count_and_encode_runs_are_ignored(monkeypatch):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    main._live_copy_probe[1] = (time.monotonic(), "h264")
    main.note_live_copy_run(1, True, 5.0, False)
    main.note_live_copy_run(1, True, 600.0, False)     # healthy
    main.note_live_copy_run(1, True, 5.0, False)
    assert _video() == ["-c:v", "copy"]
    main.note_live_copy_run(2, False, 1.0, True)        # an encoding run never counts
    assert 2 not in main._live_copy_quick_failures


# ------------------------------------------------------------------ the probe
class FakeProcess:
    def __init__(self, stdout=b"h264\n", returncode=0, hang=False):
        self._stdout, self.returncode, self._hang, self.killed = stdout, returncode, hang, False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _probe(monkeypatch, process, timeout=1.0):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return process
    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    codec = asyncio.run(main.probe_camera_video_codec("rtsp://admin:p@ss/w0rd@10.0.0.5:554/s", timeout=timeout))
    return codec, seen


def test_the_probe_reads_the_codec_and_discards_stderr(monkeypatch):
    codec, seen = _probe(monkeypatch, FakeProcess(b"h264\n"))
    assert codec == "h264"
    assert seen["args"][0] == "ffprobe" and "-rtsp_transport" in seen["args"] and "stream=codec_name" in seen["args"]
    assert seen["kwargs"]["stderr"] is asyncio.subprocess.DEVNULL
    assert seen["kwargs"]["stdout"] is asyncio.subprocess.PIPE


@pytest.mark.parametrize("process", [FakeProcess(b"", 0), FakeProcess(b"h264", 1), FakeProcess(b"h264; rm -rf /", 0),
                                     FakeProcess(b"\xff\xfe", 0)])
def test_a_failed_or_odd_probe_means_encode(monkeypatch, process):
    assert _probe(monkeypatch, process)[0] is None


def test_a_hanging_probe_is_killed_after_the_timeout(monkeypatch):
    process = FakeProcess(hang=True)
    started = time.monotonic()
    assert _probe(monkeypatch, process, timeout=0.2)[0] is None
    assert process.killed and time.monotonic() - started < 5


def test_no_ffprobe_means_encode(monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", missing)
    assert asyncio.run(main.probe_camera_video_codec("rtsp://cam/s")) is None


def test_refresh_caches_retries_failures_sooner_and_never_logs_the_url(monkeypatch, capsys):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    monkeypatch.setattr(main, "camera_url", lambda n: "rtsp://admin:Secret!7@10.0.0.5:554/s")
    answers = ["h264", "h264"]
    calls = []

    async def probe(url, timeout=10):
        calls.append(url)
        return answers.pop(0)
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    asyncio.run(main.refresh_live_copy_probe(4))
    asyncio.run(main.refresh_live_copy_probe(4))             # cached
    assert len(calls) == 1 and main.live_copy_allowed(4)
    out = capsys.readouterr().out
    assert "camera sends h264; using stream copy" in out
    assert "Secret" not in out and "admin" not in out and "10.0.0.5" not in out
    main._live_copy_probe[4] = (time.monotonic() - main.LIVE_VIDEO_COPY_PROBE_TTL_SECONDS - 1, "h264")
    asyncio.run(main.refresh_live_copy_probe(4))             # expired: probed again
    assert len(calls) == 2


def test_refresh_does_nothing_when_copy_is_off_or_the_camera_is_not_configured(monkeypatch):
    calls = []

    async def probe(url, timeout=10):
        calls.append(url)
        return "h264"
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", False)
    asyncio.run(main.refresh_live_copy_probe(1))
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)

    def not_configured(n):
        raise main.CameraNotConfiguredError(n)
    monkeypatch.setattr(main, "camera_url", not_configured)
    asyncio.run(main.refresh_live_copy_probe(1))
    assert calls == [] and 1 not in main._live_copy_probe


# ------------------------------------------------------------------ redaction, every password shape
@pytest.mark.parametrize("url, secret", [
    ("rtsp://admin:p@ss@10.0.0.5:554/s", "p@ss"),          # "@" in the password (leaked "ss" before)
    ("rtsp://admin:pa/ss@10.0.0.5:554/s", "pa/ss"),        # "/" in the password (not masked at all before)
    ("rtsp://admin:a:b:c@10.0.0.5/s", "a:b:c"),
    ("rtsp://admin:S3cret%21@10.0.0.5/s", "S3cret"),
    ("rtsps://op:x@y@cam.local/live", "x@y"),
    ("'rtsp://admin:quoted@10.0.0.5/s'", "quoted"),
])
def test_no_password_shape_survives(url, secret):
    line = f"[rtsp @ 0x55] Error opening input file {url}."
    redacted = main._redact_stream_credentials(line)
    assert secret not in redacted and "admin" not in redacted and "op:" not in redacted
    assert "://***@" in redacted


def test_the_host_and_path_stay_and_other_urls_are_untouched():
    redacted = main._redact_stream_credentials("Error opening input file rtsp://u:p@ss@10.0.0.5:554/Streaming/101 then http://example/x")
    assert "rtsp://***@10.0.0.5:554/Streaming/101" in redacted and "http://example/x" in redacted


def test_drained_output_never_carries_a_password(capsys):
    script = ("import sys\n"
              "for url in ['rtsp://admin:p@ss@10.0.0.5/s', 'rtsp://admin:pa/ss@10.0.0.6/s', 'rtsp://admin:plain@10.0.0.7/s']:\n"
              "    print(f'Error opening input file {url}', file=sys.stderr)\n")
    process = subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE)
    buffer = []
    asyncio.run(main._drain_camera_stderr(7, process, buffer))
    process.wait(timeout=10)
    out = capsys.readouterr().out
    for secret in ("p@ss", "pa/ss", "plain", "admin"):
        assert secret not in out and all(secret not in line for line in buffer), secret
    assert out.count("rtsp://***@") == 3


# ------------------------------------------------------------------ ffmpeg process handling
def test_every_worker_stderr_is_drained_by_the_supervisor():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    supervisor = source.split("async def process_supervisor", 1)[1].split("\nasync def ", 1)[0]
    assert "if process.stderr is not None:" in supervisor
    assert 'if mode == "live" and process.stderr is not None:' not in supervisor   # not live-only any more
    assert supervisor.count("stderr=subprocess.PIPE") == 0                          # started only by the starters
    assert "await drain_task" in supervisor


def test_the_supervisor_probes_before_starting_and_records_the_outcome():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    supervisor = source.split("async def process_supervisor", 1)[1].split("\nasync def ", 1)[0]
    probe = supervisor.index("await refresh_live_copy_probe(camera_number)")
    assert probe < supervisor.index("process = _select_recording_starter()(camera_number)")
    assert supervisor.index("completed = True") < supervisor.index(
        "note_live_copy_run(camera_number, live_copy_used, time.monotonic() - worker_started, watchdog_restart)")


def test_the_container_log_is_bounded():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "driver: json-file" in compose and 'max-size: "50m"' in compose and 'max-file: "5"' in compose
