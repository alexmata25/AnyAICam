"""WebRTC (P2P) source selection must use each stream's OBSERVED codec.

Seen live on 2026-10-10 (Samsung, VMS 1.2.5): ONVIF advertised the camera's
substream as H264, the stream itself was H.265, the substream was chosen for
WebRTC, and MediaMTX rejected every viewer with HTTP 400 "codecs not supported
by client" -- a black tile, with only the status code in the logs.

Pinned here: every advertised stream (main and sub) is probed with ffprobe;
the observed codec is recorded next to (never instead of) the advertised one;
a verified H.264 substream is preferred, else an H.264 main stream; with no
H.264 stream an actionable warning is recorded and logged; camera settings are
never changed; a codec rejection from MediaMTX triggers a (rate-limited)
revalidation; MediaMTX's rejection reason is logged sanitized; recording and
live HLS keep their own stream selection.
"""
import io
import json
import logging
import subprocess
import urllib.error
from pathlib import Path

import pytest

import camera_capabilities
import webrtc_publisher as wp

MAIN = "rtsp://u:s3cr%40t@10.0.0.9:554/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
SUB = "rtsp://u:s3cr%40t@10.0.0.9:554/Streaming/Channels/102?transportmode=unicast&profile=Profile_2"
LOGGER = "anyaicam.webrtc_publisher"


@pytest.fixture()
def selection(monkeypatch):
    """No DB, no ONVIF; probes are recorded and answered from `codecs`."""
    state = {"codecs": {}, "probes": [], "saved": [], "record": None}
    monkeypatch.setattr(wp, "_p2p_source_choice", {})
    monkeypatch.setattr(wp, "_codec_revalidated_at", {})
    monkeypatch.setattr(wp, "P2P_STREAM_PREFERENCE", "auto")
    monkeypatch.setattr(wp, "_load_capabilities", lambda camera_id: state["record"])
    def save(camera_id, record):
        state["saved"].append(json.loads(json.dumps(record)))
        state["record"] = json.loads(json.dumps(record))
    monkeypatch.setattr(wp, "_save_capabilities", save)
    monkeypatch.setattr(wp, "_onvif_soap_call", lambda: None)
    monkeypatch.setattr(wp, "_probe_video_codec", lambda url: state["probes"].append(url) or state["codecs"].get(url))
    return state


def _onvif_record(sub_codec: str, main_codec: str = "H264") -> dict:
    """What ONVIF discovery stores (credentials never stored)."""
    clean_main = camera_capabilities.split_credentials(MAIN)[0]
    clean_sub = camera_capabilities.split_credentials(SUB)[0]
    return {"schema": camera_capabilities.SCHEMA_VERSION, "probed_at": "2099-01-01T00:00:00+00:00",
            "streams": [{"id": "Profile_1", "uri": clean_main, "codec": main_codec, "width": 3840, "height": 2160, "role": "main"},
                        {"id": "Profile_2", "uri": clean_sub, "codec": sub_codec, "width": 640, "height": 360, "role": "sub"}]}


def _use_onvif(selection, monkeypatch, record):
    selection["record"] = record
    monkeypatch.setattr(camera_capabilities, "needs_reprobe", lambda record, main_uri: False)


def _stream(record: dict, role: str) -> dict:
    return next(s for s in record["streams"] if (s.get("role") == "main") == (role == "main"))


# ------------------------------------------------------------------ the Samsung reproduction

def test_samsung_onvif_says_h264_but_the_substream_is_h265(selection, monkeypatch, caplog):
    _use_onvif(selection, monkeypatch, _onvif_record("H264"))
    selection["codecs"] = {SUB: "hevc", MAIN: "h264"}  # what ffprobe/MediaMTX actually see
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")  # a compatible source, chosen automatically
    saved = selection["saved"][-1]
    sub, main = _stream(saved, "sub"), _stream(saved, "main")
    assert sub["codec"] == "H264" and sub["observed_codec"] == "h265" and sub["verified"] is True
    assert main["observed_codec"] == "h264"
    assert saved["webrtc"]["compatible"] is True and saved["webrtc"]["source_role"] == "main"
    assert "substream_codec_unsupported camera_id=cam-x codec=h265" in caplog.text
    assert "s3cr" not in caplog.text and "10.0.0.9" not in caplog.text


# ------------------------------------------------------------------ selection

def test_verified_h264_substream_is_preferred(selection):
    selection["codecs"] = {SUB: "h264", MAIN: "h264"}
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "substream")
    assert selection["saved"][-1]["webrtc"]["source_role"] == "sub"


def test_url_guessed_h265_substream_falls_back_to_h264_main(selection):
    selection["codecs"] = {SUB: "hevc", MAIN: "h264"}
    assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")
    assert _stream(selection["saved"][-1], "sub")["observed_codec"] == "h265"


def test_advertised_h265_but_observed_h264_substream_is_used(selection, monkeypatch):
    _use_onvif(selection, monkeypatch, _onvif_record("H265"))  # the opposite mislabel
    selection["codecs"] = {SUB: "h264", MAIN: "h264"}
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "substream")


def test_no_h264_stream_and_transcoding_off_records_an_actionable_warning(selection, monkeypatch, caplog):
    monkeypatch.setattr(wp, "P2P_TRANSCODE", "off")
    _use_onvif(selection, monkeypatch, _onvif_record("H264"))
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")
    webrtc = selection["saved"][-1]["webrtc"]
    assert webrtc["compatible"] is False and "H.264" in webrtc["warning"] and "relay" in webrtc["warning"]
    assert "no_webrtc_compatible_stream camera_id=cam-x main_codec=h265 sub_codecs=h265" in caplog.text
    assert "s3cr" not in caplog.text


def test_unreachable_streams_are_not_reported_as_incompatible(selection, caplog):
    selection["codecs"] = {}  # nothing could be opened right now
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")
    saved = selection["saved"][-1]
    assert _stream(saved, "sub")["verified"] is False and saved["webrtc"]["warning"] is None
    assert "no_webrtc_compatible_stream" not in caplog.text


def test_both_streams_are_probed_and_recorded(selection, monkeypatch):
    _use_onvif(selection, monkeypatch, _onvif_record("H264"))
    selection["codecs"] = {SUB: "h264", MAIN: "hevc"}
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "substream")
    assert set(selection["probes"]) == {SUB, MAIN}
    assert _stream(selection["saved"][-1], "main")["observed_codec"] == "h265"


def test_main_preference_checks_only_the_main_stream(selection):
    wp.P2P_STREAM_PREFERENCE = "main"
    selection["codecs"] = {MAIN: "h264", SUB: "h264"}
    assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")
    assert selection["probes"] == [MAIN]


def test_main_preference_with_an_h265_main_stream_is_transcoded(selection):
    # Codex review (2026-10-10, high): ANYAICAM_LIVE_P2P_STREAM=main must not bypass the codec check.
    wp.P2P_STREAM_PREFERENCE = "main"
    selection["codecs"] = {MAIN: "hevc"}
    assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "transcode")


def test_a_cached_choice_is_rechecked_after_the_recheck_interval(selection, monkeypatch):
    selection["codecs"] = {SUB: "h264", MAIN: "h264"}
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "substream")
    probes = len(selection["probes"])
    monkeypatch.setattr(wp, "P2P_SOURCE_RECHECK_SECONDS", 0.0)
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}  # camera reconfigured, same main URL
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "transcode")
    assert len(selection["probes"]) > probes


def test_an_old_format_cache_entry_is_rechecked(selection):
    wp._p2p_source_choice["cam-x"] = (MAIN, SUB)  # pre-2026-10-10 tuple, no kind/timestamp
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "transcode")


def test_sync_replaces_a_path_when_an_expired_decision_changes(selection, monkeypatch):
    calls = []
    monkeypatch.setattr(wp, "_config_request", lambda method, path, payload=None: calls.append((method, path, payload)) or (200, "ok"))
    monkeypatch.setattr(wp, "_camera_map", {"cam-x": 1})
    monkeypatch.setattr(wp, "_known_paths", set())
    monkeypatch.setattr(wp, "_path_add_retries", {})
    selection["codecs"] = {SUB: "h264", MAIN: "h264"}
    wp.sync_camera_paths(lambda n: MAIN)
    assert [c[1] for c in calls] == ["/v3/config/paths/add/cam-x"]
    calls.clear()
    wp.sync_camera_paths(lambda n: MAIN)
    assert calls == []  # fresh decision: nothing re-checked, nothing replaced
    monkeypatch.setattr(wp, "P2P_SOURCE_RECHECK_SECONDS", 0.0)
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    wp.sync_camera_paths(lambda n: MAIN)
    assert [c[1] for c in calls] == ["/v3/config/paths/replace/cam-x"] and calls[0][2]["source"] == "publisher"


def test_transcoding_is_limited_per_appliance(selection, monkeypatch, caplog):
    monkeypatch.setattr(wp, "P2P_TRANSCODE_MAX_CAMERAS", 1)
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    assert wp.p2p_source_for("cam-a", MAIN)[1] == "transcode"
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert wp.p2p_source_for("cam-b", MAIN) == (MAIN, "main")  # over budget: warn-only
    assert "p2p_transcode_limit_reached camera_id=cam-b max=1" in caplog.text
    assert selection["saved"][-1]["webrtc"]["compatible"] is False


def test_records_carry_the_camera_lifecycle_field_names(selection, monkeypatch):
    _use_onvif(selection, monkeypatch, _onvif_record("H264"))
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    wp.p2p_source_for("cam-x", MAIN)
    saved = selection["saved"][-1]
    sub, main = _stream(saved, "sub"), _stream(saved, "main")
    assert (sub["stream_role"], sub["advertised_codec"], sub["observed_codec"]) == ("substream", "h264", "h265")
    assert (main["stream_role"], main["advertised_codec"], main["observed_codec"]) == ("main", "h264", "h265")
    assert sub["role"] == "sub" and sub["codec"] == "H264"  # legacy fields kept for existing readers


def test_choice_is_cached_until_the_stream_changes(selection):
    selection["codecs"] = {SUB: "hevc", MAIN: "h264"}
    wp.p2p_source_for("cam-x", MAIN)
    probes = list(selection["probes"])
    wp.p2p_source_for("cam-x", MAIN)
    assert selection["probes"] == probes  # cached
    reprovisioned = MAIN.replace("10.0.0.9", "10.0.0.10")
    selection["codecs"][reprovisioned] = "h264"
    wp.p2p_source_for("cam-x", reprovisioned)
    assert len(selection["probes"]) > len(probes)  # a changed stream is revalidated


def test_camera_settings_are_never_written(selection, monkeypatch):
    calls = []
    monkeypatch.setattr(wp, "_onvif_soap_call", lambda: (lambda *a, **k: calls.append(a) or None))
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    wp.p2p_source_for("cam-x", MAIN)
    assert not [c for c in calls if any("SetVideoEncoderConfiguration" in str(part) for part in c)]


# ------------------------------------------------------------------ on-demand H.265 -> H.264 transcoding

def test_h265_only_camera_is_transcoded_from_the_substream(selection, monkeypatch, caplog):
    _use_onvif(selection, monkeypatch, _onvif_record("H264"))  # the Samsung camera after 15:20: both streams H.265
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "transcode")
    webrtc = selection["saved"][-1]["webrtc"]
    assert webrtc["compatible"] is True and webrtc["transcoded"] is True and webrtc["source_role"] == "transcode"
    assert "transcoded to H.264" in webrtc["warning"]
    assert "p2p_transcode camera_id=cam-x from=substream" in caplog.text
    assert "no_webrtc_compatible_stream" not in caplog.text
    assert "s3cr" not in caplog.text and "10.0.0.9" not in caplog.text


def test_transcode_falls_back_to_main_when_the_substream_cannot_open(selection):
    selection["codecs"] = {MAIN: "hevc"}  # substream unreachable
    assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "transcode")


def test_transcode_kind_survives_a_cache_hit(selection):
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    first = wp.p2p_source_for("cam-x", MAIN)
    probes = len(selection["probes"])
    assert wp.p2p_source_for("cam-x", MAIN) == first == (SUB, "transcode")
    assert len(selection["probes"]) == probes


def test_an_h264_stream_is_never_transcoded(selection):
    selection["codecs"] = {SUB: "hevc", MAIN: "h264"}
    assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")


def test_transcode_path_config_is_on_demand_loopback_and_video_only():
    config = wp.path_config_for(SUB, "transcode")
    assert config["source"] == "publisher" and "sourceOnDemand" not in config
    assert config["runOnDemandRestart"] is True and config["runOnDemandCloseAfter"] == "10s"
    cmd = config["runOnDemand"]
    import shlex
    args = shlex.split(cmd.replace("$MTX_PATH", "cam-x"))
    assert args[0] == "ffmpeg" and args[args.index("-i") + 1] == SUB  # the camera URL is one quoted argument
    assert "-an" in args and args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-profile:v") + 1] == "baseline" and args[args.index("-bf") + 1] == "0"
    assert args[-1] == "rtsp://127.0.0.1:8554/cam-x"  # publishes back into its own path, loopback only
    assert "min(iw\,1280)" in args[args.index("-vf") + 1]
    assert wp.path_config_for(SUB, "substream") == {"source": SUB, "sourceOnDemand": True}
    assert wp.path_config_for(MAIN, "main") == {"source": MAIN, "sourceOnDemand": True}


def test_sync_adds_the_transcode_path(selection, monkeypatch):
    selection["codecs"] = {SUB: "hevc", MAIN: "hevc"}
    calls = []
    monkeypatch.setattr(wp, "_config_request", lambda method, path, payload=None: calls.append((method, path, payload)) or (200, "ok"))
    monkeypatch.setattr(wp, "_camera_map", {"cam-x": 1})
    monkeypatch.setattr(wp, "_known_paths", set())
    monkeypatch.setattr(wp, "_path_add_retries", {})
    wp.sync_camera_paths(lambda n: MAIN)
    adds = [c for c in calls if c[1] == "/v3/config/paths/add/cam-x"]
    assert len(adds) == 1 and adds[0][2]["source"] == "publisher" and "libx264" in adds[0][2]["runOnDemand"]


def test_mediamtx_config_rtsp_is_loopback_tcp_only_and_unused_servers_are_off():
    lines = wp.render_mediamtx_config().splitlines()
    for expected in ("rtsp: yes", "rtspAddress: 127.0.0.1:8554", "rtspTransports: [tcp]", "rtmp: no", "hls: no", "srt: no",
                     "webrtcAddress: 127.0.0.1:8889", "apiAddress: 127.0.0.1:9997"):
        assert expected in lines


# ------------------------------------------------------------------ revalidation after a codec rejection

def _http_error(body: bytes, code=400):
    return urllib.error.HTTPError("http://127.0.0.1:8889/cam-x/whep", code, "Bad Request", {}, io.BytesIO(body))


def test_codec_rejection_triggers_one_rate_limited_revalidation(selection, monkeypatch):
    selection["codecs"] = {SUB: "h264", MAIN: "h264"}
    wp.p2p_source_for("cam-x", MAIN)
    monkeypatch.setattr(wp, "_known_paths", {"cam-x"})
    monkeypatch.setattr(wp, "_paths_resync_pending", False)
    def reject(request, timeout=0):
        raise _http_error(b'{"error":"codecs not supported by client"}')
    monkeypatch.setattr(wp.urllib.request, "urlopen", reject)
    assert wp.whep_offer("cam-x", "v=0\r\n") is None
    assert "cam-x" not in wp._p2p_source_choice and "cam-x" not in wp._known_paths and wp._paths_resync_pending
    assert selection["record"]["probed_at"].startswith("1970")  # stored capabilities forced to re-probe
    monkeypatch.setattr(wp, "_known_paths", {"cam-x"})
    wp.whep_offer("cam-x", "v=0\r\n")  # a second rejection inside the window
    assert "cam-x" in wp._known_paths  # not re-scheduled: no probe loop for an H.265-only camera


def test_non_codec_rejection_does_not_revalidate(selection, monkeypatch):
    monkeypatch.setattr(wp, "_known_paths", {"cam-x"})
    def reject(request, timeout=0):
        raise _http_error(b'{"error":"source of path is not ready"}')
    monkeypatch.setattr(wp.urllib.request, "urlopen", reject)
    wp.whep_offer("cam-x", "v=0\r\n")
    assert "cam-x" in wp._known_paths


# ------------------------------------------------------------------ the probe itself

class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout


@pytest.mark.parametrize("payload,expected", [
    ({"streams": [{"codec_type": "video", "codec_name": "HEVC", "width": 640}]}, "h265"),
    ({"streams": [{"codec_type": "video", "codec_name": "h264", "width": 3840}]}, "h264"),
    ({"streams": [{"codec_type": "video", "codec_name": "h264", "width": 0}]}, None),
    ({"streams": [{"codec_type": "audio", "codec_name": "aac"}]}, None),
    ({"streams": []}, None),
])
def test_probe_reports_the_observed_codec(monkeypatch, payload, expected):
    seen = {}
    def fake_run(args, **kwargs):
        seen["args"] = args
        return _Result(0, json.dumps(payload))
    monkeypatch.setattr(wp.subprocess, "run", fake_run)
    assert wp._probe_video_codec(SUB) == expected
    assert "stream=codec_type,codec_name,width,height" in seen["args"]


@pytest.mark.parametrize("raw,expected", [("hevc", "h265"), ("HEV1", "h265"), ("hvc1", "h265"), ("avc1", "h264"),
                                          ("H264", "h264"), ("mjpeg", "mjpeg"), ("", None), (None, None)])
def test_codec_names_match_the_camera_lifecycle_normalization(raw, expected):
    assert wp.normalize_codec(raw) == expected


def test_probe_failures_are_none(monkeypatch):
    monkeypatch.setattr(wp.subprocess, "run", lambda args, **kw: _Result(1, ""))
    assert wp._probe_video_codec(SUB) is None
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 10)
    monkeypatch.setattr(wp.subprocess, "run", boom)
    assert wp._probe_video_codec(SUB) is None
    monkeypatch.setattr(wp.subprocess, "run", lambda args, **kw: _Result(0, "not json"))
    assert wp._probe_video_codec(SUB) is None


# ------------------------------------------------------------------ rejection reason logging

def test_whep_rejection_logs_mediamtx_reason(monkeypatch, caplog):
    monkeypatch.setattr(wp, "_codec_revalidated_at", {"cam-x": float("inf")})
    def reject(request, timeout=0):
        raise _http_error(b'{"error":"codecs not supported by client"}')
    monkeypatch.setattr(wp.urllib.request, "urlopen", reject)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert wp.whep_offer("cam-x", "v=0\r\n") is None
    assert "whep_offer_rejected path=cam-x status=400 reason=codecs not supported by client" in caplog.text


def test_whep_rejection_reason_is_sanitized(monkeypatch, caplog):
    body = (b'{"error":"source rtsp://u:s3cr%40t@10.0.0.9:554/x failed; a=ice-pwd:SECRETPWD '
            b'a=fingerprint:sha-256 AB:CD:EF candidate:1 1 udp 2122 192.168.0.5 5000 typ host"}')
    def reject(request, timeout=0):
        raise _http_error(body)
    monkeypatch.setattr(wp.urllib.request, "urlopen", reject)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        wp.whep_offer("cam-x", "v=0\r\n")
    text = caplog.text
    for secret in ("s3cr", "SECRETPWD", "AB:CD", "192.168.0.5", "10.0.0.9"):
        assert secret not in text
    assert "<rtsp-url>" in text and "ice-pwd:<redacted>" in text and "candidate:<redacted>" in text


@pytest.mark.parametrize("address", ["2600:100d:b211:4109::2a", "fe80::5868:c683:162a:bbe6%enx9c69d3a75ca1",
                                     "[fd7a:115c:a1e0::c737:2761]:443", "::1", "2001:db8:0:0:0:0:0:1"])
def test_whep_rejection_reason_redacts_ipv6(monkeypatch, caplog, address):
    def reject(request, timeout=0):
        raise _http_error(('{"error":"peer ' + address + ' unreachable"}').encode())
    monkeypatch.setattr(wp.urllib.request, "urlopen", reject)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        wp.whep_offer("cam-x", "v=0\r\n")
    core = address.strip("[]").split("]")[0].split("%")[0]
    assert core not in caplog.text and "<ip>" in caplog.text


def test_whep_rejection_with_unreadable_body_still_logs(monkeypatch, caplog):
    class Broken(urllib.error.HTTPError):
        def read(self, *a):
            raise OSError("gone")
    def reject(request, timeout=0):
        raise Broken("http://127.0.0.1:8889/cam-x/whep", 400, "Bad Request", {}, None)
    monkeypatch.setattr(wp.urllib.request, "urlopen", reject)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert wp.whep_offer("cam-x", "v=0\r\n") is None
    assert "status=400 reason=unavailable" in caplog.text


# ------------------------------------------------------------------ independent pipelines

def test_recording_and_live_hls_select_streams_independently():
    app_dir = Path(wp.__file__).parent
    main_source = (app_dir / "main.py").read_text(encoding="utf-8")
    assert "_probe_video_codec" not in main_source and "p2p_source_for" not in main_source
    record = {"streams": [{"role": "main", "uri": "m", "codec": "H264", "width": 3840},
                          {"role": "sub", "uri": "s", "codec": "H264", "width": 640}]}
    assert [s["uri"] for s in camera_capabilities.select_streams(record, "recording")] == ["m"]
    assert camera_capabilities.select_streams(record, "analytics")[0]["uri"] in ("s", "m")
