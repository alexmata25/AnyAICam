import asyncio
import subprocess
import sys

import main


def test_redaction_masks_user_and_password_but_keeps_host_and_path():
    line = "Error opening input file rtsp://admin:S3cret%21@192.168.0.44:554/Streaming/Channels/101?profile=Profile_1."
    redacted = main._redact_stream_credentials(line)
    assert "admin" not in redacted and "S3cret" not in redacted
    assert "rtsp://***@192.168.0.44:554/Streaming/Channels/101?profile=Profile_1." in redacted


def test_redaction_leaves_lines_without_credentials_alone():
    for line in ("frame=2397028 fps= 30 q=21.0", "Connection to tcp://192.168.0.145:554?timeout=0 failed",
                 "rtsp://192.168.0.44:554/Streaming/Channels/101", "user@example.com wrote"):
        assert main._redact_stream_credentials(line) == line


def test_redaction_covers_every_scheme_and_repeated_urls():
    line = "a rtsp://u:p@h1/x b RTSPS://u:p@h2/y c http://u:p@h3/z"
    assert main._redact_stream_credentials(line) == "a rtsp://***@h1/x b RTSPS://***@h2/y c http://***@h3/z"


def test_drain_prints_and_keeps_only_redacted_lines(capsys):
    script = "import sys; [print(f'Error opening input file rtsp://admin:pw{i}@10.0.0.{i}:554/s', file=sys.stderr) for i in range(30)]"
    process = subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE)
    buffer = []
    asyncio.run(main._drain_camera_stderr(2, process, buffer))
    process.wait(timeout=10)
    out = capsys.readouterr().out
    assert "admin" not in out and "pw" not in out
    assert "[camera2] Error opening input file rtsp://***@10.0.0.29:554/s" in out
    assert len(buffer) == main.CAMERA_STDERR_MAX_LINES and all("***@" in line for line in buffer)


def test_drain_does_not_occupy_the_default_executor():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"], stderr=subprocess.PIPE)

    async def run():
        loop = asyncio.get_running_loop()
        calls = []
        original = loop.run_in_executor
        loop.run_in_executor = lambda *a, **k: calls.append(a) or original(*a, **k)
        await main._drain_camera_stderr(1, process, [])
        return calls

    assert asyncio.run(run()) == []
    process.wait(timeout=10)


def _launched_command(monkeypatch, starter):
    seen = {}
    monkeypatch.setattr(main, "camera_url", lambda n: "rtsp://admin:pw@cam/stream")
    monkeypatch.setattr(main.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or object())
    starter(3)
    return seen


def test_recording_and_buffer_processes_pipe_stderr_for_redaction(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    for starter in (main.start_recording, main.start_event_recording_buffer):
        seen = _launched_command(monkeypatch, starter)
        assert seen["kw"].get("stderr") is subprocess.PIPE, starter.__name__


def test_live_stream_keeps_piping_stderr(monkeypatch):
    seen = _launched_command(monkeypatch, main.start_live_stream)
    assert seen["kw"].get("stderr") is subprocess.PIPE
