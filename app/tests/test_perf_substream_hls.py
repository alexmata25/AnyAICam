"""perf-substream-hls (2026-10-10): motion reader on the camera substream
(ANYAICAM_MOTION_SOURCE) and on-demand live HLS (ANYAICAM_LIVE_HLS_MODE).

Fakes only -- no real ffmpeg, camera, network or database. Covers:
  * motion reader command construction for both sources, the fallback to
    HLS after repeated substream failures, and "hls" = the old command;
  * substream URL resolution from the stored capability record and from
    the URL convention, credentials applied but never logged;
  * the live supervisor's on-demand start / idle stop / restart, and that
    "always" starts the encoder with no demand exactly as before;
  * consumers still served: HTTP/static playlist + segment requests, the
    customer live playlist route, the live relay, analytics frames, motion
    thumbnails, local status and the appliance agent's status reader.
"""
import asyncio
import os
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

import camera_capabilities
import live_hls_demand
import motion_source
import main

_REAL_SLEEP = asyncio.sleep
SECRET = "s3cr3t-P@ss"
MAIN_URL = f"rtsp://admin:{SECRET}@192.0.2.10:554/Streaming/Channels/101"
SUB_URL = f"rtsp://admin:{SECRET}@192.0.2.10:554/Streaming/Channels/102"

OLD_MOTION_HLS_COMMAND = [
    "ffmpeg", "-loglevel", "error", "-fflags", "nobuffer",
    "-i", "/app/static/hls/camera3.m3u8", "-an", "-vf", "fps=1,scale=160:90,format=gray",
    "-f", "rawvideo", "pipe:1",
]


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _reset_demand():
    live_hls_demand.configure(mode="always")
    yield
    live_hls_demand.configure(mode="always")


# --------------------------------------------------------------- motion_source


def test_hls_command_is_the_previous_command_exactly():
    assert motion_source.hls_command("/app/static/hls/camera3.m3u8") == OLD_MOTION_HLS_COMMAND


def test_substream_command_same_filter_over_rtsp_tcp():
    command = motion_source.substream_command(SUB_URL)
    assert command[:5] == ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp"]
    assert command[command.index("-i") + 1] == SUB_URL
    assert command[command.index("-vf") + 1] == "fps=1,scale=160:90,format=gray"
    assert command[-3:] == ["-f", "rawvideo", "pipe:1"]
    assert "-an" in command


def test_frame_geometry_matches_detector_assumptions():
    # motion_detector() reads exactly 160*90 bytes per frame and the zone
    # mask is 160x90: the filter must force that size from any source.
    assert motion_source.MOTION_FRAME_WIDTH * motion_source.MOTION_FRAME_HEIGHT == 160 * 90
    assert "scale=160:90" in motion_source.MOTION_FRAME_FILTER and "format=gray" in motion_source.MOTION_FRAME_FILTER


@pytest.mark.parametrize("value,expected", [
    (None, "auto"), ("auto", "auto"), ("SUBSTREAM", "substream"), (" hls ", "hls"), ("bogus", "hls"), ("", "hls"),
])
def test_motion_source_mode_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("ANYAICAM_MOTION_SOURCE", raising=False)
    if value is None:
        assert motion_source.configured_mode() == expected
    else:
        assert motion_source.configured_mode(value) == expected


def _record(main_uri, *subs):
    streams = [{"id": "main", "uri": main_uri, "role": "main", "width": 3840, "height": 2160}]
    streams += list(subs)
    return {"schema": 1, "probed_at": "2026-10-10T00:00:00+00:00", "streams": streams}


def test_substream_from_record_prefers_smallest_usable_sub():
    clean_main = camera_capabilities.split_credentials(MAIN_URL)[0]
    record = _record(
        clean_main,
        {"uri": "rtsp://192.0.2.10:554/third", "role": "sub", "width": 1280, "height": 720},
        {"uri": "rtsp://192.0.2.10:554/Streaming/Channels/102", "role": "sub", "width": 640, "height": 360},
        {"uri": "rtsp://192.0.2.10:554/tiny", "role": "sub", "width": 96, "height": 54},
        {"uri": "rtsp://192.0.2.10:554/broken", "role": "sub", "width": 320, "height": 180, "verified": False},
    )
    assert motion_source.substream_from_record(record, MAIN_URL) == "rtsp://192.0.2.10:554/Streaming/Channels/102"


def test_substream_from_record_ignores_record_for_another_main_url():
    record = _record("rtsp://192.0.2.99:554/other", {"uri": "rtsp://192.0.2.99:554/sub", "role": "sub", "width": 640})
    assert motion_source.substream_from_record(record, MAIN_URL) is None
    assert motion_source.substream_from_record(None, MAIN_URL) is None
    assert motion_source.substream_from_record(_record(camera_capabilities.split_credentials(MAIN_URL)[0]), MAIN_URL) is None


def test_resolve_applies_credentials_to_record_substream():
    clean_main = camera_capabilities.split_credentials(MAIN_URL)[0]
    record = _record(clean_main, {"uri": "rtsp://192.0.2.10:554/Streaming/Channels/102", "role": "sub", "width": 640})
    url = motion_source.resolve_substream_url(MAIN_URL, record, None)
    assert url is not None and url.endswith("/Streaming/Channels/102")
    assert camera_capabilities.split_credentials(url)[1:] == ("admin", SECRET)


def test_resolve_falls_back_to_url_convention():
    import webrtc_publisher
    url = motion_source.resolve_substream_url(MAIN_URL, None, webrtc_publisher.substream_candidates)
    assert url == SUB_URL


def test_resolve_none_without_distinct_substream():
    import webrtc_publisher
    unknown = f"rtsp://admin:{SECRET}@192.0.2.10:554/live/main"
    assert motion_source.resolve_substream_url(unknown, None, webrtc_publisher.substream_candidates) is None
    assert motion_source.resolve_substream_url(MAIN_URL, None, lambda url: [url]) is None
    assert motion_source.resolve_substream_url(MAIN_URL, None, lambda url: 1 / 0) is None
    assert motion_source.resolve_substream_url("", None, None) is None


def test_selector_modes():
    resolver = lambda n: SUB_URL
    assert motion_source.MotionSourceSelector("hls").choose(1, resolver) == ("hls", None)
    assert motion_source.MotionSourceSelector("auto").choose(1, resolver) == ("substream", SUB_URL)
    assert motion_source.MotionSourceSelector("substream").choose(1, resolver) == ("substream", SUB_URL)
    assert motion_source.MotionSourceSelector("auto").choose(1, lambda n: None) == ("hls", None)
    assert motion_source.MotionSourceSelector("substream").choose(1, lambda n: None) == ("hls", None)

    def boom(n):
        raise RuntimeError(SECRET)
    assert motion_source.MotionSourceSelector("auto").choose(1, boom) == ("hls", None)


def test_selector_auto_falls_back_after_n_failures_then_retries():
    clock = Clock()
    selector = motion_source.MotionSourceSelector("auto", max_failures=3, retry_seconds=600, clock=clock)
    resolver = lambda n: SUB_URL
    assert selector.note_run(1, "substream", frames=0) is None
    assert selector.note_run(1, "substream", frames=5) is None  # too few frames: still a failure
    message = selector.note_run(1, "substream", frames=0, stalled=True)
    assert message and SECRET not in message and "rtsp" not in message
    assert selector.choose(1, resolver) == ("hls", None)
    assert selector.choose(2, resolver) == ("substream", SUB_URL)  # per camera
    clock.now += 601
    assert selector.choose(1, resolver) == ("substream", SUB_URL)


def test_selector_healthy_run_resets_and_substream_mode_never_falls_back():
    selector = motion_source.MotionSourceSelector("auto", max_failures=2)
    selector.note_run(1, "substream", frames=0)
    selector.note_run(1, "substream", frames=500)
    assert selector.failures(1) == 0
    forced = motion_source.MotionSourceSelector("substream", max_failures=1)
    for _ in range(5):
        assert forced.note_run(1, "substream", frames=0) is None
    assert forced.choose(1, lambda n: SUB_URL) == ("substream", SUB_URL)
    assert selector.note_run(1, "hls", frames=0) is None


def test_note_kind_messages_never_carry_urls_and_are_silent_in_hls_mode():
    selector = motion_source.MotionSourceSelector("auto")
    message = selector.note_kind(4, "substream")
    assert message and "substream" in message and "rtsp" not in message
    assert selector.note_kind(4, "substream") is None
    assert motion_source.MotionSourceSelector("hls").note_kind(4, "hls") is None


# --------------------------------------------------------------- motion_detector wiring


class _Stdout:
    def __init__(self, script):
        self.script = list(script)

    async def readexactly(self, n):
        if not self.script:
            raise asyncio.CancelledError()
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if item == "hang":
            await _REAL_SLEEP(3600)
        return item


class _Proc:
    def __init__(self, script):
        self.stdout = _Stdout(script)
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode or 0


def _motion_env(monkeypatch, tmp_path, mode, scripts, resolver=lambda n: SUB_URL, clock=None):
    hls = tmp_path / "hls"
    hls.mkdir(exist_ok=True)
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    selector = motion_source.MotionSourceSelector(mode, max_failures=3, retry_seconds=600, clock=clock or Clock())
    monkeypatch.setattr(main, "_motion_source_selector", selector)
    monkeypatch.setattr(main, "motion_substream_url", resolver)
    monkeypatch.setattr(main, "get_event_settings", lambda n: types.SimpleNamespace(enabled=False))
    commands = []
    queue = list(scripts)

    async def fake_exec(*command, **kwargs):
        commands.append(list(command))
        assert kwargs.get("stderr") is asyncio.subprocess.DEVNULL  # ffmpeg's stderr (with the URL) is discarded
        return _Proc(queue.pop(0) if queue else [])

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
    return hls, commands, selector


FRAME = bytes(160 * 90)


def test_motion_detector_reads_substream_without_any_hls(monkeypatch, tmp_path, capsys):
    hls, commands, _ = _motion_env(monkeypatch, tmp_path, "auto", [[FRAME, FRAME]])
    assert not (hls / "camera3.m3u8").exists()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(3))
    assert commands == [motion_source.substream_command(SUB_URL)]
    out = capsys.readouterr().out
    assert "motion detector source: camera substream" in out
    assert SECRET not in out


def test_motion_detector_hls_mode_runs_the_old_command(monkeypatch, tmp_path, capsys):
    hls, commands, _ = _motion_env(monkeypatch, tmp_path, "hls", [[FRAME, FRAME]])
    (hls / "camera3.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(3))
    expected = list(OLD_MOTION_HLS_COMMAND)
    expected[expected.index("-i") + 1] = str(hls / "camera3.m3u8")
    assert commands == [expected]
    assert "motion detector source" not in capsys.readouterr().out


def test_motion_detector_falls_back_to_hls_after_substream_failures(monkeypatch, tmp_path, capsys):
    eof = asyncio.IncompleteReadError(b"", 160 * 90)
    scripts = [[eof], [eof], [eof], [FRAME]]
    hls, commands, selector = _motion_env(monkeypatch, tmp_path, "auto", scripts)
    (hls / "camera3.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(3))
    kinds = ["substream" if "-rtsp_transport" in c else "hls" for c in commands]
    assert kinds == ["substream", "substream", "substream", "hls"]
    out = capsys.readouterr().out
    assert "failed 3 times" in out and SECRET not in out


def test_motion_detector_stalled_substream_counts_as_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(motion_source, "SUBSTREAM_STALL_SECONDS", 0.05)
    scripts = [["hang"]]
    hls, commands, selector = _motion_env(monkeypatch, tmp_path, "substream", scripts)

    calls = {"n": 0}
    real_choose = selector.choose

    def choose_then_stop(camera_number, resolver):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()
        return real_choose(camera_number, resolver)

    monkeypatch.setattr(selector, "choose", choose_then_stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(3))
    assert selector.failures(3) == 1
    out = capsys.readouterr().out
    assert "stopped sending frames" in out and SECRET not in out


def test_motion_hls_reader_holds_on_demand_encoder(monkeypatch, tmp_path):
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    hls, commands, _ = _motion_env(monkeypatch, tmp_path, "auto", [[FRAME]], resolver=lambda n: None)
    (hls / "camera3.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(3))
    assert live_hls_demand.state(3)["holds"] == ["motion"]
    assert live_hls_demand.wanted(3)


def test_motion_substream_reader_does_not_hold_encoder(monkeypatch, tmp_path):
    clock = Clock()
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=clock)
    _motion_env(monkeypatch, tmp_path, "auto", [[FRAME]])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(3))
    clock.now += 61
    assert not live_hls_demand.wanted(3)
    assert live_hls_demand.camera_reachable(3, recorder_running=False, within_seconds=120)


# --------------------------------------------------------------- live_hls_demand


def test_always_mode_is_inert(tmp_path):
    live_hls_demand.configure(mode="always")
    assert live_hls_demand.wanted(1)
    live_hls_demand.touch(1)
    live_hls_demand.hold(1, "x")
    assert live_hls_demand.state(1)["holds"] == []
    live_hls_demand.write_alive(tmp_path, 1)
    assert not live_hls_demand.alive_path(tmp_path, 1).exists()


# video-pipeline P2b (2026-10-11): the default is now "auto" (test_live_hls_auto_mode.py).
@pytest.mark.parametrize("value,expected", [(None, "auto"), ("on_demand", "on_demand"), ("on-demand", "on_demand"),
                                            ("ALWAYS", "always"), ("x", "always"), ("auto", "auto")])
def test_live_hls_mode_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("ANYAICAM_LIVE_HLS_MODE", raising=False)
    assert live_hls_demand.configured_mode(value) == expected


def test_on_demand_touch_hold_release_provider_and_idle():
    clock = Clock()
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=clock)
    assert not live_hls_demand.wanted(2)
    live_hls_demand.touch(2, "http")
    assert live_hls_demand.wanted(2)
    clock.now += 59
    assert live_hls_demand.wanted(2)
    clock.now += 2
    assert not live_hls_demand.wanted(2)
    live_hls_demand.hold(2, "motion")
    clock.now += 10_000
    assert live_hls_demand.wanted(2)
    live_hls_demand.release(2, "motion")
    assert live_hls_demand.wanted(2)  # idle timer restarts at release
    clock.now += 61
    assert not live_hls_demand.wanted(2)
    active = []
    live_hls_demand.add_provider(lambda: active)
    live_hls_demand.add_provider(lambda: 1 / 0)  # a broken provider never breaks the check
    assert not live_hls_demand.wanted(2)
    active.append(2)
    assert live_hls_demand.wanted(2)


@pytest.mark.parametrize("path,expected", [
    ("/static/hls/camera1.m3u8", 1), ("/static/hls/camera12_000000031.ts", 12), ("/static/hls/camera1.alive", None),
    ("/static/hls/other.m3u8", None), ("/static/hls/camera.m3u8", None), ("/static/hls/camera3x.m3u8", None),
])
def test_camera_number_from_hls_path(path, expected):
    assert live_hls_demand.camera_number_from_hls_path(path) == expected


def test_mark_playlist_stale_and_freshness(tmp_path):
    playlist = tmp_path / "camera1.m3u8"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    assert live_hls_demand.playlist_fresh(playlist)
    live_hls_demand.mark_playlist_stale(playlist)
    assert not live_hls_demand.playlist_fresh(playlist)
    assert playlist.read_text(encoding="utf-8") == "#EXTM3U\n"  # content kept for append_list
    live_hls_demand.mark_playlist_stale(tmp_path / "missing.m3u8")  # never raises


# --------------------------------------------------------------- live supervisor


class _LiveProc:
    def __init__(self):
        self.returncode = None
        self.stderr = None
        self.terminated = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _supervisor_env(monkeypatch, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir(exist_ok=True)
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main, "CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(live_hls_demand, "IDLE_POLL_SECONDS", 0)
    started = []

    def fake_start(camera_number):
        process = _LiveProc()
        started.append(process)
        (hls / f"camera{camera_number}.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
        return process

    monkeypatch.setattr(main, "start_live_stream", fake_start)
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
    return hls, started, real_sleep


async def _until(predicate, real_sleep, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await real_sleep(0.002)


def test_supervisor_on_demand_start_idle_stop_restart(monkeypatch, tmp_path, capsys):
    camera = 57
    clock = Clock()
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=clock)
    hls, started, real_sleep = _supervisor_env(monkeypatch, tmp_path)
    state = main.camera_process_state[camera]
    reconnects_before = main.camera_reconnect_counts[camera]

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera, "live"))
        try:
            await _until(lambda: state["live"] == "idle", real_sleep)
            await real_sleep(0.05)
            assert started == []  # no demand: the encoder never starts
            live_hls_demand.touch(camera, "http")
            await _until(lambda: len(started) == 1 and state["live"] == "running", real_sleep)
            clock.now += 30
            await real_sleep(0.05)
            assert started[0].terminated == 0  # still within the idle window
            clock.now += 31
            await _until(lambda: started[0].terminated == 1 and state["live"] == "idle", real_sleep)
            assert len(started) == 1
            assert not live_hls_demand.playlist_fresh(hls / f"camera{camera}.m3u8")
            live_hls_demand.touch(camera, "playlist")
            await _until(lambda: len(started) == 2 and state["live"] == "running", real_sleep)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert main.camera_reconnect_counts[camera] == reconnects_before  # idle stops are not failures
    out = capsys.readouterr().out
    assert "stopping the encoder" in out and "process exited with" not in out


def test_supervisor_on_demand_writes_alive_while_idle_and_reachable(monkeypatch, tmp_path):
    camera = 58
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    hls, started, real_sleep = _supervisor_env(monkeypatch, tmp_path)
    main.camera_process_state[camera]["recording"] = "running"

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera, "live"))
        try:
            await _until(lambda: (hls / f"camera{camera}.alive").exists(), real_sleep)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert started == []


def test_supervisor_always_mode_starts_without_demand(monkeypatch, tmp_path):
    camera = 59
    live_hls_demand.configure(mode="always")
    hls, started, real_sleep = _supervisor_env(monkeypatch, tmp_path)
    main.camera_process_state[camera]["recording"] = "running"

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera, "live"))
        try:
            await _until(lambda: len(started) == 1, real_sleep)
            await real_sleep(0.05)
            assert started[0].terminated == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not (hls / f"camera{camera}.alive").exists()
    assert main.camera_process_state[camera]["live"] != "idle"


# --------------------------------------------------------------- consumers


def test_static_hls_request_is_demand(monkeypatch):
    from fastapi.testclient import TestClient
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    client = TestClient(main.app)
    client.get("/static/hls/camera6.m3u8")
    client.get("/static/hls/camera8_000000004.ts")
    assert live_hls_demand.wanted(6) and live_hls_demand.wanted(8)
    assert not live_hls_demand.wanted(7)


def test_customer_local_playlist_and_segment_are_demand(tmp_path):
    import local_live_hls
    from fastapi import HTTPException
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    text = local_live_hls.local_playlist(tmp_path, 4, "cam-4")
    assert "#EXTINF" not in text  # nothing yet: the live page keeps polling
    assert live_hls_demand.wanted(4)
    with pytest.raises(HTTPException):
        local_live_hls.segment_path(tmp_path, 9, "camera9_000000001.ts")
    assert live_hls_demand.wanted(9)


def test_customer_local_playlist_served_while_fresh(tmp_path):
    import local_live_hls
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    (tmp_path / "camera4_000000001.ts").write_bytes(b"ts")
    (tmp_path / "camera4.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\ncamera4_000000001.ts\n", encoding="utf-8")
    assert "camera4_000000001.ts" in local_live_hls.local_playlist(tmp_path, 4, "cam-4")
    live_hls_demand.mark_playlist_stale(tmp_path / "camera4.m3u8")
    assert "#EXTINF" not in local_live_hls.local_playlist(tmp_path, 4, "cam-4")


def test_detect_objects_frame_on_demand_never_reads_stale_playlist(monkeypatch, tmp_path):
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    monkeypatch.setattr(main.ai_frames, "mode", "hls")  # the live-HLS AI path (video-pipeline P2a: ANYAICAM_AI_SOURCE=hls)
    hls = tmp_path / "hls"
    hls.mkdir()
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main, "cv2", types.SimpleNamespace(VideoCapture=lambda *a: pytest.fail("read a stale playlist")))
    monkeypatch.setattr(main, "YOLO", object())
    playlist = hls / "camera2.m3u8"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    live_hls_demand.mark_playlist_stale(playlist)
    result = main.detect_objects_frame(2)
    assert result["ok"] is False and "starting" in result["error"]
    assert live_hls_demand.wanted(2)  # analytics keeps the encoder running


def test_create_motion_thumbnail_uses_substream_when_encoder_idle(monkeypatch, tmp_path):
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    hls = tmp_path / "hls"
    hls.mkdir()
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main, "MOTION_THUMBNAILS_FOLDER", tmp_path / "thumbs")
    monkeypatch.setattr(main, "motion_substream_url", lambda n: SUB_URL)
    commands = []

    class P:
        returncode = 1

        async def wait(self):
            return 1

        async def communicate(self, data=None):
            return b"", b""

    async def fake_exec(*command, **kwargs):
        commands.append(list(command))
        return P()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(main.create_motion_thumbnail(2, "e" * 32, FRAME, datetime(2026, 10, 10, 12, 0, 0)))
    first = commands[0]
    assert first[first.index("-i") - 2:first.index("-i") + 2] == ["-rtsp_transport", "tcp", "-i", SUB_URL]


def test_create_motion_thumbnail_always_mode_reads_hls_as_before(monkeypatch, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "camera2.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main, "MOTION_THUMBNAILS_FOLDER", tmp_path / "thumbs")
    monkeypatch.setattr(main, "motion_substream_url", lambda n: pytest.fail("always mode must not use the substream"))
    commands = []

    class P:
        returncode = 1

        async def wait(self):
            return 1

        async def communicate(self, data=None):
            return b"", b""

    async def fake_exec(*command, **kwargs):
        commands.append(list(command))
        return P()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(main.create_motion_thumbnail(2, "e" * 32, FRAME, datetime(2026, 10, 10, 12, 0, 0)))
    assert commands[0][:6] == ["ffmpeg", "-loglevel", "error", "-y", "-i", str(hls / "camera2.m3u8")]


def _status_for(monkeypatch, tmp_path, camera):
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [camera])
    return main._legacy_camera_status()["cameras"][0]


def test_local_status_counts_alive_file_only_on_demand(monkeypatch, tmp_path):
    camera = 61
    (tmp_path / f"camera{camera}.alive").write_text("", encoding="utf-8")
    live_hls_demand.configure(mode="always")
    assert _status_for(monkeypatch, tmp_path, camera)["online"] is False  # unchanged when always on
    live_hls_demand.configure(mode="on_demand")
    assert _status_for(monkeypatch, tmp_path, camera)["online"] is True
    old = time.time() - 60
    os.utime(tmp_path / f"camera{camera}.alive", (old, old))
    assert _status_for(monkeypatch, tmp_path, camera)["online"] is False


def test_agent_status_reader_accepts_alive_file(tmp_path):
    from anyaicam_agent.camera_binding import LocalVmsStatusReader
    reader = LocalVmsStatusReader(tmp_path, tmp_path / "rec")
    assert reader.status(3)["online"] is False
    (tmp_path / "camera3.alive").write_text("", encoding="utf-8")
    assert reader.status(3)["online"] is True
    old = time.time() - 60
    os.utime(tmp_path / "camera3.alive", (old, old))
    assert reader.status(3)["online"] is False
    (tmp_path / "camera3.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    assert reader.status(3)["online"] is True


def test_live_relay_active_cameras_keep_encoder_running():
    import live_relay_uploader
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    live_hls_demand.add_provider(live_relay_uploader.active_camera_numbers)
    assert not live_hls_demand.wanted(11)
    live_relay_uploader.set_relay_active(11, "cam-11", True)
    try:
        assert live_hls_demand.wanted(11)
    finally:
        live_relay_uploader.set_relay_active(11, "cam-11", False)
    assert not live_hls_demand.wanted(11)


def test_live_encoder_command_is_untouched():
    # The recorder and the live encoder commands are not part of this change.
    import inspect
    source = inspect.getsource(main.start_live_stream)
    assert '"-c:a", "aac"' in source and "camera_url(camera_number)" in source
    assert "live_hls_demand" not in source
    assert "live_hls_demand" not in inspect.getsource(main.start_recording)


# --------------------------------------------------------------- watchdog restart churn


class _FakeTime:
    """main.time stand-in: every (patched) asyncio.sleep advances it."""

    def __init__(self):
        self.offset = 0.0
        self._real = time

    def monotonic(self):
        return self._real.monotonic() + self.offset

    def time(self):
        return self._real.time() + self.offset

    def __getattr__(self, name):
        return getattr(self._real, name)


def _first_watchdog_kill_offset(monkeypatch, tmp_path, from_worker_start, write_playlist_at=None):
    camera = 62
    live_hls_demand.configure(mode="always")
    hls = tmp_path / "hls"
    hls.mkdir(exist_ok=True)
    playlist = hls / f"camera{camera}.m3u8"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    old = time.time() - 120  # the previous worker's playlist, left by append_list
    os.utime(playlist, (old, old))
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main, "CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main, "LIVE_HLS_WATCHDOG_FROM_WORKER_START", from_worker_start)
    fake_time = _FakeTime()
    monkeypatch.setattr(main, "time", fake_time)
    kills = []

    class Proc(_LiveProc):
        def poll(self):
            if write_playlist_at is not None and fake_time.offset >= write_playlist_at:
                now = time.time() + fake_time.offset
                os.utime(playlist, (now, now))
            return self.returncode

        def terminate(self):
            kills.append(fake_time.offset)
            super().terminate()

    monkeypatch.setattr(main, "start_live_stream", lambda n: Proc())
    real_sleep = asyncio.sleep

    async def advancing_sleep(seconds, *args, **kwargs):
        fake_time.offset += seconds
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", advancing_sleep)

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera, "live"))
        try:
            deadline = time.monotonic() + 3
            while not kills and fake_time.offset < 200 and time.monotonic() < deadline:
                await real_sleep(0.001)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    return kills[0] if kills else None


def test_watchdog_old_rule_kills_slow_starting_worker_at_first_check(monkeypatch, tmp_path, capsys):
    # The measured ~3 restarts / 120 s: killed at 30 s, +TERM, +10 s backoff.
    assert _first_watchdog_kill_offset(monkeypatch, tmp_path, from_worker_start=False) == 30
    assert "live HLS playlist stale" in capsys.readouterr().out


def test_watchdog_counts_from_worker_start(monkeypatch, tmp_path):
    # A new worker gets the full 45 s to write its first playlist ...
    assert _first_watchdog_kill_offset(monkeypatch, tmp_path, from_worker_start=True) in (45, 50)


def test_watchdog_slow_but_working_start_is_not_killed(monkeypatch, tmp_path):
    # ... and one that writes it at 35 s keeps running.
    assert _first_watchdog_kill_offset(monkeypatch, tmp_path, from_worker_start=True, write_playlist_at=35) is None
