"""Windows 1.3.2 corrections (2026-10-21, Blue): integration on a real Windows host with the bundled FFmpeg/MediaMTX.

Opt-in (Windows only): ANYAICAM_TEST_FFMPEG_DIR = folder with ffmpeg.exe, ANYAICAM_TEST_MEDIAMTX = mediamtx.exe (the
setup's vendored builds). A synthetic camera (a moving box, H.264) is published by a separate MediaMTX on 127.0.0.5;
the VMS's exact live-HLS encoder and recording commands run next to a motion reader, and the live playlist is watched.

* characterization (the root cause, functional Sandbox D2): FFmpeg's HLS reader on the live playlist freezes it;
* the correction: the motion reader on the camera's RTSP stream (motion_source.substream_command, what reader_url()
  hands it on Windows) keeps the playlist fresh, gets frames with motion, recording keeps growing, and short HTTP-style
  reads of the playlist (D1's /static/hls) do not freeze it either;
* O3: MediaMTX with the generated Windows config has no MoQ listener, and its WebRTC server, API and a camera path work.
"""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import motion_source
import webrtc_publisher

FFMPEG_DIR = os.environ.get("ANYAICAM_TEST_FFMPEG_DIR", "")
MEDIAMTX = os.environ.get("ANYAICAM_TEST_MEDIAMTX", "")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not (FFMPEG_DIR and Path(FFMPEG_DIR, "ffmpeg.exe").is_file() and MEDIAMTX and Path(MEDIAMTX).is_file()),
    reason="Windows integration: set ANYAICAM_TEST_FFMPEG_DIR and ANYAICAM_TEST_MEDIAMTX to the bundled builds")

FFMPEG = str(Path(FFMPEG_DIR, "ffmpeg.exe"))
CAMERA = "rtsp://127.0.0.5:18554/cam1"
APP = Path(__file__).resolve().parents[1]
FRAME = 160 * 90


def _free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((host, port)) != 0


@pytest.fixture(scope="module")
def camera(tmp_path_factory):
    folder = tmp_path_factory.mktemp("camera")
    config = folder / "camera.yml"
    config.write_text("\n".join(["logLevel: warn", "api: no", "metrics: no", "pprof: no", "playback: no", "rtsp: yes",
                                 "rtspAddress: 127.0.0.5:18554", "rtspTransports: [tcp]", "rtmp: no", "hls: no",
                                 "webrtc: no", "srt: no", "moq: no", "paths:", "  cam1:"]) + "\n", encoding="ascii")
    server = subprocess.Popen([MEDIAMTX, str(config)], cwd=str(folder), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    publisher = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10",
                                  "-f", "lavfi", "-i", "color=c=white:s=240x180:r=10", "-filter_complex",
                                  "[0][1]overlay=x='if(lt(mod(t,12),6),60+150*mod(t,12),960)':y=270:shortest=0",
                                  "-pix_fmt", "yuv420p", "-g", "20", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                                  "-f", "rtsp", "-rtsp_transport", "tcp", CAMERA], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    assert server.poll() is None and publisher.poll() is None, "synthetic camera did not start"
    yield CAMERA
    for p in (publisher, server):
        p.kill()


def _live_encoder(folder: Path):
    """The VMS's live HLS command (main.py start_live_stream), with the camera URL and folders of this test."""
    return subprocess.Popen([FFMPEG, "-rtsp_transport", "tcp", "-i", CAMERA, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                             "-preset", "veryfast", "-tune", "zerolatency", "-g", "56", "-keyint_min", "28", "-sc_threshold", "0",
                             "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
                             "-maxrate", "800k", "-bufsize", "1600k", "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000",
                             "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                             "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
                             "-hls_segment_filename", str(folder / "camera1_%09d.ts"), str(folder / "camera1.m3u8")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _recorder(folder: Path):
    """The VMS's continuous recording command (main.py, segment muxer, stream copy)."""
    return subprocess.Popen([FFMPEG, "-rtsp_transport", "tcp", "-i", CAMERA, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
                             "-c:a", "aac", "-b:a", "96k", "-f", "segment", "-segment_time", "300", "-reset_timestamps", "1",
                             "-strftime", "1", str(folder / "camera1_%Y-%m-%d_%H-%M-%S.mkv")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _true_size(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            return handle.tell()
    except OSError:
        return -1


def _run(tmp_path: Path, reader_command, seconds: int = 40):
    hls, recordings = tmp_path / "hls", tmp_path / "recordings"
    hls.mkdir()
    recordings.mkdir()
    live, recorder = _live_encoder(hls), _recorder(recordings)
    playlist = hls / "camera1.m3u8"
    deadline = time.time() + 30
    while not playlist.exists() and time.time() < deadline:
        time.sleep(0.5)
    assert playlist.exists(), "the live encoder wrote no playlist"
    command = reader_command(str(playlist))
    reader = subprocess.Popen([FFMPEG] + command[1:], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames, ages, http_failures = [], [], [0]
    stop = threading.Event()

    def read():
        while not stop.is_set():
            chunk = reader.stdout.read(FRAME)
            if not chunk or len(chunk) < FRAME:
                return
            frames.append(chunk)

    def http_like_reads():   # what an HTTP route / StaticFiles does per request (D1)
        while not stop.is_set():
            try:
                playlist.read_bytes()
            except OSError:
                http_failures[0] += 1
            time.sleep(0.5)

    threads = [threading.Thread(target=read, daemon=True), threading.Thread(target=http_like_reads, daemon=True)]
    for t in threads:
        t.start()
    first_mkv_size = None
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(5)
        ages.append(round(time.time() - os.stat(playlist).st_mtime, 1))
        mkv = sorted(recordings.glob("*.mkv"))
        if mkv and first_mkv_size is None:
            first_mkv_size = _true_size(mkv[-1])
    mkv = sorted(recordings.glob("*.mkv"))
    last_mkv_size = _true_size(mkv[-1]) if mkv else -1
    stop.set()
    for p in (reader, live, recorder):
        p.kill()
    time.sleep(1)
    motion = 0
    for a, b in zip(frames, frames[1:]):
        d = [abs(x - y) for x, y in zip(a, b)]
        if sum(d) / FRAME >= 3.6 and 0.02 <= sum(1 for v in d if v >= 20) / FRAME <= 0.85:
            motion += 1
    return {"frames": len(frames), "motion_pairs": motion, "playlist_ages": ages, "mkv": (first_mkv_size, last_mkv_size),
            "http_failures": http_failures[0]}


def test_the_mirrored_commands_match_the_vms_live_and_recording_flags():
    source = (APP / "main.py").read_text(encoding="utf-8")
    for flag in ('"-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments"', '"-hls_time", "2"',
                 '"-f", "segment", "-segment_time", "300"'):
        assert flag in source, flag


def test_characterization_ffmpeg_reading_the_live_playlist_freezes_it_on_windows(camera, tmp_path):
    result = _run(tmp_path, motion_source.hls_command, seconds=30)
    assert max(result["playlist_ages"]) > 10, result      # root cause of D2 (and the empty live playlist)
    assert result["frames"] < 15, result


def test_the_windows_rtsp_motion_reader_keeps_live_view_and_recording_working(camera, tmp_path):
    url = motion_source.reader_url(CAMERA, None, None, windows=True)
    assert url == CAMERA
    result = _run(tmp_path, lambda playlist: motion_source.substream_command(url), seconds=40)
    assert max(result["playlist_ages"]) < 8, result        # the live playlist keeps advancing (no freeze)
    assert result["frames"] >= 30, result                  # ~1 frame per second for the detector
    assert result["motion_pairs"] >= 3, result             # the moving box is detectable
    first, last = result["mkv"]
    assert first is not None and last > max(first, 0), result  # recording keeps growing meanwhile
    assert result["http_failures"] == 0, result            # short HTTP-style playlist reads never fail


def _listeners(pid: int) -> set:
    import psutil
    found = set()
    for c in psutil.Process(pid).net_connections(kind="inet"):
        if c.laddr and (c.status == psutil.CONN_LISTEN or c.type == socket.SOCK_DGRAM):
            found.add(("tcp" if c.type == socket.SOCK_STREAM else "udp", c.laddr.port))
    return found


@pytest.mark.parametrize("windows", [True, False])
def test_generated_mediamtx_config_moq_off_on_windows_and_webrtc_still_serves(camera, tmp_path, windows):
    for host, port in (("127.0.0.1", 8554), ("127.0.0.1", 8889), ("127.0.0.1", 9997)):
        if not _free(host, port):
            pytest.skip(f"{host}:{port} is busy on this host")
    config = tmp_path / "mediamtx.yml"
    config.write_text(webrtc_publisher.render_mediamtx_config(windows=windows), encoding="ascii")
    # cwd: with MoQ on, MediaMTX writes auto.crt/auto.key into its working folder -- keep them in tmp_path.
    server = subprocess.Popen([MEDIAMTX, str(config)], cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        assert server.poll() is None, "MediaMTX refused the generated config"
        ports = _listeners(server.pid)
        if windows:
            assert ("tcp", 8892) not in ports and ("udp", 8892) not in ports and ("udp", 8893) not in ports, ports
        else:
            assert ("tcp", 8892) in ports, ports                # MediaMTX 1.21's default MoQ (what O3 removes on Windows)
        assert ("tcp", 8889) in ports and ("udp", 8189) in ports and ("tcp", 9997) in ports, ports
        body = json.dumps({"source": CAMERA}).encode()
        add = urllib.request.Request("http://127.0.0.1:9997/v3/config/paths/add/cam-x", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(add, timeout=10).status == 200
        ready = False
        for _ in range(20):
            state = json.loads(urllib.request.urlopen("http://127.0.0.1:9997/v3/paths/get/cam-x", timeout=10).read())
            if state.get("ready"):
                ready = True
                break
            time.sleep(1)
        assert ready, "the camera path never became ready"
        options = urllib.request.Request("http://127.0.0.1:8889/cam-x/whep", method="OPTIONS")
        answer = urllib.request.urlopen(options, timeout=10)
        assert answer.status in (200, 204)                      # the WHEP endpoint of the live path answers
    finally:
        server.kill()
