"""Capability-driven camera integration (camera_capabilities.py).

The five Ryzen cameras appear here only as a regression fixture; every other
test uses arbitrary counts, brands and URL shapes."""
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import camera_capabilities as cc
import camera_stream_selection as sel
import webrtc_publisher as wp

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _profiles_xml(profiles):
    items = []
    for p in profiles:
        extra = ('<tt:AudioOutputConfiguration token="ao"/>' if p.get("audio") else "")
        if p.get("audio_in"):
            extra += ('<tt:AudioSourceConfiguration token="ai"/><tt:AudioEncoderConfiguration token="ae">'
                      f'<tt:Encoding>{p["audio_in"]}</tt:Encoding></tt:AudioEncoderConfiguration>')
        if p.get("ptz"):
            spaces = "".join(f"<tt:{name}>x</tt:{name}>" for name in p.get("ptz_spaces", ()))
            extra += f'<tt:PTZConfiguration token="ptz">{spaces}</tt:PTZConfiguration>'
        items.append(
            f'<trt:Profiles token="{p["token"]}"><tt:VideoEncoderConfiguration><tt:Encoding>{p["codec"]}</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{p["w"]}</tt:Width><tt:Height>{p["h"]}</tt:Height></tt:Resolution>'
            f'<tt:RateControl><tt:FrameRateLimit>{p["fps"]}</tt:FrameRateLimit><tt:BitrateLimit>{p["kbps"]}</tt:BitrateLimit></tt:RateControl>'
            f'</tt:VideoEncoderConfiguration>{extra}</trt:Profiles>')
    return ('<Envelope xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            f'<Body><trt:GetProfilesResponse>{"".join(items)}</trt:GetProfilesResponse></Body></Envelope>')


def fake_onvif(profiles, uris, snapshot="http://cam/snapshot.jpg", device=None, services=None):
    """soap_call stand-in: GetProfiles / GetStreamUri (by token) / GetSnapshotUri,
    plus the device service when device/services are given (otherwise it
    fails, like a camera that only implements the media service)."""
    calls = []

    def soap_call(host, username, password, body, action):
        calls.append(action.rsplit("/", 1)[-1])
        if action.endswith("GetDeviceInformation"):
            if device is None:
                raise OSError("no device service")
            return ET.fromstring("<E>" + "".join(f"<{k}>{v}</{k}>" for k, v in device.items()) + "</E>")
        if action.endswith("GetCapabilities"):
            if services is None:
                raise OSError("no device service")
            return ET.fromstring("<E>" + "".join(f"<{k}><XAddr>{v}</XAddr></{k}>" for k, v in services.items()) + "</E>")
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
        device={"Manufacturer": "AnyVendor", "Model": "X-1", "FirmwareVersion": "5.7", "SerialNumber": "SN-DO-NOT-STORE"},
        services={"Media": "http://admin:pw@10.1.1.5/onvif/Media", "Events": "http://10.1.1.5/onvif/Events"},
    )
    record = cc.discover("rtsp://admin:s3cret@10.1.1.5:554/media/stream1", soap_call=soap, url_candidates=wp.substream_candidates, now=NOW)
    assert record["schema"] == 2 and record["adapters"] == ["onvif_media", "onvif_device"]
    roles = {s["id"]: (s["role"], s["codec"], s["width"], s["fps"], s["bitrate_kbps"]) for s in record["streams"]}
    assert roles == {"hi": ("main", "H264", 3840, 25, 12000), "lo": ("sub", "H264", 704, 15, 512)}
    assert record["streams"][0]["transport"] == {"protocol": "rtsp", "scheme": "rtsp", "port": 554, "rtsp_transport": "tcp"}
    assert record["audio"]["output"] is True and record["ptz"]["supported"] is True
    assert record["snapshot"] == {"supported": True, "uri": "http://10.1.1.5/snap.jpg"}
    assert record["device"] == {"manufacturer": "AnyVendor", "model": "X-1", "firmware": "5.7"}
    assert record["events"] == {"supported": True, "analytics": False}
    assert record["onvif"] == {"supported": True, "auth": "ws_security", "device_service": None,
                               "media_service": "http://10.1.1.5/onvif/Media"}
    assert record["auth"]["credentials_configured"] is True
    dumped = json.dumps(record)
    assert "s3cret" not in dumped and "admin" not in dumped and "pw@" not in dumped and "SN-DO-NOT-STORE" not in dumped


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


# ------------------------------------------------------------ schema 2: the camera shapes AnyAiCam must handle

def _discover(profiles, uris, main_uri=None, **fake):
    main_uri = main_uri or "rtsp://u:p@" + uris[profiles[0]["token"]].split("//", 1)[1]
    return cc.discover(main_uri, soap_call=fake_onvif(profiles, uris, **fake), url_candidates=wp.substream_candidates, now=NOW)


def test_main_plus_substream_recording_gets_main_and_p2p_gets_the_substream():
    record = _discover([{"token": "p0", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096},
                        {"token": "p1", "codec": "H264", "w": 640, "h": 480, "fps": 15, "kbps": 512}],
                       {"p0": "rtsp://10.20.0.1/live/0", "p1": "rtsp://10.20.0.1/live/1"})
    assert sel.select_streams(record, "recording")[0]["id"] == "p0"
    assert [s["id"] for s in sel.select_streams(record, "live_p2p")] == ["p1", "p0"]


def test_main_stream_only_camera_uses_its_main_stream_for_everything():
    record = _discover([{"token": "only", "codec": "H264", "w": 1280, "h": 720, "fps": 30, "kbps": 2048}],
                       {"only": "rtsp://10.21.0.1:8554/stream"})
    assert "url_convention" not in record["adapters"]  # an unrecognized URL shape is not guessed at
    for purpose in ("recording", "live_p2p", "analytics"):
        assert [s["id"] for s in sel.select_streams(record, purpose)] == ["only"]


def test_h264_and_h265_options_record_keeps_quality_p2p_gets_browser_compatible_h264():
    record = _discover([{"token": "m265", "codec": "H265", "w": 3840, "h": 2160, "fps": 20, "kbps": 8192},
                        {"token": "s265", "codec": "H265", "w": 1280, "h": 720, "fps": 15, "kbps": 768},
                        {"token": "s264", "codec": "H264", "w": 960, "h": 540, "fps": 15, "kbps": 1024}],
                       {"m265": "rtsp://10.22.0.1/m", "s265": "rtsp://10.22.0.1/s1", "s264": "rtsp://10.22.0.1/s2"})
    assert sel.select_streams(record, "recording")[0]["id"] == "m265"  # recorded as-is (-c:v copy)
    assert [s["id"] for s in sel.select_streams(record, "live_p2p")] == ["s264", "m265"]  # never the H.265 sub


def test_an_h265_only_camera_degrades_to_its_main_stream_for_p2p():
    record = _discover([{"token": "m", "codec": "H265", "w": 2560, "h": 1440, "fps": 20, "kbps": 4096},
                        {"token": "s", "codec": "H265", "w": 640, "h": 360, "fps": 10, "kbps": 256}],
                       {"m": "rtsp://10.23.0.1/m", "s": "rtsp://10.23.0.1/s"})
    assert [s["id"] for s in sel.select_streams(record, "live_p2p")] == ["m"]


def test_a_camera_with_no_audio_reports_no_rather_than_unknown():
    record = _discover([{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096}],
                       {"m": "rtsp://10.24.0.1/m"})
    assert record["audio"] == {"input": False, "input_codec": None, "output": False, "two_way": False}
    assert record["streams"][0]["audio"] == {"present": False, "codec": None}
    assert sel.supports(record, "talk") is False and sel.supports(record, "two_way_audio") is False


def test_a_two_way_audio_camera():
    record = _discover([{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096,
                         "audio": True, "audio_in": "G711"}], {"m": "rtsp://10.25.0.1/m"})
    assert record["audio"] == {"input": True, "input_codec": "G711", "output": True, "two_way": True}
    assert record["streams"][0]["audio"] == {"present": True, "codec": "G711"}
    assert sel.supports(record, "two_way_audio") is True and sel.supports(record, "talk") is True


def test_a_ptz_camera_from_its_profile_and_from_its_ptz_service_alone():
    record = _discover([{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096, "ptz": True,
                         "ptz_spaces": ("DefaultContinuousPanTiltVelocitySpace", "DefaultContinuousZoomVelocitySpace")}],
                       {"m": "rtsp://10.26.0.1/m"})
    assert record["ptz"] == {"supported": True, "pan_tilt": True, "zoom": True} and sel.supports(record, "ptz") is True
    service_only = _discover([{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096}],
                             {"m": "rtsp://10.26.0.2/m"}, services={"PTZ": "http://10.26.0.2/onvif/PTZ"})
    assert service_only["ptz"] == {"supported": True, "pan_tilt": None, "zoom": None}  # which axes: unknown
    fixed = _discover([{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096}], {"m": "rtsp://10.26.0.3/m"})
    assert sel.supports(fixed, "ptz") is False


def test_incomplete_discovery_leaves_unknowns_unknown_and_still_selects_safely():
    """ONVIF answers GetProfiles with no encoder details, one stream URI
    fails, and there is no device service."""
    def partial(host, username, password, body, action):
        if action.endswith("GetProfiles"):
            return ET.fromstring('<E xmlns:trt="http://www.onvif.org/ver10/media/wsdl"><trt:Profiles token="a"/>'
                                 '<trt:Profiles token="b"/><trt:Profiles token="c"/></E>')
        if action.endswith("GetStreamUri"):
            token = body.split("<trt:ProfileToken>")[1].split("<")[0]
            if token == "c":
                raise TimeoutError
            return ET.fromstring(f"<E><Uri>rtsp://10.27.0.1/{token}</Uri></E>")
        raise OSError(action)
    record = cc.discover("rtsp://u:p@10.27.0.1/a", soap_call=partial, url_candidates=wp.substream_candidates, now=NOW)
    assert [(s["id"], s["role"], s["codec"], s["width"]) for s in record["streams"]] == [("a", "main", None, None), ("b", "sub", None, None)]
    assert record["device"] == {"manufacturer": None, "model": None, "firmware": None}
    assert record["snapshot"] == {"supported": False, "uri": None} and record["events"]["supported"] is None
    assert sel.select_streams(record, "recording")[0]["uri"] == "rtsp://10.27.0.1/a"
    assert [s["id"] for s in sel.select_streams(record, "live_p2p")] == ["b", "a"]  # unknown codec: the caller's probe decides


def test_no_onvif_at_all_still_works_through_rtsp_and_claims_nothing():
    record = cc.discover("rtsp://u:p@10.28.0.1:10554/ch0", soap_call=no_onvif, url_candidates=wp.substream_candidates, now=NOW)
    assert record["adapters"] == [] and record["onvif"]["supported"] is None
    assert [s["uri"] for s in sel.select_streams(record, "recording")] == ["rtsp://10.28.0.1:10554/ch0"]
    assert record["streams"][0]["transport"]["port"] == 10554
    for feature in ("talk", "two_way_audio", "ptz", "snapshot", "events", "onvif"):
        assert sel.supports(record, feature) is None, feature  # unknown, never guessed as "no"
    assert sel.supports(None, "ptz") is None and sel.select_streams(None, "live_p2p") == []


def test_an_onvif_outage_on_reprobe_keeps_the_earlier_substream_and_retries_sooner():
    profiles = [{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096},
                {"token": "s", "codec": "H264", "w": 640, "h": 360, "fps": 10, "kbps": 256}]
    first = _discover(profiles, {"m": "rtsp://10.29.0.1/m", "s": "rtsp://10.29.0.1/s"})
    later = NOW + timedelta(seconds=cc.REPROBE_SECONDS)
    again = cc.discover("rtsp://u:p@10.29.0.1/m", soap_call=no_onvif, url_candidates=None, now=later, previous=first)
    assert again["probe_status"] == "stale" and again["probed_at"] == first["probed_at"]
    assert sel.select_streams(again, "live_p2p")[0]["id"] == "s"
    assert not cc.needs_reprobe(again, "rtsp://u:p@10.29.0.1/m", now=later + timedelta(minutes=5))
    assert cc.needs_reprobe(again, "rtsp://u:p@10.29.0.1/m", now=later + timedelta(seconds=cc.STALE_REPROBE_SECONDS))
    moved = cc.discover("rtsp://u:p@10.29.0.9/m", soap_call=no_onvif, url_candidates=None, now=later, previous=first)
    assert moved["probe_status"] == "fresh" and [s["uri"] for s in moved["streams"]] == ["rtsp://10.29.0.9/m"]


def test_a_stored_schema_1_record_still_selects_and_answers_then_is_reprobed():
    old = {"schema": 1, "probed_at": NOW.isoformat(), "adapters": ["onvif_media"], "audio": {"output": True}, "ptz": False,
           "snapshot_uri": "http://10.30.0.1/snap.jpg",
           "streams": [{"id": "main-a", "role": "main", "uri": "rtsp://10.30.0.1/101", "codec": "H264", "width": 2560},
                       {"id": "sub-b", "role": "sub", "uri": "rtsp://10.30.0.1/102", "codec": "H264", "width": 640}]}
    assert sel.select_streams(old, "live_p2p")[0]["id"] == "sub-b"
    assert sel.supports(old, "talk") is True and sel.supports(old, "ptz") is False and sel.supports(old, "snapshot") is True
    assert sel.snapshot_uri(old) == "http://10.30.0.1/snap.jpg"
    assert cc.needs_reprobe(old, "rtsp://u:p@10.30.0.1/101", now=NOW)
    upgraded = cc.upgrade(old)
    assert upgraded["ptz"]["supported"] is False and upgraded["audio"]["output"] is True
    assert upgraded["streams"][1]["transport"]["port"] == 554 and upgraded["onvif"]["supported"] is True
    kept = cc.discover("rtsp://u:p@10.30.0.1/101", soap_call=no_onvif, url_candidates=None, now=NOW, previous=old)
    assert kept["schema"] == 2 and kept["probe_status"] == "stale" and sel.select_streams(kept, "live_p2p")[0]["id"] == "sub-b"


def test_onvif_soap_call_routes_by_service_and_never_sends_the_password(monkeypatch):
    import talk_down_discovery as tdd
    sent = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<E/>"

    def fake_urlopen(request, timeout):
        sent.append((request.full_url, request.data.decode()))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cc.onvif_soap_call("10.31.0.1", "admin", "Pa55word!", "<x/>", "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation")
    cc.onvif_soap_call("10.31.0.1", "admin", "Pa55word!", "<x/>", "http://www.onvif.org/ver10/media/wsdl/GetProfiles")
    cc.onvif_soap_call("10.31.0.1", "", "", "<x/>", "http://www.onvif.org/ver10/media/wsdl/GetProfiles")
    assert [url for url, _ in sent] == [f"http://10.31.0.1:{tdd.ONVIF_PORT}/onvif/device_service",
                                        f"http://10.31.0.1:{tdd.ONVIF_PORT}/onvif/media_service",
                                        f"http://10.31.0.1:{tdd.ONVIF_PORT}/onvif/media_service"]
    assert all("Pa55word!" not in body for _, body in sent)  # digest only
    assert "UsernameToken" in sent[0][1] and "UsernameToken" not in sent[2][1]  # no credentials when none configured


def test_p2p_keeps_its_substream_through_an_onvif_outage(monkeypatch):
    """webrtc_publisher integration: the reprobe fails, the stored
    substream is still what MediaMTX is given."""
    stored = _discover([{"token": "m", "codec": "H264", "w": 1920, "h": 1080, "fps": 25, "kbps": 4096},
                        {"token": "s", "codec": "H264", "w": 640, "h": 360, "fps": 10, "kbps": 256}],
                       {"m": "rtsp://10.32.0.1/m", "s": "rtsp://10.32.0.1/s"})
    stored["probed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=cc.REPROBE_SECONDS + 1)).isoformat()
    saved = {}
    monkeypatch.setattr(wp, "_p2p_source_choice", {})
    monkeypatch.setattr(wp, "P2P_STREAM_PREFERENCE", "auto")
    monkeypatch.setattr(wp, "_load_capabilities", lambda camera_id: stored)
    monkeypatch.setattr(wp, "_save_capabilities", lambda camera_id, record: saved.update({camera_id: record}))
    monkeypatch.setattr(wp, "_onvif_soap_call", lambda: no_onvif)
    monkeypatch.setattr(wp, "_probe_video_stream", lambda url: url.startswith("rtsp://u:p@10.32.0.1/s"))
    source, kind = wp.p2p_source_for("cam-x", "rtsp://u:p@10.32.0.1/m")
    assert (source, kind) == ("rtsp://u:p@10.32.0.1/s", "substream")
    assert saved["cam-x"]["probe_status"] == "stale" and "u:p" not in json.dumps(saved["cam-x"])


def test_camera_count_ids_and_vendor_urls_are_never_part_of_the_model():
    import inspect
    for module in (cc, sel):
        source = inspect.getsource(module)
        for forbidden in ("camera_number", "192.168.", "Profile_1", "Streaming/Channels", "realmonitor"):
            assert forbidden not in source, (module.__name__, forbidden)
