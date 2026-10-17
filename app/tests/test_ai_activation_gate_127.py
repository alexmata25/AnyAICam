"""VMS 1.2.7: analytics activation gate (Yellow's Ryzen finding, 2026-10-17).

A camera analytic counts as active only when BOTH its camera-level flag and the appliance-wide switch / pilot scope
that the analytic's own processing applies allow it. If no analytic is effectively active, the shared YOLO detector
pauses, so the full-resolution AI frame reader (idle-stopped by analytics_frames) is never asked for frames.
Seen on the Ryzen: cameras 2/3 with ppe_enabled=1 while ANYAICAM_PPE_ENABLED=false kept YOLO and a 1440p reader
running (20-27 % CPU each) with every analytic off in the UI.
"""
import pytest

import main

ALL_OFF = {"smart_motion_enabled": False, "people_counting_enabled": False, "lpr_enabled": False,
           "ppe_enabled": False, "facial_recognition_enabled": False}


@pytest.fixture()
def globals_off(monkeypatch):
    """Every appliance-wide analytic switch off; each test turns on what it needs."""
    monkeypatch.setattr(main, "PEOPLE_COUNTING_ENABLED", False)
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", False)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera: True)
    monkeypatch.setattr(main.ppe, "PPE_ENABLED", False)
    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda camera: True)
    monkeypatch.setattr(main.facial_recognition, "FACIAL_RECOGNITION_ENABLED", False)
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda camera: True)
    monkeypatch.setattr(main.smart_motion, "SMART_MOTION_ENABLED", False)
    return monkeypatch


# ------------------------------------------------------------------ effective_analytics()
def test_camera_ppe_on_but_ppe_disabled_appliance_wide_is_not_active(globals_off):
    """The Ryzen case: cameras 2/3 have ppe_enabled=1, PPE is off appliance-wide."""
    assert main.effective_analytics({**ALL_OFF, "ppe_enabled": True}, 2) == []


def test_camera_ppe_on_and_ppe_enabled_is_active(globals_off):
    globals_off.setattr(main.ppe, "PPE_ENABLED", True)
    assert main.effective_analytics({**ALL_OFF, "ppe_enabled": True}, 2) == ["ppe_enabled"]


def test_ppe_pilot_scope_excluding_the_camera_is_not_active(globals_off):
    globals_off.setattr(main.ppe, "PPE_ENABLED", True)
    globals_off.setattr(main.ppe, "is_camera_enabled", lambda camera: camera != 2)
    assert main.effective_analytics({**ALL_OFF, "ppe_enabled": True}, 2) == []
    assert main.effective_analytics({**ALL_OFF, "ppe_enabled": True}, 1) == ["ppe_enabled"]


def test_mixed_flags_only_the_globally_enabled_ones_count(globals_off):
    globals_off.setattr(main.lpr, "LPR_ENABLED", True)
    identity = {**ALL_OFF, "lpr_enabled": True, "ppe_enabled": True, "facial_recognition_enabled": True,
                "people_counting_enabled": True}
    assert main.effective_analytics(identity, 3) == ["lpr_enabled"]


def test_independently_enabled_lpr_is_preserved(globals_off):
    """LPR authorised on the camera and enabled on the appliance stays active even with PPE off everywhere."""
    globals_off.setattr(main.lpr, "LPR_ENABLED", True)
    assert main.effective_analytics({**ALL_OFF, "lpr_enabled": True, "ppe_enabled": True}, 1) == ["lpr_enabled"]


def test_lpr_camera_scope_is_respected(globals_off):
    globals_off.setattr(main.lpr, "LPR_ENABLED", True)
    globals_off.setattr(main.lpr, "is_camera_enabled", lambda camera: camera in (1, 4))
    assert main.effective_analytics({**ALL_OFF, "lpr_enabled": True}, 2) == []


def test_smart_motion_needs_the_camera_flag_and_the_appliance_switch(globals_off):
    """Codex 2026-10-17: Smart Motion was the one analytic whose appliance-wide switch the gate ignored."""
    assert main.effective_analytics({**ALL_OFF, "smart_motion_enabled": True}, 1) == []
    globals_off.setattr(main.smart_motion, "SMART_MOTION_ENABLED", True)
    assert main.effective_analytics({**ALL_OFF, "smart_motion_enabled": True}, 1) == ["smart_motion_enabled"]
    assert main.effective_analytics(ALL_OFF, 1) == []


def test_people_counting_needs_the_appliance_switch(globals_off):
    assert main.effective_analytics({**ALL_OFF, "people_counting_enabled": True}, 1) == []
    globals_off.setattr(main, "PEOPLE_COUNTING_ENABLED", True)
    assert main.effective_analytics({**ALL_OFF, "people_counting_enabled": True}, 1) == ["people_counting_enabled"]


def test_facial_recognition_needs_the_appliance_switch_and_scope(globals_off):
    assert main.effective_analytics({**ALL_OFF, "facial_recognition_enabled": True}, 1) == []
    globals_off.setattr(main.facial_recognition, "FACIAL_RECOGNITION_ENABLED", True)
    assert main.effective_analytics({**ALL_OFF, "facial_recognition_enabled": True}, 1) == ["facial_recognition_enabled"]
    globals_off.setattr(main.facial_recognition, "is_camera_enabled", lambda camera: False)
    assert main.effective_analytics({**ALL_OFF, "facial_recognition_enabled": True}, 1) == []


@pytest.mark.parametrize("identity", [None, {}, ALL_OFF])
def test_no_identity_or_all_off_is_not_active(globals_off, identity):
    assert main.effective_analytics(identity, 1) == []


def test_a_failing_scope_check_fails_closed_for_that_analytic_only(globals_off):
    globals_off.setattr(main.ppe, "PPE_ENABLED", True)

    def boom(camera):
        raise RuntimeError("bad env")
    globals_off.setattr(main.ppe, "is_camera_enabled", boom)
    globals_off.setattr(main.smart_motion, "SMART_MOTION_ENABLED", True)
    identity = {**ALL_OFF, "ppe_enabled": True, "smart_motion_enabled": True}
    assert main.effective_analytics(identity, 1) == ["smart_motion_enabled"]


# ------------------------------------------------------------------ the detector itself
def _run_detector_once(monkeypatch, identity):
    """Drive one ai_person_detector() pass; returns (frames_requested, state)."""
    class Stop(BaseException):
        pass

    requested = []

    async def no_sleep(_seconds):
        raise Stop()

    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_detect(camera):
        requested.append(camera)
        return {"ok": True, "frame": None, "frame_captured_at": None, "detections": []}

    class ImmediateSemaphore:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main, "cv2", object())
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "get_yolo_model", lambda: None)
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (30.0, 300.0))
    monkeypatch.setattr(main, "detect_objects_frame", fake_detect)
    monkeypatch.setattr(main, "save_yolo_events", lambda camera, result: [])
    monkeypatch.setattr(main.asyncio, "to_thread", direct)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main, "ai_inference_semaphore", ImmediateSemaphore())
    monkeypatch.setattr(main, "_ai_event_media_loop", object())
    monkeypatch.setitem(main.ai_detection_state, 7, main._new_ai_state())
    monkeypatch.setitem(main.ai_person_last_event, 7, -1e18)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: dict(identity, camera_id="cam-7"))
    detector = main.ai_person_detector(7)
    try:
        detector.send(None)
    except Stop:
        pass
    finally:
        detector.close()
    return requested, main.ai_detection_state[7]


def test_detector_pauses_and_requests_no_frame_when_ppe_is_off_appliance_wide(globals_off):
    requested, state = _run_detector_once(globals_off, {**ALL_OFF, "ppe_enabled": True})
    assert requested == []
    assert state.get("status") == "paused" and state.get("pause_reason") == "analytics_disabled"


def test_detector_pauses_when_smart_motion_is_off_appliance_wide(globals_off):
    requested, state = _run_detector_once(globals_off, {**ALL_OFF, "smart_motion_enabled": True})
    assert requested == [] and state.get("pause_reason") == "analytics_disabled"


def test_detector_runs_for_smart_motion_with_its_switch_on(globals_off):
    globals_off.setattr(main.smart_motion, "SMART_MOTION_ENABLED", True)
    requested, state = _run_detector_once(globals_off, {**ALL_OFF, "smart_motion_enabled": True})
    assert requested == [7] and state.get("pause_reason") is None


def test_detector_runs_when_an_analytic_is_effectively_active(globals_off):
    globals_off.setattr(main.lpr, "LPR_ENABLED", True)
    requested, state = _run_detector_once(globals_off, {**ALL_OFF, "lpr_enabled": True, "ppe_enabled": True})
    assert requested == [7]
    assert state.get("pause_reason") is None


# ------------------------------------------------------------------ latency: map refreshed right after each edge sync
def _run_sync_worker_once(monkeypatch, status):
    import asyncio
    import edge_camera_sync
    import recording_uploader

    class Stop(BaseException):
        pass

    refreshed = []

    async def no_sleep(_seconds):
        raise Stop()

    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "sync_provisioned_cameras", lambda: {"status": status})
    monkeypatch.setattr(edge_camera_sync.product_mode, "restart_if_running_flags_stale", lambda: None)
    monkeypatch.setattr(recording_uploader, "_refresh_camera_map", lambda **kw: refreshed.append(kw))
    monkeypatch.setattr(edge_camera_sync.asyncio, "to_thread", direct)
    monkeypatch.setattr(edge_camera_sync.asyncio, "sleep", no_sleep)
    worker = edge_camera_sync.camera_configuration_sync_worker()
    try:
        worker.send(None)
    except Stop:
        pass
    finally:
        worker.close()
    return refreshed


def test_a_successful_edge_sync_refreshes_the_analytics_map_immediately(monkeypatch):
    """Yellow, Ryzen 2026-10-17: a UI analytics change took up to ~7 min (300 s map refresh + 120 s reader idle)."""
    assert _run_sync_worker_once(monkeypatch, "ok") == [{}]


@pytest.mark.parametrize("status", ["not_activated", "error", "cloud_unavailable"])
def test_no_refresh_when_the_sync_did_not_succeed(monkeypatch, status):
    assert _run_sync_worker_once(monkeypatch, status) == []


def test_a_failing_refresh_never_breaks_the_sync_worker(monkeypatch):
    import edge_camera_sync
    import recording_uploader

    def boom(**_kw):
        raise RuntimeError("db locked")
    monkeypatch.setattr(recording_uploader, "_refresh_camera_map", boom)
    edge_camera_sync.refresh_analytics_camera_map()  # logs, never raises
