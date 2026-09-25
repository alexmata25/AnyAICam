"""Capability-driven camera integration (camera_capabilities.py).

The five Ryzen cameras appear here only as a regression fixture; every other
test uses arbitrary counts, brands and URL shapes."""
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import camera_capabilities as cc
import webrtc_publisher as wp

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _profiles_xml(profiles):
    items = []
    for p in profiles:
        extra = ('<tt:AudioOutputConfiguration token="ao"/>' if p.get("audio") else "") + ('<tt:PTZConfiguration token="ptz"/>' if p.get("ptz") else "")
        items.append(
            f'<trt:Profiles token="{p["token"]}"><tt:VideoEncoderConfiguration><tt:Encoding>{p["codec"]}</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{p["w"]}</tt:Width><tt:Height>{p["h"]}</tt:Height></tt:Resolution>'
            f'<tt:RateControl><tt:FrameRateLimit>{p["fps"]}</tt:FrameRateLimit><tt:BitrateLimit>{p["kbps"]}</tt:BitrateLimit></tt:RateControl>'
            f'</tt:VideoEncoderConfiguration>{extra}</trt:Profiles>')
    return ('<Envelope xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            f'<Body><trt:GetProfilesResponse>{"".join(items)}</trt:GetProfilesResponse></Body></Envelope>')


def fake_onvif(profiles, uris, snapshot="http://cam/snapshot.jpg"):
    """soap_call stand-in: GetProfiles / GetStreamUri (by token) / GetSnapshotUri."""
    calls = []

    def soap_call(host, username, password, body, action):
        calls.append(action.rsplit("/", 1)[-1])
        if action.endswith("GetProfiles"):
            return ET.fromstring(_profiles_xml(profiles))
        if action.endswith("GetStreamUri"):
            token = body.split("<trt:ProfileToken>")[1].split("<")[0]
            return ET.fromstring(f"<E><Uri>{uris[token]}</Uri></E>")
        if action.endswith("GetSnapshotUri"):
            return ET.fromstring(f"<E><Uri>{snapshot}</Uri></E>")
        raise AssertionError(action)
    soap_call.calls = calls
    return soap_call


def no_onvif(*_args, **_kwargs):
    raise OSError("no ONVIF service")


# ------------------------------------------------------------ discovery

def test_onvif_discovery_records_streams_audio_ptz_snapshot_without_credentials():
    soap = fake_onvif(
        [{"token": "hi", "codec": "H264", "w": 3840, "h": 2160, "fps": 25, "kbps": 12000, "audio": True, "ptz": True},
         {"token": "lo", "codec": "H264", "w": 704, "h": 480, "fps": 15, "kbps": 512}],
        {"hi": "rtsp://10.1.1.5:554/media/stream1", "lo": "rtsp://10.1.1.5:554/media/stream2"},
        snapshot="http://admin:pw@10.1.1.5/snap.jpg",
    )
    record = cc.discover("rtsp://admin:s3cret@10.1.1.5:554/media/stream1", soap_call=soap, url_candidates=wp.substream_candidates, now=NOW)
    assert record["adapters"] == ["onvif_media"]
    roles = {s["id"]: (s["role"], s["codec"], s["width"], s["fps"], s["bitrate_kbps"]) for s in record["streams"]}
    assert roles == {"hi": ("main", "H264", 3840, 25, 12000), "lo": ("sub", "H264", 704, 15, 512)}
    assert record["audio"] == {"output": True} and record["ptz"] is True
    assert record["snapshot_uri"] == "http://10.1.1.5/snap.jpg"
    assert "s3cret" not in json.dumps(record) and "admin" not in json.dumps(record)


def test_url_convention_is_only_a_fallback_when_onvif_is_unavailable():
    record = cc.discover("rtsp://u:p@10.2.0.9/cam/realmonitor?channel=1&subtype=0", soap_call=no_onvif, url_candidates=wp.substream_candidates, now=NOW)
    assert record["adapters"] == ["url_convention"]
    assert [s["uri"] for s in record["streams"] if s["role"] == "sub"] == ["rtsp://10.2.0.9/cam/realmonitor?channel=1&subtype=1"]


def test_onvif_with_a_second_stream_never_falls_back_to_url_guessing():
    soap = fake_onvif(
        [{"token": "a", "codec": "H264", "w": 1920, "h": 1080, "fps": 20, "kbps": 4096},
         {"token": "b", "codec": "H264", "w": 640, "h": 360, "fps": 10, "kbps": 256}],
        {"a": "rtsp://10.3.0.2/Streaming/Channels/101", "b": "rtsp://10.3.0.2/some/vendor/sub"})
    record = cc.discover("rtsp://u:p@10.3.0.2/Streaming/Channels/101", soap_call=soap, url_candidates=wp.substream_candidates, now=NOW)
    assert "url_convention" not in record["adapters"]
    assert "rtsp://10.3.0.2/Streaming/Channels/102" not in [s["uri"] for s in record["streams"]]


def test_an_unknown_camera_with_no_onvif_keeps_only_its_main_stream():
    record = cc.discover("rtsp://u:p@10.4.0.7:8554/live", soap_call=no_onvif, url_candidates=wp.substream_candidates, now=NOW)
    assert [(s["role"], s["uri"]) for s in record["streams"]] == [("main", "rtsp://10.4.0.7:8554/live")]
    assert [s["role"] for s in cc.select_streams(record, "live_p2p")] == ["main"]


# ------------------------------------------------------------ selection

def _record(*streams):
    return {"schema": 1, "probed_at": NOW.isoformat(), "adapters": [], "streams": [dict(s) for s in streams]}


MAIN_4K = {"id": "m", "role": "main", "uri": "rtsp://c/m", "codec": "H264", "width": 3840, "fps": 25, "bitrate_kbps": 12000}
SUB_720 = {"id": "s1", "role": "sub", "uri": "rtsp://c/s1", "codec": "H264", "width": 1280, "fps": 15, "bitrate_kbps": 1024}
SUB_360 = {"id": "s2", "role": "sub", "uri": "rtsp://c/s2", "codec": "H264", "width": 640, "fps": 10, "bitrate_kbps": 256}
SUB_H265 = {"id": "s3", "role": "sub", "uri": "rtsp://c/s3", "codec": "H265", "width": 640, "fps": 10, "bitrate_kbps": 200}
TINY = {"id": "s4", "role": "sub", "uri": "rtsp://c/s4", "codec": "H264", "width": 320, "fps": 5, "bitrate_kbps": 64}


def test_recording_always_selects_the_full_quality_main_stream():
    assert [s["id"] for s in cc.select_streams(_record(SUB_360, MAIN_4K, SUB_720), "recording")] == ["m"]


def test_p2p_prefers_the_lightest_webrtc_compatible_stream_that_is_big_enough():
    order = [s["id"] for s in cc.select_streams(_record(MAIN_4K, SUB_720, SUB_360, TINY), "live_p2p")]
    assert order[:2] == ["s2", "s1"] and order[-1] == "m"


def test_p2p_skips_a_substream_browsers_cannot_decode():
    assert [s["id"] for s in cc.select_streams(_record(MAIN_4K, SUB_H265), "live_p2p")] == ["m"]


def test_analytics_uses_the_smallest_stream_that_is_still_detailed_enough():
    assert [s["id"] for s in cc.select_streams(_record(MAIN_4K, SUB_720, SUB_360, TINY), "analytics")][:1] == ["s2"]


def test_arbitrary_camera_counts_and_mixed_brands():
    """Twelve cameras, three URL/brand shapes, some with ONVIF, some without."""
    shapes = [
        ("rtsp://u:p@10.9.0.{n}/Streaming/Channels/101", True),
        ("rtsp://u:p@10.9.0.{n}/cam/realmonitor?channel=1&subtype=0", False),
        ("rtsp://u:p@10.9.0.{n}:8554/stream", False),
    ]
    for n in range(1, 13):
        template, has_onvif = shapes[n % 3]
        main = template.format(n=n)
        soap = fake_onvif(
            [{"token": "m", "codec": "H264", "w": 2688, "h": 1520, "fps": 20, "kbps": 6144},
             {"token": "s", "codec": "H264", "w": 640, "h": 480, "fps": 15, "kbps": 384}],
            {"m": cc.split_credentials(main)[0], "s": f"rtsp://10.9.0.{n}/onvif-sub"}) if has_onvif else no_onvif
        record = cc.discover(main, soap_call=soap, url_candidates=wp.substream_candidates, now=NOW)
        assert cc.select_streams(record, "recording")[0]["uri"] == cc.split_credentials(main)[0]
        assert record["streams"], n


def test_the_five_ryzen_cameras_regression_fixture():
    """Hikvision-style ONVIF cameras: Profile_1 main, Profile_2 640x360 sub."""
    for n in range(1, 6):
        main = f"rtsp://u:p@192.168.0.{40 + n}:554/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
        soap = fake_onvif(
            [{"token": "Profile_1", "codec": "H264", "w": 2560, "h": 1440, "fps": 20, "kbps": 6144},
             {"token": "Profile_2", "codec": "H264", "w": 640, "h": 360, "fps": 10, "kbps": 256}],
            {"Profile_1": cc.split_credentials(main)[0],
             "Profile_2": f"rtsp://192.168.0.{40 + n}:554/Streaming/Channels/102?transportmode=unicast&profile=Profile_2"})
        record = cc.discover(main, soap_call=soap, url_candidates=wp.substream_candidates, now=NOW)
        assert cc.select_streams(record, "live_p2p")[0]["uri"].endswith("/Streaming/Channels/102?transportmode=unicast&profile=Profile_2")
        assert cc.select_streams(record, "recording")[0]["uri"].endswith("/Streaming/Channels/101?transportmode=unicast&profile=Profile_1")


# ------------------------------------------------------------ lifecycle

def test_reprobe_rules():
    record = cc.discover("rtsp://u:p@10.5.0.1/a", soap_call=no_onvif, url_candidates=None, now=NOW)
    assert cc.needs_reprobe(None, "rtsp://u:p@10.5.0.1/a")
    assert not cc.needs_reprobe(record, "rtsp://u:p@10.5.0.1/a", now=NOW + timedelta(hours=1))
    assert cc.needs_reprobe(record, "rtsp://u:p@10.5.0.2/a", now=NOW + timedelta(hours=1))  # camera re-provisioned
    assert cc.needs_reprobe(record, "rtsp://u:p@10.5.0.1/a", now=NOW + timedelta(seconds=cc.REPROBE_SECONDS))


def test_persistence_round_trip():
    import db_migrations
    sql = dict(db_migrations.MIGRATIONS)["20260925_camera_capabilities"] if hasattr(db_migrations, "MIGRATIONS") else None
    db = sqlite3.connect(":memory:")
    db.executescript(sql or "CREATE TABLE camera_capabilities(camera_id TEXT PRIMARY KEY, capabilities_json TEXT NOT NULL, probed_at TEXT NOT NULL);")
    record = cc.discover("rtsp://u:p@10.6.0.1/Streaming/Channels/101", soap_call=no_onvif, url_candidates=wp.substream_candidates, now=NOW)
    cc.save(db, "cam-9", record)
    cc.save(db, "cam-9", record)
    assert cc.load(db, "cam-9") == json.loads(json.dumps(record))
    assert cc.load(db, "missing") is None


def test_onvif_failures_never_break_discovery():
    def flaky(host, username, password, body, action):
        if action.endswith("GetProfiles"):
            return ET.fromstring(_profiles_xml([{"token": "x", "codec": "H264", "w": 1920, "h": 1080, "fps": 20, "kbps": 4000}]))
        raise TimeoutError("camera stopped answering")
    record = cc.discover("rtsp://u:p@10.7.0.1/x", soap_call=flaky, url_candidates=None, now=NOW)
    assert [s["role"] for s in record["streams"]] == ["main"]
