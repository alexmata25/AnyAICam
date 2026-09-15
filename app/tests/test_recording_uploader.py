"""R3 (recording-pipeline roadmap): focused tests for
recording_uploader.py's pure/filesystem logic -- no network, no AWS, no
DB. Uses monkeypatch to point RECORDINGS_FOLDER at a pytest tmp_path
for the duration of each test, so nothing here ever touches the real
/app/recordings path.
"""

from datetime import datetime, timedelta, timezone

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_recordings_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    yield tmp_path


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _touch(path, name, mtime_offset_seconds=0):
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / name
    file_path.write_bytes(b"fake-mkv-bytes")
    if mtime_offset_seconds:
        import os
        stat = file_path.stat()
        os.utime(file_path, (stat.st_atime, stat.st_atime + mtime_offset_seconds))
    return file_path


def test_filename_pattern_matches_start_recording_shape():
    pattern = ru._recording_filename_pattern(1)
    assert pattern.fullmatch("camera1_2026-08-21_00-05-00.mkv")
    assert not pattern.fullmatch("camera2_2026-08-21_00-05-00.mkv")  # different camera
    assert not pattern.fullmatch("camera1_2026-08-21_00-05-00.mp4")  # wrong extension
    assert not pattern.fullmatch("camera10_2026-08-21_00-05-00.mkv")  # camera1 must not match camera10's files
    assert not pattern.fullmatch("../camera1_2026-08-21_00-05-00.mkv")  # path traversal attempt


def test_recording_started_at_parses_the_embedded_timestamp(tmp_path):
    path = tmp_path / "camera1" / "camera1_2026-08-21_00-05-00.mkv"
    started = ru._recording_started_at(path, 1)
    assert started == datetime(2026, 8, 21, 0, 5, 0)


def test_recording_started_at_returns_none_for_unparseable_name(tmp_path):
    path = tmp_path / "camera1" / "camera1_not-a-real-timestamp.mkv"
    assert ru._recording_started_at(path, 1) is None


def test_newest_file_is_excluded_as_still_open(_isolated_recordings_folder):
    folder = _isolated_recordings_folder / "camera1"
    _touch(folder, "camera1_2026-08-21_00-00-00.mkv", mtime_offset_seconds=0)
    _touch(folder, "camera1_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    completed = [item.name for item in ru._completed_recording_files(1)]
    assert completed == ["camera1_2026-08-21_00-00-00.mkv"]


def test_single_file_is_never_uploaded_while_it_might_still_be_open(_isolated_recordings_folder):
    folder = _isolated_recordings_folder / "camera1"
    _touch(folder, "camera1_2026-08-21_00-00-00.mkv")
    assert ru._completed_recording_files(1) == []


def test_no_folder_yet_returns_empty_not_an_error(_isolated_recordings_folder):
    assert ru._completed_recording_files(1) == []


def test_completed_files_ignore_a_different_cameras_folder(_isolated_recordings_folder):
    folder1 = _isolated_recordings_folder / "camera1"
    folder2 = _isolated_recordings_folder / "camera2"
    _touch(folder1, "camera1_2026-08-21_00-00-00.mkv")
    _touch(folder1, "camera1_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    _touch(folder2, "camera2_2026-08-21_00-00-00.mkv")
    _touch(folder2, "camera2_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    completed_1 = [item.name for item in ru._completed_recording_files(1)]
    completed_2 = [item.name for item in ru._completed_recording_files(2)]
    assert completed_1 == ["camera1_2026-08-21_00-00-00.mkv"]
    assert completed_2 == ["camera2_2026-08-21_00-00-00.mkv"]


def test_pending_excludes_already_uploaded_files(_isolated_recordings_folder):
    folder = _isolated_recordings_folder / "camera1"
    _touch(folder, "camera1_2026-08-21_00-00-00.mkv")
    _touch(folder, "camera1_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    pending = ru._pending_recording_files(1, already_uploaded={"camera1_2026-08-21_00-00-00.mkv"})
    assert pending == []


def test_session_expires_soon_treats_missing_expiration_as_expired():
    assert ru._session_expires_soon({}) is True
    assert ru._session_expires_soon({"expires_at": None}) is True
    assert ru._session_expires_soon({"expires_at": "not-a-date"}) is True


def test_session_expires_soon_treats_timezone_naive_as_expired():
    naive = datetime.now() + timedelta(hours=1)
    assert ru._session_expires_soon({"expires_at": naive.isoformat()}) is True


def test_session_expires_soon_false_for_a_healthy_future_expiration():
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert ru._session_expires_soon({"expires_at": future.isoformat()}) is False


def test_session_expires_soon_true_within_the_renewal_margin():
    soon = datetime.now(timezone.utc) + timedelta(seconds=ru.SESSION_RENEW_MARGIN_SECONDS - 5)
    assert ru._session_expires_soon({"expires_at": soon.isoformat()}) is True


# --------------------------------------------------------- controlled-rollout camera scope + hard total cap


@pytest.fixture(autouse=True)
def _reset_scope_and_cap_and_uploaded_state(monkeypatch):
    """RECORDING_UPLOAD_CAMERA_SCOPE/RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA
    and _uploaded_files are all module-level state that must not leak
    between tests -- explicit reset (not relying on import order) matches
    this file's own existing _isolated_recordings_folder precedent."""
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", None)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", None)
    previous = dict(ru._uploaded_files)
    ru._uploaded_files.clear()
    yield
    ru._uploaded_files.clear()
    ru._uploaded_files.update(previous)


def test_camera_scope_unset_restricts_nothing():
    assert ru.RECORDING_UPLOAD_CAMERA_SCOPE is None


def test_total_cap_unset_never_blocks():
    assert ru.RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA is None
    ru._uploaded_files[1] = ["a.mkv", "b.mkv", "c.mkv"]
    assert ru._camera_at_or_over_total_cap(1) is False


def test_total_cap_blocks_once_reached(monkeypatch):
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", 1)
    ru._uploaded_files[1] = ["camera1_2026-08-21_00-00-00.mkv"]
    assert ru._camera_at_or_over_total_cap(1) is True


def test_total_cap_does_not_affect_a_different_camera(monkeypatch):
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", 1)
    ru._uploaded_files[1] = ["camera1_2026-08-21_00-00-00.mkv"]
    assert ru._camera_at_or_over_total_cap(2) is False


def test_total_cap_allows_up_to_but_not_beyond_the_configured_count(monkeypatch):
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", 2)
    ru._uploaded_files[1] = ["a.mkv"]
    assert ru._camera_at_or_over_total_cap(1) is False
    ru._uploaded_files[1] = ["a.mkv", "b.mkv"]
    assert ru._camera_at_or_over_total_cap(1) is True


# ---------------------------------------------------- worker loop actually honors both


@pytest.mark.anyio
async def test_worker_never_calls_relay_for_a_camera_outside_scope(monkeypatch):
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", frozenset({1}))
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.02)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": f"cam-{n}", "site_id": "site-1", "cloud_recording_mode": None})
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    import asyncio
    task = asyncio.ensure_future(ru.recording_upload_worker())
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert 1 in calls
    assert 2 not in calls


@pytest.mark.anyio
async def test_worker_never_calls_relay_again_once_a_camera_hits_its_hard_total_cap(monkeypatch):
    """The scenario this whole mechanism exists for: even across many
    scan iterations, a camera that has already reached its configured
    total must never be relayed again -- a hard limit, not merely a
    per-scan one."""
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", frozenset({1}))
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", 1)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.02)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": "cam-1", "site_id": "site-1", "cloud_recording_mode": None})

    def fake_relay(camera_number, camera_id):
        # Simulates a real successful upload's own bookkeeping.
        ru._remember_uploaded(camera_number, "camera1_2026-08-21_00-00-00.mkv")

    monkeypatch.setattr(ru, "_relay_camera_once", fake_relay)

    import asyncio
    task = asyncio.ensure_future(ru.recording_upload_worker())
    # Several scan intervals' worth of real time -- proves the cap holds
    # across many iterations, not just the first one.
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ru._uploaded_files.get(1) == ["camera1_2026-08-21_00-00-00.mkv"]


@pytest.mark.anyio
async def test_worker_starts_for_a_pilot_camera_even_when_globally_disabled(monkeypatch):
    """2026-09-15: the exact defect confirmed live on Ryzen.
    ANYAICAM_RECORDING_UPLOAD_CAMERAS=1 (RECORDING_UPLOAD_CAMERA_SCOPE)
    was already configured there specifically to validate one pilot
    camera without flipping RECORDING_UPLOAD_ENABLED globally -- see
    that constant's own module-level comment -- but recording_upload_
    worker()'s top-level gate checked RECORDING_UPLOAD_ENABLED alone,
    before ever consulting the scope, so the worker never started at
    all and the pilot mechanism was dead code. A non-empty scope must
    now start the worker on its own; RECORDING_UPLOAD_ENABLED staying
    False must still mean only the scoped camera(s) are ever relayed,
    proven here by camera 2 (outside the scope) never being called."""
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", False)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", frozenset({1}))
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.02)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": f"cam-{n}", "site_id": "site-1", "cloud_recording_mode": None})
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    import asyncio
    task = asyncio.ensure_future(ru.recording_upload_worker())
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ru.recording_upload_state["worker_status"] != "disabled"
    assert 1 in calls
    assert 2 not in calls


@pytest.mark.anyio
async def test_worker_stays_disabled_with_no_scope_and_globally_off(monkeypatch):
    """Regression lock in the other direction: a deployment that opts
    into neither RECORDING_UPLOAD_ENABLED nor a pilot scope must still
    get the original, unchanged disabled behavior."""
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", False)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", None)
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2])

    import asyncio
    task = asyncio.ensure_future(ru.recording_upload_worker())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ru.recording_upload_state["worker_status"] == "disabled"
    assert calls == []


@pytest.mark.anyio
async def test_worker_relays_normally_when_scope_and_cap_are_both_unset(monkeypatch):
    """Regression lock: neither new mechanism changes existing behavior
    for a deployment that doesn't opt into either."""
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.02)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2, 3, 4, 5])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": f"cam-{n}", "site_id": "site-1", "cloud_recording_mode": None})
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    import asyncio
    task = asyncio.ensure_future(ru.recording_upload_worker())
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert set(calls) == {1, 2, 3, 4, 5}
