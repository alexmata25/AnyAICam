"""VMS 1.2.7 LPR efficiency (2026-10-17): the recording-buffer full-resolution frame is decoded lazily.

On the HLS / buffer path (ANYAICAM_AI_SOURCE=hls, or a camera whose main-stream reader fell back to HLS -- the
Ryzen's driveway cameras), run_lpr_scan used to decode the newest buffer segment to a full-resolution PNG
(~0.45-0.8 CPU-s at 1080p-1440p) on EVERY AI pass, even with no vehicle in view and the LPR scan not due.
It is now decoded only when a vehicle box is actually read, or when the no-vehicle full-frame fallback is due --
the same frame, used exactly where it was before.
"""
import numpy as np
import pytest

import lpr
import main

SMALL = np.zeros((360, 640, 3), np.uint8)
FULL = np.zeros((1440, 2560, 3), np.uint8)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(lpr, "LPR_FULL_RES_FRAMES", True)
    monkeypatch.setattr(lpr, "LPR_FULL_FRAME_FALLBACK", True)
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda n: True)
    monkeypatch.setattr(lpr, "_last_scan", {})
    monkeypatch.setattr(lpr, "_last_full_frame", {})
    monkeypatch.setattr(lpr, "should_attempt", lambda *a, **k: True)
    monkeypatch.setattr(lpr, "recognize_plate", lambda *a, **k: None)
    return monkeypatch


def _provider(calls):
    return lambda: calls.append(1) or FULL


def test_no_vehicle_and_fallback_not_due_never_decodes(env):
    env.setattr(lpr, "_due", lambda table, cam, interval, now: table is lpr._last_scan)  # scan due, fallback not
    calls = []
    lpr.scan_frame(5, SMALL, [], full_frame=_provider(calls), full_frame_for_fallback=True, now=100.0)
    assert calls == []


def test_scan_not_due_never_decodes(env):
    env.setattr(lpr, "_due", lambda *a: False)
    calls = []
    lpr.scan_frame(5, SMALL, [(1, 1, 50, 50)], full_frame=_provider(calls), full_frame_for_fallback=True, now=100.0)
    assert calls == []


def test_no_vehicle_fallback_due_decodes_once_and_reads_on_the_full_frame(env):
    env.setattr(lpr, "_due", lambda *a: True)
    seen = []
    env.setattr(lpr, "detect_plates_full_frame", lambda frame: [{"region": (100, 100, 40, 12), "detector_confidence": 0.9}])
    env.setattr(lpr, "read_detected_plate", lambda image, conf: seen.append(image.shape) or None)
    calls = []
    lpr.scan_frame(5, SMALL, [], full_frame=_provider(calls), full_frame_for_fallback=True, now=100.0)
    assert calls == [1]
    # the region was scaled onto the 2560-wide buffer frame (not read from the 640-wide analytics frame)
    assert seen and seen[0][1] > 40


def test_a_vehicle_decodes_once_per_pass(env):
    env.setattr(lpr, "_due", lambda *a: True)
    calls = []
    lpr.scan_frame(5, SMALL, [(1, 1, 50, 50), (300, 100, 60, 60)], full_frame=_provider(calls),
                   full_frame_for_fallback=True, now=100.0)
    assert calls == [1]


def test_main_stream_path_keeps_its_rule_no_grab_for_the_fallback(env):
    """Default full_frame_for_fallback=False: the RTSP grab is never made for the no-vehicle fallback."""
    env.setattr(lpr, "_due", lambda *a: True)
    env.setattr(lpr, "detect_plates_full_frame", lambda frame: [])
    lpr.scan_frame(5, SMALL, [], full_frame=lambda: pytest.fail("RTSP grab for the fallback"), now=100.0)


def test_run_lpr_scan_on_the_hls_path_hands_over_a_lazy_buffer_frame(env):
    env.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": True})
    buffer_calls, seen = [], {}
    env.setattr(lpr, "latest_full_resolution_frame", lambda n, folder: buffer_calls.append(str(folder)) or FULL)
    env.setattr(lpr, "scan_frame", lambda *a, **k: seen.update(k) or [])
    main.run_lpr_scan(2, {"frame": SMALL, "frame_source": "hls", "detections": []})
    assert buffer_calls == []
    assert seen["full_frame_for_fallback"] is True and seen["full_frame"]() is FULL
    assert buffer_calls and buffer_calls[0].endswith("_event_buffer")


def test_run_lpr_scan_main_stream_path_is_unchanged(env):
    env.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": True})
    seen = {}
    env.setattr(lpr, "scan_frame", lambda *a, **k: seen.update(k) or [])
    car = {"class_name": "car", "x": 1, "y": 1, "width": 50, "height": 50, "confidence": 0.9}
    main.run_lpr_scan(2, {"frame": SMALL, "frame_source": "substream", "detections": [car]})
    assert "full_frame_for_fallback" not in seen and callable(seen["full_frame"])
