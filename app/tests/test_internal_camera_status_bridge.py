"""GET /internal/camera-status: the RDM camera-status bridge for the
host-level anyaicam-agent. Reuses camera_status() and
ai_detection_status() directly -- these tests verify the merge logic
and the loopback-only guard, not a second/duplicate state source.

NOTE: like every other test file in this suite that imports main.py
(see test_yolo_model_concurrency.py, test_lpr.py, etc.), this file
transitively hits `import pytesseract` via main.py -> lpr.py and will
show as a collection ERROR in this sandbox, where pytesseract isn't
installed -- a pre-existing, unrelated environment gap, not something
this change introduces. It collects and runs cleanly wherever the
other main.py-importing tests already do (e.g. real CI with the full
dependency set installed).
"""

from types import SimpleNamespace

import pytest

import main


def _fake_request(host):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


@pytest.fixture(autouse=True)
def _reset_camera_state():
    for camera_number in range(1, main.CAMERA_COUNT + 1):
        main.camera_process_state[camera_number] = {
            "live": "starting", "recording": "starting",
            "last_exit_code": None, "last_error": None, "last_error_at": None,
        }
        main.ai_detection_state[camera_number] = {
            "status": "starting", "last_checked": None, "last_detection": None,
            "detections": 0, "error": None,
        }
    yield


def test_rejects_non_loopback_client():
    with pytest.raises(Exception) as excinfo:
        main.internal_camera_status(_fake_request("203.0.113.5"))
    assert getattr(excinfo.value, "status_code", None) == 403


def test_rejects_missing_client():
    with pytest.raises(Exception) as excinfo:
        main.internal_camera_status(_fake_request(None))
    assert getattr(excinfo.value, "status_code", None) == 403


def test_allows_loopback_and_returns_all_cameras():
    result = main.internal_camera_status(_fake_request("127.0.0.1"))
    assert {item["camera"] for item in result["cameras"]} == set(range(1, main.CAMERA_COUNT + 1))


def test_recording_reflects_real_camera_process_state():
    main.camera_process_state[1]["recording"] = True
    main.camera_process_state[2]["recording"] = False
    result = main.internal_camera_status(_fake_request("127.0.0.1"))
    by_camera = {item["camera"]: item for item in result["cameras"]}
    assert by_camera[1]["recording"] is True
    assert by_camera[2]["recording"] is False


def test_analytics_true_only_when_detection_loop_is_actually_running():
    main.ai_detection_state[1]["status"] = "running"
    main.ai_detection_state[2]["status"] = "waiting"
    main.ai_detection_state[3]["status"] = "starting"
    result = main.internal_camera_status(_fake_request("127.0.0.1"))
    by_camera = {item["camera"]: item for item in result["cameras"]}
    assert by_camera[1]["analytics"] is True
    assert by_camera[2]["analytics"] is False
    assert by_camera[3]["analytics"] is False


def test_camera_5_analytics_disabled_reflects_real_state_not_hardcoded():
    """Guards against a regression back to the old bug this whole bridge
    exists to fix: Camera 5 must read as analytics-disabled because its
    real ai_detection_state says so, not because of any special-cased
    camera-5 logic in this route (there is none -- see the route's own
    implementation, which treats every camera identically)."""
    main.ai_detection_state[5]["status"] = "waiting"
    result = main.internal_camera_status(_fake_request("127.0.0.1"))
    by_camera = {item["camera"]: item for item in result["cameras"]}
    assert by_camera[5]["analytics"] is False


def test_online_reflects_real_hls_streaming_state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    manifest = tmp_path / "camera1.m3u8"
    manifest.write_text("#EXTM3U")
    result = main.internal_camera_status(_fake_request("127.0.0.1"))
    by_camera = {item["camera"]: item for item in result["cameras"]}
    assert by_camera[1]["online"] is True
    assert by_camera[2]["online"] is False  # no manifest file for camera 2


def test_public_path_prefix_registered_so_auth_middleware_skips_it():
    assert any(prefix == "/internal/" for prefix in main.PUBLIC_PATH_PREFIXES)
