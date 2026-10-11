"""End-to-end: H.265-only camera -> on-demand H.264 transcode -> real WHEP answer.

Opt-in (needs Linux, the real MediaMTX binary and ffmpeg with libx264 and
libx265): set ANYAICAM_MEDIAMTX_INTEGRATION=1 and ANYAICAM_MEDIAMTX_BINARY.
Reproduces the 2026-10-10 Samsung failure with a simulated H.265 camera and
the browser's real (re-keyed) Chrome WHEP offer, then proves the fix: the
transcoded path negotiates H.264, starts on demand, stops after the viewer
leaves, restarts after a transcoder crash, and recovers after a camera
outage. Uses webrtc_publisher's own config rendering, path config and WHEP
client -- no hand-written equivalents.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_MEDIAMTX_INTEGRATION") != "1"
    or not Path(os.environ.get("ANYAICAM_MEDIAMTX_BINARY", "/nonexistent")).is_file()
    or shutil.which("ffmpeg") is None,
    reason="real MediaMTX + ffmpeg integration test (opt-in)")

OFFER = (Path(__file__).parent / "chrome_whep_offer.sdp").read_text(encoding="utf-8")
CAMERA = "rtsp://127.0.0.1:8554/fakecam"  # stands in for the camera's H.265 substream


def _api(path):
    with urllib.request.urlopen("http://127.0.0.1:9997" + path, timeout=5) as r:
        return json.load(r)


def _ready(path):
    """A path MediaMTX created for a publisher disappears (404) when it leaves."""
    try:
        return bool(_api(f"/v3/paths/get/{path}").get("ready"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def _wait(predicate, timeout, step=0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def _transcoders():
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return [line.split(None, 1)[0] for line in out.splitlines() if "libx264" in line and "8554/cam-x" in line]


def _cpu_seconds(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    import webrtc_publisher as wp
    monkeypatch.setattr(wp, "LAN_ADDRESSES_FILE", tmp_path / "none.json", raising=False)
    config = tmp_path / "mediamtx.yml"
    config.write_text(wp.render_mediamtx_config())
    mtx = subprocess.Popen([os.environ["ANYAICAM_MEDIAMTX_BINARY"], str(config)],
                           stdout=open(tmp_path / "mediamtx.log", "w"), stderr=subprocess.STDOUT)
    assert _wait(lambda: _api("/v3/paths/list") is not None, 15)
    def start_camera():
        return subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
                                 "-i", "testsrc2=size=640x360:rate=20", "-c:v", "libx265", "-preset", "ultrafast",
                                 "-x265-params", "keyint=40:log-level=error", "-f", "rtsp", "-rtsp_transport", "tcp", CAMERA])
    camera = start_camera()
    assert _wait(lambda: _ready("fakecam"), 20)
    state = {"wp": wp, "camera": camera, "start_camera": start_camera, "log": tmp_path / "mediamtx.log"}
    yield state
    for proc in (state["camera"], mtx):
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_samsung_failure_reproduces_on_the_raw_h265_path(stack):
    assert _api("/v3/paths/get/fakecam")["tracks"] == ["H265"]
    req = urllib.request.Request("http://127.0.0.1:8889/fakecam/whep", data=OFFER.encode(), method="POST",
                                 headers={"Content-Type": "application/sdp"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 400 and "codecs not supported" in err.value.read().decode()


def test_transcoded_path_negotiates_h264_on_demand_and_cleans_up(stack):
    wp = stack["wp"]
    status, _ = wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(CAMERA, "transcode"))
    assert status in (200, 201)
    assert _transcoders() == []  # nothing runs until a viewer asks

    started = time.monotonic()
    result = wp.whep_offer("cam-x", OFFER)
    time_to_answer = time.monotonic() - started
    assert result is not None, stack["log"].read_text()[-2000:]
    location, answer = result
    assert "H264/90000" in answer and "H265" not in answer and "a=sendonly" in answer
    assert _api("/v3/paths/get/cam-x")["tracks"] == ["H264"]
    pids = _transcoders()
    assert len(pids) == 1
    assert time_to_answer < 12, time_to_answer  # on-demand start + first keyframe

    cpu_before = _cpu_seconds(pids[0])
    time.sleep(5)
    cpu_share = (_cpu_seconds(pids[0]) - cpu_before) / 5
    print(f"PERF time_to_answer_s={time_to_answer:.2f} transcoder_cpu_share={cpu_share:.2f} (1.0 = one core)")
    assert cpu_share < 1.5  # 640x360 H.265 -> H.264 ultrafast must stay well under two cores

    with urllib.request.urlopen(urllib.request.Request(location, method="DELETE"), timeout=5) as r:
        assert r.status == 200
    # runOnDemandCloseAfter=10s after the last reader leaves
    assert _wait(lambda: _transcoders() == [], 25), "transcoder still running after the viewer left"
    assert _wait(lambda: not _ready("cam-x"), 10)


def test_transcoder_crash_is_restarted_while_a_viewer_is_connected(stack):
    wp = stack["wp"]
    wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(CAMERA, "transcode"))
    assert wp.whep_offer("cam-x", OFFER) is not None
    first = _transcoders()
    assert len(first) == 1
    subprocess.run(["kill", "-9", first[0]])
    # runOnDemandRestart=yes: MediaMTX restarts the command while the path has readers
    assert _wait(lambda: _transcoders() not in ([], first), 15), "transcoder was not restarted"
    assert wp.whep_offer("cam-x", OFFER) is not None


def test_camera_outage_then_recovery(stack):
    wp = stack["wp"]
    wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(CAMERA, "transcode"))
    stack["camera"].terminate()
    stack["camera"].wait(5)
    assert _wait(lambda: not _ready("fakecam"), 10)
    assert wp.whep_offer("cam-x", OFFER) is None  # no source: rejected, no hang beyond the start timeout
    stack["camera"] = stack["start_camera"]()
    assert _wait(lambda: _ready("fakecam"), 20)
    assert _wait(lambda: wp.whep_offer("cam-x", OFFER) is not None, 40, step=2), "no recovery after the camera returned"


# ------------------------------------------------------------------ selection with REAL ffprobe, per camera type

MAIN_URL = "rtsp://127.0.0.1:8554/Streaming/Channels/101"
SUB_URL = "rtsp://127.0.0.1:8554/Streaming/Channels/102"


def _publish(path, codec, size):
    encoder = {"h264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "40"],
               "hevc": ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "keyint=40:log-level=error"]}[codec]
    return subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=20",
                             *encoder, "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:8554/{path}"])


@pytest.fixture()
def camera_streams(stack, monkeypatch):
    """A simulated camera at Streaming/Channels/101 (main) and /102 (sub) with chosen codecs."""
    wp = stack["wp"]
    procs = []
    monkeypatch.setattr(wp, "_p2p_source_choice", {})
    monkeypatch.setattr(wp, "_codec_revalidated_at", {})
    monkeypatch.setattr(wp, "P2P_STREAM_PREFERENCE", "auto")
    monkeypatch.setattr(wp, "_load_capabilities", lambda camera_id: None)
    monkeypatch.setattr(wp, "_save_capabilities", lambda camera_id, record: None)
    monkeypatch.setattr(wp, "_onvif_soap_call", lambda: None)
    def start(main_codec, sub_codec):
        if main_codec:
            procs.append(_publish("Streaming/Channels/101", main_codec, "1280x720"))
        if sub_codec:
            procs.append(_publish("Streaming/Channels/102", sub_codec, "640x360"))
        for path, codec in (("Streaming/Channels/101", main_codec), ("Streaming/Channels/102", sub_codec)):
            if codec:
                assert _wait(lambda: _ready(path), 20)
    yield start
    for proc in procs:
        proc.terminate()


@pytest.mark.parametrize("main_codec,sub_codec,expected_kind,expected_url,transcoder", [
    ("h264", "h264", "substream", SUB_URL, False),   # H.264-only camera
    ("h264", "hevc", "main", MAIN_URL, False),       # mixed: H.265 substream, H.264 main
    ("hevc", "hevc", "transcode", SUB_URL, True),    # H.265-only (the Samsung camera)
])
def test_observed_codec_selection_end_to_end(stack, camera_streams, main_codec, sub_codec, expected_kind, expected_url, transcoder):
    wp = stack["wp"]
    camera_streams(main_codec, sub_codec)
    source, kind = wp.p2p_source_for("cam-x", MAIN_URL)  # real ffprobe against the live streams
    assert (source, kind) == (expected_url, expected_kind)
    status, _ = wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(source, kind))
    assert status in (200, 201)
    result = wp.whep_offer("cam-x", OFFER)
    assert result is not None, stack["log"].read_text()[-1500:]
    assert "H264/90000" in result[1]
    assert bool(_transcoders()) is transcoder


def test_disconnected_camera_is_not_mislabelled_and_fails_bounded(stack, camera_streams):
    wp = stack["wp"]
    camera_streams(None, None)  # nothing publishing: the camera is offline
    source, kind = wp.p2p_source_for("cam-x", MAIN_URL)
    assert (source, kind) == (MAIN_URL, "main")  # unreachable is not "incompatible": no transcoder is configured
    wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(source, kind))
    started = time.monotonic()
    assert wp.whep_offer("cam-x", OFFER) is None
    assert time.monotonic() - started < 15  # bounded by MediaMTX's 10 s source start timeout


# ------------------------------------------------------------------ a real WebRTC client decodes the H.264 frames

def test_real_webrtc_client_receives_and_decodes_transcoded_h264(stack):
    aiortc = pytest.importorskip("aiortc")
    import asyncio
    wp = stack["wp"]
    wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(CAMERA, "transcode"))

    async def watch():
        pc = aiortc.RTCPeerConnection(aiortc.RTCConfiguration(iceServers=[]))  # host candidates only, no external STUN
        pc.addTransceiver("video", direction="recvonly")
        frames = []
        done = asyncio.Event()

        @pc.on("track")
        def on_track(track):
            async def pull():
                try:
                    while len(frames) < 30:
                        frame = await track.recv()
                        frames.append(frame)
                finally:
                    done.set()
            asyncio.ensure_future(pull())

        await pc.setLocalDescription(await pc.createOffer())
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        result = await asyncio.to_thread(wp.whep_offer, "cam-x", pc.localDescription.sdp)
        assert result is not None, stack["log"].read_text()[-1500:]
        location, answer = result
        await pc.setRemoteDescription(aiortc.RTCSessionDescription(sdp=answer, type="answer"))
        await asyncio.wait_for(done.wait(), 30)
        await pc.close()
        video = answer.split("m=video", 1)[1].split("\nm=", 1)[0]
        codecs = sorted({line.split()[1].split("/")[0] for line in video.splitlines() if line.startswith("a=rtpmap:")})
        return frames, codecs, location

    frames, codecs, location = asyncio.run(watch())
    assert len(frames) >= 30
    widths = {f.width for f in frames}
    assert widths == {640}  # the 640x360 substream, never upscaled
    luma = [f.to_ndarray(format="gray") for f in frames[-5:]]
    assert all(float(img.std()) > 10 for img in luma)  # real picture content, not a black/flat frame
    assert any(float(abs(luma[i].astype(int) - luma[i + 1].astype(int)).mean()) > 0.5 for i in range(4))  # moving
    assert codecs and all(c.upper() in ("H264", "RTX") for c in codecs)  # the only video codec negotiated is H.264
    print(f"PLAYABLE frames={len(frames)} size={frames[-1].width}x{frames[-1].height} codec={codecs}")
    # viewer gone -> transcoder stops after runOnDemandCloseAfter
    assert _wait(lambda: _transcoders() == [], 30)


def test_sustained_camera_outage_with_a_viewer_is_rate_limited_and_cleaned_up(stack):
    """Codex review 2026-10-10: restart behaviour while the camera stays down."""
    aiortc = pytest.importorskip("aiortc")
    import asyncio
    wp = stack["wp"]
    wp._config_request("POST", "/v3/config/paths/add/cam-x", wp.path_config_for(CAMERA, "transcode"))

    async def outage():
        pc = aiortc.RTCPeerConnection(aiortc.RTCConfiguration(iceServers=[]))
        pc.addTransceiver("video", direction="recvonly")
        await pc.setLocalDescription(await pc.createOffer())
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        result = await asyncio.to_thread(wp.whep_offer, "cam-x", pc.localDescription.sdp)
        assert result is not None
        await pc.setRemoteDescription(aiortc.RTCSessionDescription(sdp=result[1], type="answer"))
        await asyncio.sleep(3)  # connected and receiving
        stack["camera"].terminate()  # camera goes down while the viewer stays connected
        seen, samples = set(), 0
        end = time.monotonic() + 30
        while time.monotonic() < end:
            seen.update(_transcoders())
            samples += 1
            await asyncio.sleep(0.5)
        await pc.close()
        return seen
    seen = asyncio.run(outage())
    print(f"OUTAGE transcoder_processes_in_30s={len(seen)}")
    assert len(seen) <= 10  # MediaMTX pauses between restarts: no restart storm
    assert _wait(lambda: _transcoders() == [], 40), "transcoder left running after the viewer left during an outage"
