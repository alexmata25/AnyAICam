"""Windows 1.3.2 corrections (2026-10-21, Blue) for the functional Sandbox defects D1/D2 and observation O3.

D2: on Windows an FFmpeg reader of the live camera<N>.m3u8 freezes the live encoder's playlist (its rename over the
open file fails silently), so the motion detector got no frames and every live viewer an empty playlist. On Windows
the motion and AI frame readers therefore never read the playlist (windows=True below). D1: /static/hls is served
from HLS_FOLDER when that folder is outside the static folder (Windows). O3: MoQ off in the Windows MediaMTX config.
The Linux contract (windows=False) is pinned here too; the suite's existing tests keep describing it."""
from pathlib import Path

import pytest

import motion_source
import webrtc_publisher

MAIN = "rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101"
SUB = "rtsp://user:pass@192.168.1.64:554/Streaming/Channels/102"


# ---------------------------------------------------------------- motion reader URL
def test_windows_single_stream_camera_is_read_over_its_main_stream():
    assert motion_source.reader_url(MAIN, None, None, windows=True) == MAIN


def test_windows_camera_with_a_distinct_substream_still_uses_the_substream():
    assert motion_source.reader_url(MAIN, None, lambda url: [SUB], windows=True) == SUB


def test_linux_single_stream_camera_keeps_the_hls_contract():
    assert motion_source.reader_url(MAIN, None, None, windows=False) is None
    assert motion_source.reader_url(MAIN, None, lambda url: [SUB], windows=False) == SUB


def test_no_camera_stream_means_no_reader_url_on_either_platform():
    assert motion_source.reader_url("", None, None, windows=True) is None
    assert motion_source.reader_url("", None, None, windows=False) is None


# ---------------------------------------------------------------- motion source selector
@pytest.mark.parametrize("mode", ["auto", "substream", "hls"])
def test_windows_selector_never_chooses_the_hls_playlist(mode):
    selector = motion_source.MotionSourceSelector(mode, windows=True)
    assert selector.mode in ("auto", "substream")  # "hls" means "auto" on Windows
    assert selector.choose(1, lambda n: MAIN) == ("substream", MAIN)
    assert selector.choose(1, lambda n: None) == ("wait", None)


def test_windows_selector_keeps_retrying_the_camera_stream_after_failures():
    now = [1000.0]
    selector = motion_source.MotionSourceSelector("auto", windows=True, max_failures=3, clock=lambda: now[0])
    messages = [selector.note_run(1, "substream", frames=0, stalled=True) for _ in range(3)]
    assert messages[:2] == [None, None]
    assert messages[2] and "retrying" in messages[2] and "never read" in messages[2]
    assert "rtsp" not in messages[2].lower()  # never a URL in a log line
    assert selector.choose(1, lambda n: MAIN) == ("substream", MAIN)  # no HLS window was opened


def test_linux_selector_still_falls_back_to_hls_after_failures():
    now = [1000.0]
    selector = motion_source.MotionSourceSelector("auto", windows=False, max_failures=3, clock=lambda: now[0])
    for _ in range(3):
        selector.note_run(1, "substream", frames=0, stalled=True)
    assert selector.choose(1, lambda n: MAIN) == ("hls", None)
    assert motion_source.MotionSourceSelector("auto", windows=False).choose(1, lambda n: None) == ("hls", None)


def test_windows_selector_messages_name_the_source_without_a_url():
    selector = motion_source.MotionSourceSelector("auto", windows=True)
    assert "none yet" in selector.note_kind(1, "wait")
    message = selector.note_kind(1, "substream")
    assert "camera stream" in message and "not read on Windows" in message and "rtsp://" not in message


def test_the_reader_command_for_a_main_stream_is_the_bounded_rtsp_reader():
    command = motion_source.substream_command(MAIN)
    assert command[:5] == ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp"]
    assert "-vf" in command and motion_source.MOTION_FRAME_FILTER in command and command[-1] == "pipe:1"
    assert not any(part.endswith(".m3u8") for part in command)


# ---------------------------------------------------------------- MediaMTX MoQ (O3)
def test_windows_mediamtx_config_switches_moq_off_and_keeps_webrtc():
    lines = webrtc_publisher.render_mediamtx_config(windows=True).splitlines()
    assert "moq: no" in lines
    for expected in ("webrtc: yes", "webrtcAddress: 127.0.0.1:8889", "webrtcLocalUDPAddress: :8189", "api: yes"):
        assert expected in lines


def test_linux_mediamtx_config_is_unchanged():
    assert "moq" not in webrtc_publisher.render_mediamtx_config(windows=False)


# ---------------------------------------------------------------- /static/hls (D1)
def test_hls_folder_outside_static_detection(tmp_path):
    import main
    static = tmp_path / "app" / "static"
    assert main.hls_folder_outside_static(static / "hls", static) is False            # Linux layout: unchanged
    assert main.hls_folder_outside_static(tmp_path / "ProgramData" / "hls", static) is True   # Windows layout


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows paths are case-insensitive")
def test_hls_folder_inside_static_is_recognised_case_insensitively_on_windows(tmp_path):
    import main
    static = tmp_path / "App" / "Static"
    assert main.hls_folder_outside_static(Path(str(static).lower()) / "hls", static) is False


def test_static_hls_is_served_from_the_hls_folder_when_it_is_outside_static():
    import main
    from fastapi.testclient import TestClient
    outside = main.hls_folder_outside_static(main.HLS_FOLDER, main.STATIC_FOLDER)
    mounted = [route for route in main.app.routes if getattr(route, "name", None) == "static-hls"]
    assert bool(mounted) == outside
    if not outside:
        pytest.skip("this test host keeps HLS inside static (the Linux layout): nothing to serve differently")
    Path(main.HLS_FOLDER).mkdir(parents=True, exist_ok=True)
    playlist = Path(main.HLS_FOLDER) / "camera97.m3u8"
    segment = Path(main.HLS_FOLDER) / "camera97_000000001.ts"
    playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\ncamera97_000000001.ts\n", encoding="utf-8")
    segment.write_bytes(b"\x47" * 188)
    try:
        client = TestClient(main.app)
        answer = client.get("/static/hls/camera97.m3u8")
        assert answer.status_code == 200 and "camera97_000000001.ts" in answer.text
        assert answer.headers.get("CDN-Cache-Control") == "no-store"   # the HLS headers still apply
        assert client.get("/static/hls/camera97_000000001.ts").content == b"\x47" * 188
        assert client.get("/static/hls/..%2F..%2Fdatabase%2Fpartner_portal.db").status_code == 404
        assert client.get("/static/hls/../vms.env").status_code == 404
    finally:
        playlist.unlink(missing_ok=True)
        segment.unlink(missing_ok=True)
