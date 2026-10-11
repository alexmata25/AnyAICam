"""video-pipeline P1 (2026-10-11): measured camera resolution and the
per-camera processing profile.

Covers video_profile.py (class/shape/box/fit/profile, safe defaults),
camera_video_probe.py (ffprobe parsing, measurement into the capability
record without changing what P2P selects, re-probe on age / URL change,
changed resolution, the credential-free status file), the live encode
scale, the appliance agent's report and the cloud's measured_* columns
(never cameras.resolution).

Where ffmpeg is installed, the scale filter is also run for real on
synthetic sources of each size and the output size is checked."""
import json
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

import camera_capabilities
import camera_video_probe
import video_profile

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SECRET = "pa:ss@w0rd/x"
MAIN = f"rtsp://admin:{SECRET.replace('/', '%2F').replace('@', '%40').replace(':', '%3A')}@192.0.2.20:554/Streaming/Channels/101"
CLEAN_MAIN = "rtsp://192.0.2.20:554/Streaming/Channels/101"
CLEAN_SUB = "rtsp://192.0.2.20:554/Streaming/Channels/102"


def _record(main=(None, None, None), sub=None, sub_kind="onvif_media", measured_main=None, measured_sub=None):
    streams = [{"id": "m", "uri": CLEAN_MAIN, "role": "main", "codec": main[2], "width": main[0], "height": main[1],
                "fps": 20, "discovered_by": "onvif_media", "verified": None}]
    if measured_main:
        streams[0]["measured"] = dict(measured_main, by="ffprobe", at=NOW.isoformat())
    if sub is not None:
        stream = {"id": "s", "uri": CLEAN_SUB, "role": "sub", "codec": sub[2], "width": sub[0], "height": sub[1],
                  "fps": 15, "discovered_by": sub_kind, "verified": None}
        if measured_sub:
            stream["measured"] = dict(measured_sub, by="ffprobe", at=NOW.isoformat())
        streams.append(stream)
    return {"schema": 1, "probed_at": NOW.isoformat(), "adapters": ["onvif_media"], "streams": streams}


# ----------------------------------------------------------------- classes

@pytest.mark.parametrize("width,height,klass,shape,clip,live", [
    (1920, 1080, "2mp", "landscape", (1920, 1080), (1920, 1080)),
    (2560, 1440, "4mp", "landscape", (1920, 1080), (1920, 1080)),
    (2592, 1944, "5mp", "landscape", (1440, 1080), (1440, 1080)),
    (3840, 2160, "8mp", "landscape", (1920, 1080), (1920, 1080)),
    (4000, 3000, "other", "landscape", (1440, 1080), (1440, 1080)),
    (1280, 720, "2mp", "landscape", (1280, 720), (1280, 720)),       # never upscaled
    (2880, 2880, "8mp", "square", (1440, 1440), (1440, 1440)),        # fisheye
    (1080, 1920, "2mp", "portrait", (1080, 1920), (1080, 1920)),      # corridor mode
    (2160, 3840, "8mp", "portrait", (1080, 1920), (1080, 1920)),
    (5120, 1440, "8mp", "panoramic", (2560, 720), (2560, 720)),
    (704, 576, "2mp", "landscape", (704, 576), (704, 576)),           # odd D1 aspect
])
def test_profile_from_measured_geometry(width, height, klass, shape, clip, live):
    profile = video_profile.build(_record(main=(width, height, "H265")))
    assert profile.measured is True
    assert profile.resolution_class == klass
    assert profile.shape == shape
    assert profile.clip_size == clip
    assert profile.live_size == live
    assert all(v % 2 == 0 for v in profile.clip_size)
    assert video_profile.CLIP_MIN_KBPS <= profile.clip_max_kbps <= video_profile.CLIP_MAX_KBPS


def test_measured_values_win_over_onvif_and_sales_label_is_never_used():
    # ONVIF says 1080p, ffprobe measured 4K: the measurement wins.
    record = _record(main=(1920, 1080, "H264"), measured_main={"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0})
    record["resolution"] = "2mp"  # a sales label anywhere in the record is irrelevant
    profile = video_profile.build(record)
    assert profile.source["width"] == 3840 and profile.source["codec"] == "H265"
    assert profile.resolution_class == "8mp"
    assert profile.source["measured_by"] == "ffprobe"


@pytest.mark.parametrize("record", [None, {}, {"streams": "garbage"}, {"streams": [None, 7, "x"]},
                                    _record(main=(None, None, None)), _record(main=(0, -5, None))])
def test_missing_or_failed_probe_gets_safe_defaults(record):
    profile = video_profile.build(record)
    assert profile.measured is False
    assert profile.resolution_class == "unknown"
    assert profile.clip_size is None and profile.live_size is None
    # The filter only ever downscales and keeps the aspect ratio.
    assert profile.clip_filter().startswith("scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease")
    assert "format=yuv420p" in profile.clip_filter()
    summary = profile.summary()
    json.dumps(summary)
    assert summary["resolution_class"] == "unknown"


def test_analytics_stream_is_smallest_sub_at_least_640_wide_else_main():
    four_k = _record(main=(3840, 2160, "H265"), sub=(640, 360, "H265"))
    profile = video_profile.build(four_k)
    assert profile.analytics_uri == CLEAN_SUB and not profile.analytics_is_main
    assert profile.analytics_size == (640, 360)
    tiny_sub = _record(main=(1920, 1080, "H264"), sub=(352, 240, "H264"))
    assert video_profile.build(tiny_sub).analytics_is_main
    assert video_profile.build(tiny_sub).analytics_size == (1280, 720)   # main scaled to the analytics box
    # A guessed (URL-convention) sub becomes usable once measured.
    guessed = _record(main=(3840, 2160, "H265"), sub=(None, None, None), sub_kind="url_convention",
                      measured_sub={"width": 640, "height": 360, "codec": "H265", "fps": 15.0})
    assert video_profile.build(guessed).analytics_uri == CLEAN_SUB
    broken = _record(main=(3840, 2160, "H265"), sub=(640, 360, "H265"))
    broken["streams"][1]["verified"] = False
    assert video_profile.build(broken).analytics_is_main


def test_thumbnail_scale_never_upscales():
    assert video_profile.thumbnail_scale(3840) == pytest.approx(1280 / 3840)
    assert video_profile.thumbnail_scale(640) == 1.0
    assert video_profile.thumbnail_scale(None) == 1.0


# ----------------------------------------------------------------- ffprobe / measure

def test_parse_ffprobe_hevc_fraction_fps():
    out = json.dumps({"streams": [{"codec_name": "hevc", "width": 3840, "height": 2160, "avg_frame_rate": "20/1", "r_frame_rate": "20/1"}]})
    assert camera_video_probe.parse_ffprobe(out) == {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}
    out = json.dumps({"streams": [{"codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "0/0", "r_frame_rate": "30000/1001"}]})
    assert camera_video_probe.parse_ffprobe(out)["fps"] == 29.97
    assert camera_video_probe.parse_ffprobe("not json") is None
    assert camera_video_probe.parse_ffprobe(json.dumps({"streams": [{"codec_name": "h264"}]})) is None


def test_ffprobe_failure_timeout_and_missing_binary_return_none():
    class Result:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    assert camera_video_probe.ffprobe_stream(MAIN, run=lambda *a, **k: Result(1)) is None

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("ffprobe", 1)

    def missing(*_a, **_k):
        raise FileNotFoundError("ffprobe")

    assert camera_video_probe.ffprobe_stream(MAIN, run=timeout) is None
    assert camera_video_probe.ffprobe_stream(MAIN, run=missing) is None
    seen = {}

    def ok(args, **kwargs):
        seen["args"] = args
        return Result(0, json.dumps({"streams": [{"codec_name": "hevc", "width": 640, "height": 360, "avg_frame_rate": "15/1"}]}))

    assert camera_video_probe.ffprobe_stream(MAIN, run=ok)["width"] == 640
    assert seen["args"][:5] == ["ffprobe", "-v", "error", "-rtsp_transport", "tcp"]


def _probe_table(table):
    calls = []

    def probe(url):
        clean, user, password = camera_capabilities.split_credentials(url)
        calls.append((clean, user, password))
        return table.get(clean)
    probe.calls = calls
    return probe


def test_measure_fills_measured_keys_and_never_changes_what_p2p_selects():
    record = _record(main=(None, None, None), sub=(None, None, None), sub_kind="url_convention")
    p2p_before = [s["uri"] for s in camera_capabilities.select_streams(json.loads(json.dumps(record)), "live_p2p")]
    probe = _probe_table({CLEAN_MAIN: {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0},
                          CLEAN_SUB: {"width": 640, "height": 360, "codec": "H265", "fps": 15.0}})
    camera_video_probe.measure(record, MAIN, probe=probe, now=NOW)
    assert record["video_probe"]["status"] == "ok"
    assert record["streams"][0]["measured"]["width"] == 3840
    assert record["streams"][1]["measured"]["codec"] == "H265"
    # ONVIF/convention fields used by P2P are untouched -> same P2P choice.
    assert record["streams"][1]["codec"] is None and record["streams"][1]["width"] is None
    assert [s["uri"] for s in camera_capabilities.select_streams(record, "live_p2p")] == p2p_before
    # Credentials only reached the probe call, never the record.
    assert all(user == "admin" and password == SECRET for _, user, password in probe.calls)
    assert SECRET not in json.dumps(record)


def test_failed_probe_is_safe_and_retried_sooner():
    record = _record(main=(None, None, None))
    camera_video_probe.measure(record, MAIN, probe=lambda url: None, now=NOW)
    assert record["video_probe"]["status"] == "failed"
    assert video_profile.build(record).measured is False
    assert not camera_video_probe.probe_due(record, MAIN, NOW + timedelta(seconds=camera_video_probe.RETRY_SECONDS - 5))
    assert camera_video_probe.probe_due(record, MAIN, NOW + timedelta(seconds=camera_video_probe.RETRY_SECONDS + 5))
    doc = camera_video_probe.status_document(record)
    assert doc["status"] == "failed" and doc["resolution_class"] == "unknown"


def test_probe_raising_never_escapes():
    def boom(_url):
        raise RuntimeError("camera exploded")
    record = _record(main=(1920, 1080, "H264"))
    camera_video_probe.measure(record, MAIN, probe=boom, now=NOW)
    # ONVIF values still give a usable profile.
    assert video_profile.build(record).measured is True


def test_reprobe_period_and_url_change(monkeypatch):
    probe = _probe_table({CLEAN_MAIN: {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}})
    first, changed = camera_video_probe.refresh(MAIN, None, soap_call=None, url_candidates=None, probe=probe, now=NOW)
    assert changed and first["video_probe"]["status"] == "ok"
    same, changed = camera_video_probe.refresh(MAIN, first, soap_call=None, url_candidates=None, probe=probe,
                                               now=NOW + timedelta(hours=1))
    assert changed is False and same is first
    _, changed = camera_video_probe.refresh(MAIN, first, soap_call=None, url_candidates=None, probe=probe,
                                            now=NOW + timedelta(seconds=camera_video_probe.PROBE_SECONDS + 1))
    assert changed
    other = MAIN.replace("192.0.2.20", "192.0.2.21")
    moved, changed = camera_video_probe.refresh(other, first, soap_call=None, url_candidates=None,
                                                probe=_probe_table({CLEAN_MAIN.replace("192.0.2.20", "192.0.2.21"):
                                                                    {"width": 1920, "height": 1080, "codec": "H264", "fps": 25.0}}),
                                                now=NOW + timedelta(minutes=5))
    assert changed and video_profile.build(moved).source["width"] == 1920
    assert all("192.0.2.20" not in s["uri"] for s in moved["streams"])


def test_changed_resolution_is_detected_and_changes_the_profile():
    four_k = _probe_table({CLEAN_MAIN: {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}})
    record, _ = camera_video_probe.refresh(MAIN, None, soap_call=None, url_candidates=None, probe=four_k, now=NOW)
    five_mp = _probe_table({CLEAN_MAIN: {"width": 2592, "height": 1944, "codec": "H265", "fps": 20.0}})
    later, changed = camera_video_probe.refresh(MAIN, record, soap_call=None, url_candidates=None, probe=five_mp,
                                                force=True, now=NOW + timedelta(minutes=1))
    assert changed and camera_video_probe.geometry_changed(record, later)
    assert video_profile.build(later).clip_size == (1440, 1080)
    again, _ = camera_video_probe.refresh(MAIN, later, soap_call=None, url_candidates=None, probe=five_mp,
                                          force=True, now=NOW + timedelta(minutes=2))
    assert not camera_video_probe.geometry_changed(later, again)


def test_rediscovery_keeps_webrtc_verified_flags_for_the_same_streams():
    previous = _record(main=(3840, 2160, "H265"), sub=(640, 360, "H265"))
    previous["streams"][1]["verified"] = True
    previous["probed_at"] = (NOW - timedelta(days=3)).isoformat()   # capability record is due
    probe = _probe_table({CLEAN_MAIN: {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}})
    record, changed = camera_video_probe.refresh(
        MAIN, previous, soap_call=None, url_candidates=lambda url: [url.replace("/101", "/102")], probe=probe, now=NOW)
    assert changed
    sub = [s for s in record["streams"] if s["uri"] == CLEAN_SUB][0]
    assert sub["verified"] is True


def test_status_file_is_credential_free(tmp_path):
    record = _record(main=(3840, 2160, "H265"), sub=(640, 360, "H265"))
    record["video_probe"] = {"at": NOW.isoformat(), "status": "ok"}
    camera_video_probe.write_status(tmp_path, 7, record)
    text = (tmp_path / "camera7.video.json").read_text()
    data = json.loads(text)
    assert data["main"]["width"] == 3840 and data["analytics"]["width"] == 640
    assert data["resolution_class"] == "8mp" and data["status"] == "ok"
    assert "rtsp" not in text and "192.0.2" not in text and SECRET not in text


def test_profile_cache_ttl_invalidate_and_loader_failure():
    clock = [0.0]
    calls = []

    def loader(number):
        calls.append(number)
        if number == 9:
            raise RuntimeError("db down")
        return _record(main=(3840, 2160, "H265"))

    cache = camera_video_probe.ProfileCache(loader, ttl=60, clock=lambda: clock[0])
    assert cache.profile(1).clip_size == (1920, 1080)
    cache.profile(1)
    assert calls == [1]
    clock[0] = 61
    cache.profile(1)
    assert calls == [1, 1]
    cache.invalidate(1)
    cache.profile(1)
    assert calls == [1, 1, 1]
    assert cache.profile(9).measured is False   # loader failure -> safe defaults, no crash


# ----------------------------------------------------------------- real ffmpeg

@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("width,height,expected", [
    (1920, 1080, (1920, 1080)), (2560, 1440, (1920, 1080)), (2592, 1944, (1440, 1080)),
    (3840, 2160, (1920, 1080)), (4000, 3000, (1440, 1080)), (640, 360, (640, 360)),
])
def test_scale_filter_really_produces_the_profile_size(tmp_path, width, height, expected):
    profile = video_profile.build(_record(main=(width, height, "H265")))
    out = tmp_path / "o.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=5",
                    "-frames:v", "2", "-vf", profile.clip_filter(), "-c:v", "libx264", "-preset", "ultrafast", str(out)],
                   check=True, timeout=120)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height,pix_fmt", "-of", "json", str(out)],
                                      capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert (probe["width"], probe["height"]) == expected == profile.clip_size
    assert probe["pix_fmt"] == "yuv420p"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_unknown_profile_filter_downscales_a_4k_source_without_distortion(tmp_path):
    profile = video_profile.build(None)
    out = tmp_path / "o.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=2592x1944:rate=5",
                    "-frames:v", "1", "-vf", profile.clip_filter(), "-c:v", "libx264", "-preset", "ultrafast", str(out)],
                   check=True, timeout=120)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height", "-of", "json", str(out)],
                                      capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert (probe["width"], probe["height"]) == (1440, 1080)


# ----------------------------------------------------------------- live encode args

class _FixedCache:
    def __init__(self, record):
        self._profile = video_profile.build(record, 4000)

    def profile(self, _number):
        return self._profile


def test_live_encode_is_scaled_into_the_profile_box(monkeypatch):
    import main
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", False)
    monkeypatch.setattr(main, "LIVE_SCALE_MODE", "profile")
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 4000)
    monkeypatch.setattr(main, "camera_video_profiles", _FixedCache(_record(main=(3840, 2160, "H265"))))
    args = main.live_video_args(1)
    assert args[:2] == ["-c:v", "libx264"]
    vf = args[args.index("-vf") + 1]
    assert vf.startswith("scale='min(1920,iw)':'min(1080,ih)'") and vf.endswith("format=yuv420p")
    assert args[args.index("-maxrate") + 1] == "4000k"   # never above the configured cap
    for item in ("veryfast", "zerolatency", "-g", "56", "-keyint_min", "28", "-sc_threshold"):
        assert item in args


def test_live_scale_full_restores_the_previous_args(monkeypatch):
    import main
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", False)
    monkeypatch.setattr(main, "LIVE_SCALE_MODE", "full")
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 4000)
    assert main.live_video_args(1) == ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                                       "-g", "56", "-keyint_min", "28", "-sc_threshold", "0",
                                       "-maxrate", "4000k", "-bufsize", "8000k"]


def test_small_camera_gets_a_lower_live_cap_and_copy_is_never_scaled(monkeypatch):
    import main
    monkeypatch.setattr(main, "LIVE_SCALE_MODE", "profile")
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 4000)
    monkeypatch.setattr(main, "camera_video_profiles", _FixedCache(_record(main=(1280, 720, "H264"))))
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", False)
    args = main.live_video_args(2)
    assert int(args[args.index("-maxrate") + 1].rstrip("k")) < 4000
    monkeypatch.setattr(main, "LIVE_VIDEO_COPY", True)
    main._live_copy_probe[2] = (time.monotonic(), "h264")
    try:
        assert main.live_video_args(2) == ["-c:v", "copy"]
    finally:
        main._live_copy_probe.pop(2, None)


# ----------------------------------------------------------------- appliance agent

def test_agent_reports_whitelisted_video_status(tmp_path):
    from anyaicam_agent.camera_binding import LocalVmsStatusReader
    record = _record(main=(3840, 2160, "H265"), sub=(640, 360, "H265"))
    record["video_probe"] = {"at": NOW.isoformat(), "status": "ok"}
    camera_video_probe.write_status(tmp_path, 3, record)
    status = LocalVmsStatusReader(tmp_path, tmp_path / "rec").status(3)
    assert status["video"]["main"] == {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}
    assert status["video"]["sub"]["width"] == 640
    assert status["video"]["resolution_class"] == "8mp" and status["video"]["status"] == "ok"
    assert "video" not in LocalVmsStatusReader(tmp_path, tmp_path / "rec").status(4)   # no file: key absent
    (tmp_path / "camera5.video.json").write_text('{"status":"ok","main":{"width":"9e9","height":"x"},'
                                                '"resolution_class":"<script>","uri":"rtsp://a:b@c"}')
    bad = LocalVmsStatusReader(tmp_path, tmp_path / "rec").status(5)["video"]
    assert bad["main"] is None and bad["resolution_class"] == "unknown" and "uri" not in bad
    (tmp_path / "camera6.video.json").write_text("{not json")
    assert "video" not in LocalVmsStatusReader(tmp_path, tmp_path / "rec").status(6)


# ----------------------------------------------------------------- cloud

def test_cloud_columns_recompute_class_and_ignore_failed_or_malformed():
    columns = video_profile.cloud_measured_columns({
        "status": "ok", "resolution_class": "2mp",  # a wrong reported class is ignored
        "main": {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0},
        "sub": {"width": 640, "height": 360, "codec": "H265", "fps": 15}})
    assert columns["measured_resolution_class"] == "8mp" and columns["measured_width"] == 3840
    assert columns["measured_sub_width"] == 640 and columns["measured_shape"] == "landscape"
    assert video_profile.cloud_measured_columns({"status": "failed", "main": {"width": 1920, "height": 1080}}) is None
    assert video_profile.cloud_measured_columns({"status": "ok", "main": {"width": "abc"}}) is None
    assert video_profile.cloud_measured_columns(None) is None


def _cloud_client(tmp_path):
    from database_backend import override_target
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    path = tmp_path / "cloud.db"
    ctx = override_target(sqlite_path=str(path))
    ctx.__enter__()
    import appliance_cloud
    from partner_db import connection, initialize_database, password_hash
    initialize_database()
    now = "2026-10-11T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("p1", "P", "approved", "real", now))
        db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("c1", "p1", "C", "c1@example.test", "active", "real", now))
        db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("s1", "c1", "Site", now))
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("a1", "c1", "s1", "AIC-VID00001", now))
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,camera_number,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   ("cam-1", "c1", "s1", "a1", "Driveway", "2mp", 1, now))
        db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cr1", "a1", password_hash("cred"), now))
    app = FastAPI()
    appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
    return ctx, TestClient(app), connection


def _headers():
    return {"X-Appliance-Id": "a1", "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": "Bearer cred"}


def test_cloud_stores_measured_geometry_separately_from_the_sales_label(tmp_path):
    ctx, client, connection = _cloud_client(tmp_path)
    try:
        with client:
            report = {"status": "ok", "probed_at": NOW.isoformat(), "resolution_class": "8mp", "shape": "landscape",
                      "main": {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0},
                      "sub": {"width": 640, "height": 360, "codec": "H265", "fps": 15.0}}
            response = client.post("/api/appliance/cameras", json={"cameras": [{"id": "cam-1", "online": True, "video": report}]}, headers=_headers())
            assert response.status_code == 200
            with connection() as db:
                row = dict(db.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone())
            assert row["resolution"] == "2mp"                     # sales label untouched
            assert (row["measured_width"], row["measured_height"], row["measured_codec"]) == (3840, 2160, "H265")
            assert row["measured_resolution_class"] == "8mp" and row["measured_sub_width"] == 640
            assert row["measured_video_at"]
            # An older appliance (no "video") and a failed probe leave the measurement alone.
            client.post("/api/appliance/cameras", json={"cameras": [{"id": "cam-1", "online": True}]}, headers=_headers())
            client.post("/api/appliance/cameras", json={"cameras": [{"id": "cam-1", "video": {"status": "failed"}}]}, headers=_headers())
            with connection() as db:
                again = dict(db.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone())
            assert again["measured_width"] == 3840
            # A changed resolution is stored.
            report["main"] = {"width": 2592, "height": 1944, "codec": "H264", "fps": 15}
            client.post("/api/appliance/cameras", json={"cameras": [{"id": "cam-1", "video": report}]}, headers=_headers())
            with connection() as db:
                changed = dict(db.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone())
            assert changed["measured_resolution_class"] == "5mp" and changed["measured_width"] == 2592
    finally:
        ctx.__exit__(None, None, None)
