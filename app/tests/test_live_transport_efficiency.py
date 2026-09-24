"""Live-view transport efficiency (2026-09-24).

1. Live encode bitrate cap: start_live_stream()'s libx264 encode had no
   rate limit, so busy scenes produced 6-18 Mbps segments -- every
   relayed byte is CloudFront/S3 cost and customer upload bandwidth. A
   VBV cap now bounds bursts while keeping native resolution (analytics
   read frames from these same segments).
2. P2P upgrade: the browser keeps trying P2P for 15s (was 4s) and may
   take over a relay-playing tile; see test_live_p2p_upgrade_browser.py
   for the cross-browser behavior run in Chromium, Edge and WebKit.
"""
import pytest

import main
import live_view_p2p


class _FakePopen:
    last_command = None

    def __init__(self, command, **kwargs):
        _FakePopen.last_command = command


@pytest.fixture()
def captured_command(monkeypatch):
    monkeypatch.setattr(main, "camera_url", lambda camera_number: "rtsp://camera.invalid/stream")
    monkeypatch.setattr(main.subprocess, "Popen", _FakePopen)
    _FakePopen.last_command = None
    return lambda: _FakePopen.last_command


def test_the_live_encode_is_bitrate_capped_by_default(captured_command, monkeypatch):
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 4000)
    main.start_live_stream(1)
    command = captured_command()
    assert command[command.index("-maxrate") + 1] == "4000k"
    assert command[command.index("-bufsize") + 1] == "8000k"


def test_the_cap_keeps_native_resolution_and_the_existing_encode_settings(captured_command, monkeypatch):
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 4000)
    main.start_live_stream(1)
    command = captured_command()
    assert "-vf" not in command and "-s" not in command  # no downscale: analytics read these frames
    for setting in ("libx264", "veryfast", "zerolatency", "-hls_time"):
        assert setting in command


def test_a_zero_cap_restores_the_previous_uncapped_encode(captured_command, monkeypatch):
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 0)
    main.start_live_stream(1)
    command = captured_command()
    assert "-maxrate" not in command and "-bufsize" not in command


def test_bitrate_cap_args_scale_with_the_configured_value(monkeypatch):
    monkeypatch.setattr(main, "LIVE_MAX_BITRATE_KBPS", 2500)
    assert main.live_bitrate_cap_args() == ["-maxrate", "2500k", "-bufsize", "5000k"]


def test_the_browser_gets_a_longer_p2p_window_by_default():
    assert live_view_p2p.P2P_NEGOTIATION_TIMEOUT_MS == 15000


def test_both_live_pages_ship_the_p2p_upgrade_and_fallback_logic():
    import live_view_page

    source = open(live_view_page.__file__, encoding="utf-8").read()
    assert source.count("return'upgrade'") == 2  # grid + single-camera claimTransport()
    assert source.count("window.watchLiveP2P(result.pc") == 2
    assert "window.watchLiveP2P = function(pc, onLost)" in source
