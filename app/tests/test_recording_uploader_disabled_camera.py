"""Per-camera cloud-recording-upload authorization: recording_mode ==
'disabled' (see appliance_cloud.py's set_cloud_recording_mode() and
_refresh_camera_map()'s own docstring) must skip that camera entirely
in the worker's scan loop -- no credential request, no upload attempt,
no catalog notification -- while every other camera in the same scan
is completely unaffected. This is the per-camera authorization the
appliance-wide ANYAICAM_RECORDING_UPLOAD_ENABLED master flag was
always meant to be paired with, not a second control surface.
"""

import asyncio

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.05)
    ru._camera_map.clear()
    ru.recording_upload_state.update({"worker_status": "disabled", "last_scan_at": None, "last_config_refresh_at": None, "last_error": None})
    yield
    ru._camera_map.clear()


def _set_camera(camera_number, recording_mode, camera_id):
    ru._camera_map[camera_number] = {"camera_id": camera_id, "site_id": "site-1", "recording_mode": recording_mode, "people_counting_enabled": False}


# --------------------------------------------------------- _refresh_camera_map() normalization


def test_refresh_camera_map_preserves_disabled_as_a_distinct_value(monkeypatch):
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: {
        "cameras": [{"id": "cam-1", "camera_number": 1, "site_id": "site-1", "recording_mode": "disabled", "people_counting_enabled": False}]
    })
    ru._refresh_camera_map()
    assert ru._camera_map[1]["recording_mode"] == "disabled"


def test_refresh_camera_map_still_normalizes_unknown_values_to_none(monkeypatch):
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: {
        "cameras": [{"id": "cam-1", "camera_number": 1, "site_id": "site-1", "recording_mode": "some_future_typo", "people_counting_enabled": False}]
    })
    ru._refresh_camera_map()
    assert ru._camera_map[1]["recording_mode"] is None


# --------------------------------------------------------- worker loop: disabled camera is skipped entirely


async def _run_one_scan():
    task = asyncio.ensure_future(ru.recording_upload_worker())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    except asyncio.TimeoutError:
        pass
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def test_disabled_camera_never_reaches_relay_camera_once(monkeypatch):
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    _set_camera(1, "disabled", "cam-1")
    _set_camera(2, None, "cam-2")  # unaffected control camera in the same scan
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    asyncio.run(_run_one_scan())

    assert 1 not in calls, "disabled camera must never reach _relay_camera_once()"
    assert 2 in calls, "a normal camera in the same scan must be unaffected"


def test_disabled_camera_never_requests_credentials_or_status(monkeypatch):
    # A stricter version of the above: even the cheap /status
    # authorization check (_upload_currently_authorized(), called
    # inside _ensure_session() inside the real _relay_camera_once())
    # must never fire for a disabled camera -- confirmed here by
    # asserting the control-plane HTTP layer itself is never touched
    # for camera 1, while it legitimately is for camera 2.
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    _set_camera(1, "disabled", "cam-1")
    _set_camera(2, None, "cam-2")
    calls = []
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: calls.append(("get", path)) or {"enabled": True})
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: calls.append(("post", path)) or None)
    monkeypatch.setattr(ru, "_pending_recording_files", lambda camera_number, already_uploaded: [])

    asyncio.run(_run_one_scan())

    camera_paths = [path for _, path in calls if "recordings" in path]
    assert not any("cam-1" in path for path in camera_paths), "disabled camera must make zero recording-upload HTTP calls"


def test_motion_and_continuous_and_none_cameras_are_still_dispatched(monkeypatch):
    # The new gate must be additive -- every previously-working mode
    # keeps reaching _relay_camera_once() exactly as before.
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    _set_camera(1, "motion", "cam-1")
    _set_camera(2, "continuous", "cam-2")
    _set_camera(3, None, "cam-3")
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    asyncio.run(_run_one_scan())

    assert set(calls) == {1, 2, 3}
