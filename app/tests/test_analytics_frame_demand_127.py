"""Frame demand of disabled analytics (2026-10-17, follow-up to the 904594d activation gate).

save_yolo_events() runs whenever the detector runs for ANY active analytic (e.g. Smart Motion). For each person it
built PPE and facial-recognition crops from _crop_full_frame(), which on a substream analytics frame grabs one
main-stream frame over RTSP (ai_full_resolution_frame -> main_frame_grabber.grab). PPE checked only its camera flag
and pilot scope, facial recognition only its pilot scope -- so a disabled or unauthorized PPE/FR still opened an RTSP
grab for every person. Now each checks camera flag + appliance switch + scope (main.effective_analytics) BEFORE any
frame acquisition. LPR and the other analytics are unchanged.
"""
import sys
import types
from contextlib import contextmanager

import numpy as np
import pytest

import main

PERSON = {"x": 200, "y": 100, "width": 60, "height": 120, "class_name": "person", "confidence": 0.9}


@pytest.fixture
def scan(monkeypatch, tmp_path):
    main.ai_event_clip_windows.clear()
    main.ai_activities.reset()
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (0.0, 300.0))
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_analytics_event", lambda event: None)
    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = lambda **kwargs: True
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    monkeypatch.setattr(main, "_ai_event_media_loop", None)

    state = types.SimpleNamespace(identity={"smart_motion_enabled": True}, frame_requests=[], rtsp_grabs=[],
                                  ppe_calls=[], facial_calls=[])
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: dict(state.identity))
    full = np.zeros((2160, 3840, 3), np.uint8)

    def full_resolution_frame(n, result):  # a 4K main stream behind a substream: the real one grabs over RTSP
        state.frame_requests.append(n)
        return main.main_frame_grabber.grab(n)

    monkeypatch.setattr(main, "ai_full_resolution_frame", full_resolution_frame)
    monkeypatch.setattr(main.main_frame_grabber, "grab", lambda n: state.rtsp_grabs.append(n) or full)
    monkeypatch.setattr(main.ppe, "detect_ppe", lambda crop, camera_number=None: state.ppe_calls.append(camera_number))

    @contextmanager
    def connect():
        yield object()

    monkeypatch.setattr("database_backend.connect", connect)
    monkeypatch.setattr(main.facial_events, "record_facial_events",
                        lambda db, **kw: state.facial_calls.append(kw["camera_number"]) or [])
    # Every switch on, every camera in scope; each test turns off what it proves.
    monkeypatch.setattr(main.ppe, "PPE_ENABLED", True)
    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda n: True)
    monkeypatch.setattr(main.facial_recognition, "FACIAL_RECOGNITION_ENABLED", True)
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda n: True)

    def run(camera=4):
        frame = np.zeros((360, 640, 3), np.uint8)  # a substream analytics frame: crops would need the main stream
        main.save_yolo_events(camera, {"detections": [dict(PERSON)], "frame": frame, "frame_source": "substream"})
        return state

    state.run = run
    yield state
    main.ai_event_clip_windows.clear()
    main.ai_activities.reset()


def _nothing_acquired(state):
    return state.frame_requests == [] and state.rtsp_grabs == []


# ---------------------------------------------------------------- PPE
def test_ppe_disabled_appliance_wide_requests_no_full_resolution_frame(scan, monkeypatch):
    monkeypatch.setattr(main.ppe, "PPE_ENABLED", False)
    scan.identity.update(ppe_enabled=True)
    state = scan.run()
    assert _nothing_acquired(state) and state.ppe_calls == []


def test_ppe_outside_its_pilot_cameras_requests_no_frame(scan, monkeypatch):
    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda n: False)
    scan.identity.update(ppe_enabled=True)
    assert _nothing_acquired(scan.run())


def test_ppe_not_enabled_for_the_camera_requests_no_frame(scan):
    scan.identity.update(ppe_enabled=False)
    assert _nothing_acquired(scan.run())


def test_enabled_ppe_still_gets_its_main_stream_crop(scan):
    scan.identity.update(ppe_enabled=True)
    state = scan.run()
    assert state.frame_requests == [4] and state.rtsp_grabs == [4] and state.ppe_calls == [4]


# ---------------------------------------------------------------- facial recognition
def test_facial_recognition_disabled_appliance_wide_opens_no_rtsp(scan, monkeypatch):
    monkeypatch.setattr(main.facial_recognition, "FACIAL_RECOGNITION_ENABLED", False)
    scan.identity.update(facial_recognition_enabled=True)
    state = scan.run()
    assert _nothing_acquired(state) and state.facial_calls == []


def test_facial_recognition_not_authorized_for_the_camera_opens_no_rtsp(scan):
    scan.identity.update(facial_recognition_enabled=False)
    state = scan.run()
    assert _nothing_acquired(state) and state.facial_calls == []


def test_facial_recognition_with_no_camera_identity_yet_fails_closed(scan, monkeypatch):
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: None)
    state = scan.run()
    assert _nothing_acquired(state) and state.facial_calls == []


def test_facial_recognition_outside_its_cameras_opens_no_rtsp(scan, monkeypatch):
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda n: False)
    scan.identity.update(facial_recognition_enabled=True)
    assert _nothing_acquired(scan.run())


def test_authorized_facial_recognition_still_runs_on_the_main_stream_crop(scan):
    scan.identity.update(facial_recognition_enabled=True)
    state = scan.run()
    assert state.rtsp_grabs == [4] and state.facial_calls == [4]


def test_ppe_and_facial_recognition_together_share_one_grab(scan):
    scan.identity.update(ppe_enabled=True, facial_recognition_enabled=True)
    state = scan.run()
    assert state.rtsp_grabs == [4] and state.ppe_calls == [4] and state.facial_calls == [4]


# ---------------------------------------------------------------- the rest is unchanged
def test_with_ppe_and_facial_recognition_off_a_person_scan_acquires_nothing(scan, monkeypatch):
    """The Ryzen situation: the detector runs (Smart Motion), PPE/FR off appliance-wide -> zero RTSP grabs."""
    monkeypatch.setattr(main.ppe, "PPE_ENABLED", False)
    monkeypatch.setattr(main.facial_recognition, "FACIAL_RECOGNITION_ENABLED", False)
    scan.identity.update(ppe_enabled=True, facial_recognition_enabled=True)
    for _ in range(3):
        scan.run()
    assert _nothing_acquired(scan)


def test_independently_enabled_lpr_still_gets_its_full_resolution_frame(scan, monkeypatch):
    monkeypatch.setattr(main.ppe, "PPE_ENABLED", False)
    monkeypatch.setattr(main.facial_recognition, "FACIAL_RECOGNITION_ENABLED", False)
    monkeypatch.setattr(main, "cv2", object())
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(main.lpr, "LPR_FULL_RES_FRAMES", True, raising=False)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda n: True)
    scan.identity.update(lpr_enabled=True)
    used = []

    def scan_frame(camera_number, frame, boxes, *, full_frame=None, **kw):
        used.append(full_frame() if full_frame else None)
        return []

    monkeypatch.setattr(main.lpr, "scan_frame", scan_frame)
    car = {"x": 100, "y": 100, "width": 120, "height": 80, "class_name": "car", "confidence": 0.9}
    result = {"detections": [car], "frame": np.zeros((360, 640, 3), np.uint8), "frame_source": "substream"}
    assert main.run_lpr_scan(4, result) == []
    assert scan.rtsp_grabs == [4] and used and used[0] is not None
