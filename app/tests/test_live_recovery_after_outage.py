"""Recovery after a camera outage (Ryzen, 2026-10-17).

Seen live: driveway cameras 2/3 (2560x1440 H.264) were unreachable for days at
their old DHCP addresses. Every live-copy codec probe failed, so "unknown
codec" was cached (retried only every 60 s, while the live worker retries
every 10 s) and the motion reader's substream failures put it on the HLS
fallback for 30 minutes. When the address was corrected the live worker
started inside the 60 s window, chose the x264 encode without probing, and --
because a healthy worker was never re-probed -- would have stayed on x264 for
good; motion decoded the encoded live stream. Two 1440p->1080p encodes took
the box from 77 to 92 C in about 2.5 minutes (rolled back).

Now:
  - a probe answer belongs to the stream address it was taken at: a changed
    address drops it, the copy-failure fallback and the motion fallback;
  - the first video a live worker writes after an outage drops an "unknown
    codec" answer and the motion fallback (a verified codec and a genuine
    copy-failure fallback are kept);
  - a healthy worker encoding only because the codec was unknown re-probes
    (rate-limited) and restarts itself into stream copy -- not counted as a
    reconnect or an error;
  - a motion reader on the HLS fallback re-chooses its source when the camera
    recovers.
"""
import asyncio
import collections
import time

import pytest

import main
import motion_source

URL_OLD = "rtsp://admin:S3cret!x@192.168.0.44:554/Streaming/Channels/101"
URL_NEW = "rtsp://admin:S3cret!x@192.168.0.43:554/Streaming/Channels/101"


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    monkeypatch.setattr(main, "_live_copy_probe", {})
    monkeypatch.setattr(main, "_live_copy_probe_url", {})
    monkeypatch.setattr(main, "_live_copy_quick_failures", {})
    monkeypatch.setattr(main, "_live_copy_disabled_until", {})
    monkeypatch.setattr(main, "_motion_source_selector", motion_source.MotionSourceSelector(mode="auto", retry_seconds=1800))
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    monkeypatch.setattr(main.live_hls_demand, "on_demand", lambda: False)
    yield


class _Probe:
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    async def __call__(self, url, timeout=None):
        self.urls.append(url)
        return self.answers(url)


# ------------------------------------------------------------------ unit level
def test_a_changed_stream_address_drops_everything_learned_at_the_old_one(monkeypatch, capsys):
    url = {"v": URL_OLD}
    monkeypatch.setattr(main, "camera_url", lambda n: url["v"])
    probe = _Probe(lambda u: None if "0.44" in u else "h264")
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    asyncio.run(main.refresh_live_copy_probe(2))
    main._live_copy_disabled_until[2] = time.monotonic() + 3600
    main._motion_source_selector._hls_until[2] = time.monotonic() + 1800
    generation = main._motion_source_selector.generation(2)
    assert main.live_copy_allowed(2) is False
    url["v"] = URL_NEW  # the address is corrected (by hand or by 1.2.7 IP recovery)
    asyncio.run(main.refresh_live_copy_probe(2))
    assert probe.urls == [URL_OLD, URL_NEW]  # re-probed at once, not after the 60 s retry
    assert main.live_copy_allowed(2) is True
    assert 2 not in main._live_copy_disabled_until
    assert main._motion_source_selector.generation(2) == generation + 1
    assert main._motion_source_selector.choose(2, lambda n: "rtsp://sub")[0] == "substream"
    out = capsys.readouterr().out
    assert "stream address changed" in out and "S3cret" not in out and "admin" not in out


def test_the_same_address_keeps_the_retry_window(monkeypatch):
    monkeypatch.setattr(main, "camera_url", lambda n: URL_OLD)
    probe = _Probe(lambda u: None)
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    for _ in range(5):
        asyncio.run(main.refresh_live_copy_probe(2))
    assert len(probe.urls) == 1  # the 60 s retry for a failed probe is unchanged


def test_recovery_drops_only_an_unknown_codec_and_keeps_a_real_fallback():
    main._live_copy_probe[2] = (time.monotonic(), None)
    main._live_copy_probe[3] = (time.monotonic(), "h264")
    main._live_copy_disabled_until[3] = time.monotonic() + 3600  # copy genuinely failed on this camera
    main.note_live_stream_recovered(2)
    main.note_live_stream_recovered(3)
    assert 2 not in main._live_copy_probe
    assert main._live_copy_probe[3][1] == "h264"
    assert main.live_copy_allowed(3) is False  # its fallback hour is kept


def test_forget_bumps_the_generation_and_ends_the_hls_window():
    selector = motion_source.MotionSourceSelector(mode="auto", retry_seconds=1800)
    selector._hls_until[3] = time.monotonic() + 1800
    assert selector.choose(3, lambda n: "rtsp://sub")[0] == "hls"
    selector.forget(3)
    assert selector.generation(3) == 1
    assert selector.choose(3, lambda n: "rtsp://sub")[0] == "substream"


# ------------------------------------------------------- the real supervisor
class _Worker:
    """A live ffmpeg stand-in: down = exits at once without writing; up = writes
    the playlist on every poll until terminated."""

    def __init__(self, playlist, up, copy):
        self.playlist, self.up, self.copy = playlist, up, copy
        self.returncode = None if up else 1
        self.stderr = None
        self.pid = id(self)

    def poll(self):
        if self.returncode is None and self.up:
            self.playlist.write_text("#EXTM3U\n")
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    kill = terminate


def _run_supervisor(monkeypatch, scenario, until, timeout=10.0):
    """Runs process_supervisor(2, "live") with fast sleeps until `until(starts)`."""
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0.001)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(main, "CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main, "camera_process_state", collections.defaultdict(dict))
    monkeypatch.setattr(main, "camera_reconnect_counts", collections.defaultdict(int))
    monkeypatch.setattr(main, "ffmpeg_processes", [])
    starts = []
    playlist = main.HLS_FOLDER / "camera2.m3u8"

    def start_live_stream(n):
        up = scenario(len(starts))
        starts.append("copy" if main.live_copy_allowed(n) else "encode")
        if up:
            playlist.unlink(missing_ok=True)
        return _Worker(playlist, up, starts[-1] == "copy")

    monkeypatch.setattr(main, "start_live_stream", start_live_stream)

    async def go():
        task = asyncio.create_task(main.process_supervisor(2, "live"))
        deadline = time.monotonic() + timeout
        while not until(starts) and time.monotonic() < deadline:
            await real_sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    return starts


def test_a_camera_back_from_an_outage_is_upgraded_from_encode_to_copy(monkeypatch, capsys):
    monkeypatch.setattr(main, "camera_url", lambda n: URL_NEW)
    up = {"v": False}
    probe = _Probe(lambda u: "h264" if up["v"] else None)
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    generation = main._motion_source_selector.generation(2)

    def scenario(i):
        if i >= 3:
            up["v"] = True  # the camera answers again from the 4th start on
        return up["v"]

    starts = _run_supervisor(monkeypatch, scenario, lambda s: len(s) >= 5)
    # down x3 (probe failed, cached), back up: encode (inside the retry window), then copy
    assert starts[:5] == ["encode", "encode", "encode", "encode", "copy"]
    assert main._motion_source_selector.generation(2) > generation  # motion gets its substream back
    assert main.camera_reconnect_counts[2] == 3  # the upgrade restart is not a reconnect
    out = capsys.readouterr().out
    assert "is sending video again" in out and "restarting the live worker with stream copy" in out
    assert "S3cret" not in out


def test_a_readdressed_camera_starts_in_copy_straight_away(monkeypatch):
    url = {"v": URL_OLD}
    monkeypatch.setattr(main, "camera_url", lambda n: url["v"])
    monkeypatch.setattr(main, "probe_camera_video_codec", _Probe(lambda u: "h264" if "0.43" in u else None))

    def scenario(i):
        if i >= 1:
            url["v"] = URL_NEW  # corrected while start 1 is failing; start 2 is the first at the new address
        return i >= 2

    starts = _run_supervisor(monkeypatch, scenario, lambda s: len(s) >= 3, timeout=3.0)
    assert starts == ["encode", "encode", "copy"]  # the first start at the new address already copies


def test_a_healthy_h265_camera_is_not_restarted_or_probed_again(monkeypatch):
    monkeypatch.setattr(main, "camera_url", lambda n: URL_NEW)
    probe = _Probe(lambda u: "hevc")
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    starts = _run_supervisor(monkeypatch, lambda i: True, lambda s: False, timeout=1.0)
    assert starts == ["encode"]  # one worker for the whole run
    assert len(probe.urls) == 1


@pytest.mark.parametrize("retry_seconds", [60, 0.2])
def test_a_camera_whose_codec_stays_unknown_is_never_restarted(monkeypatch, capsys, retry_seconds):
    """Red's review (a): the probe keeps failing on a healthy, encoding
    camera -- no copy upgrade, no restart, at most one probe per retry window."""
    monkeypatch.setattr(main, "camera_url", lambda n: URL_NEW)
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY_PROBE_RETRY_SECONDS", retry_seconds)
    probe = _Probe(lambda u: None)
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    started = time.monotonic()
    starts = _run_supervisor(monkeypatch, lambda i: True, lambda s: False, timeout=1.0)
    elapsed = time.monotonic() - started
    assert starts == ["encode"]  # one worker, never restarted
    assert len(probe.urls) <= 1 + int(elapsed / retry_seconds) + 1
    assert main.camera_reconnect_counts[2] == 0
    assert "restarting the live worker with stream copy" not in capsys.readouterr().out


def test_a_genuine_copy_fallback_survives_recovery_without_a_restart_loop(monkeypatch):
    monkeypatch.setattr(main, "camera_url", lambda n: URL_NEW)
    probe = _Probe(lambda u: "h264")
    monkeypatch.setattr(main, "probe_camera_video_codec", probe)
    main._live_copy_probe_url[2] = main._stream_address(URL_NEW)
    main._live_copy_probe[2] = (time.monotonic(), "h264")
    main._live_copy_disabled_until[2] = time.monotonic() + 3600
    starts = _run_supervisor(monkeypatch, lambda i: i >= 2, lambda s: False, timeout=1.0)
    assert starts == ["encode", "encode", "encode"]  # stays on encode, one healthy worker, no flapping


# --------------------------------------------------------------- motion reader
class _Reader:
    def __init__(self, kind):
        self.kind, self.returncode = kind, None
        self.stdout = asyncio.StreamReader()
        self.pid = id(self)

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15
            self.stdout.feed_eof()

    kill = terminate

    async def wait(self):
        return self.returncode


def test_a_motion_reader_on_the_hls_fallback_re_chooses_its_source_on_recovery(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0.001)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(main, "camera_in_service", lambda n: True)
    monkeypatch.setattr(main, "CAMERA_IN_SERVICE_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(main, "motion_substream_url", lambda n: "rtsp://u:p@10.0.0.9/sub")
    monkeypatch.setattr(main, "get_event_settings", lambda n: type("S", (), {"enabled": False})())
    (main.HLS_FOLDER / "camera3.m3u8").write_text("#EXTM3U\n")
    main._motion_source_selector._hls_until[3] = time.monotonic() + 1800  # after the outage
    readers = []

    async def spawn(*command, **kwargs):
        reader = _Reader("substream" if "rtsp://u:p@10.0.0.9/sub" in command else "hls")
        readers.append(reader)

        async def feed():
            while reader.returncode is None:
                reader.stdout.feed_data(bytes(160 * 90))
                await real_sleep(0.002)
        asyncio.get_running_loop().create_task(feed())
        return reader

    monkeypatch.setattr(main, "spawn_process_cancellation_safe", spawn)

    async def go():
        task = asyncio.create_task(main._motion_detector_loop(3, "test"))
        while not readers:
            await real_sleep(0.005)
        await real_sleep(0.05)
        assert [r.kind for r in readers] == ["hls"]
        main.note_live_stream_recovered(3)  # the live worker sees video again
        deadline = time.monotonic() + 5
        while len(readers) < 2 and time.monotonic() < deadline:
            await real_sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for r in readers:
            r.terminate()

    asyncio.run(go())
    assert [r.kind for r in readers][:2] == ["hls", "substream"]
    assert readers[0].returncode is not None  # the HLS reader was stopped, not leaked
