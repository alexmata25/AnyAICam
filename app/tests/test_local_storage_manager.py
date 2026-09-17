"""Automatic local recording storage management (2026-09-17): regression
coverage for local_storage_manager.py.

Every disk-usage number in this file is simulated (monkeypatched
shutil.disk_usage, or a controllable fake) and every filesystem
operation happens inside a pytest tmp_path -- no real Ryzen recording,
threshold, or disk state is ever touched here, matching the explicit
"build and test this without deleting any real Ryzen recordings during
development... simulated disk thresholds first" instruction this module
was built under.
"""

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from database_backend import override_target

import local_storage_manager as lsm


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    monkeypatch.setattr(lsm, "_camera_map", {})
    monkeypatch.setattr(lsm, "local_storage_manager_state", {
        "worker_status": "disabled", "storage_state": None, "free_percent": None,
        "last_scan_at": None, "last_cleanup_at": None, "last_error": None,
    })
    monkeypatch.setattr(lsm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(lsm, "LOCAL_STATE_FILE", tmp_path / "state" / "local_storage_state.json")
    with override_target(sqlite_path=str(tmp_path / "test_local_storage_manager.db")):
        from partner_db import initialize_database
        initialize_database()
        yield


# --------------------------------------------------------------- helpers shared by these tests

def _fake_recording_start(path: Path, camera_number: int):
    """Parses the REAL filename convention (camera{N}_{YYYY-MM-DD_HH-MM-SS}.mkv)
    -- a local, test-only reimplementation, standing in for main.py's own
    recording_start() via dependency injection (see that function's own
    real parsing logic, mirrored here so tests exercise the same shape
    without importing main.py)."""
    prefix = f"camera{camera_number}_"
    try:
        return datetime.strptime(path.stem.removeprefix(prefix), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _fake_s3_key(path: Path, camera_number: int) -> str:
    return f"recordings/camera{camera_number}/{path.name}"


def _make_recording(folder: Path, camera_number: int, start: datetime, *, size_bytes: int = 1024, age_seconds: float = 999):
    camera_folder = folder / f"camera{camera_number}"
    camera_folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{start.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = camera_folder / name
    path.write_bytes(b"x" * size_bytes)
    mtime = time.time() - age_seconds
    import os
    os.utime(path, (mtime, mtime))
    return path


def _seed_camera(camera_id: str, camera_number: int, recording_mode):
    lsm._camera_map[camera_id] = {"camera_number": camera_number, "recording_mode": recording_mode}


class _FakeUsage:
    def __init__(self, total, used):
        self.total = total
        self.used = used
        self.free = total - used


# --------------------------------------------------------------- disk_usage_percent / classify_storage_state


def test_disk_usage_percent_computes_free_and_used_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(lsm.shutil, "disk_usage", lambda p: _FakeUsage(total=1000, used=900))
    result = lsm.disk_usage_percent(tmp_path)
    assert result["free_percent"] == pytest.approx(10.0)
    assert result["used_percent"] == pytest.approx(90.0)
    assert result["free_bytes"] == 100


@pytest.mark.parametrize("free_percent,reserved,warning,cleanup_in_progress,expected", [
    (50.0, 10, 20, False, "healthy"),
    (20.0, 10, 20, False, "warning"),
    (15.0, 10, 20, False, "warning"),
    (10.0, 10, 20, False, "critical"),
    (5.0, 10, 20, False, "critical"),
    (50.0, 10, 20, True, "cleanup_active"),
    (5.0, 10, 20, True, "cleanup_active"),
])
def test_classify_storage_state(free_percent, reserved, warning, cleanup_in_progress, expected):
    assert lsm.classify_storage_state(
        free_percent, reserved_free_percent=reserved, warning_free_percent=warning, cleanup_in_progress=cleanup_in_progress,
    ) == expected


# --------------------------------------------------------------- enumerate_eligible_recordings


def test_enumerate_skips_actively_written_recent_files(tmp_path):
    _seed_camera("cam-a", 1, None)
    _make_recording(tmp_path, 1, datetime(2026, 9, 1, 8, 0, 0), age_seconds=5)  # too new
    old = _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0), age_seconds=999)
    result = lsm.enumerate_eligible_recordings(tmp_path, recording_start_fn=_fake_recording_start)
    assert [c["path"] for c in result] == [old]


def test_enumerate_excludes_continuous_tier_cameras(tmp_path):
    _seed_camera("cam-hybrid", 1, "motion")
    _seed_camera("cam-continuous", 2, "continuous")
    _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0))
    _make_recording(tmp_path, 2, datetime(2026, 9, 1, 6, 0, 0))  # older, but continuous-tier
    result = lsm.enumerate_eligible_recordings(tmp_path, recording_start_fn=_fake_recording_start)
    assert len(result) == 1
    assert result[0]["camera_number"] == 1


def test_enumerate_treats_unset_recording_mode_as_eligible(tmp_path):
    _seed_camera("cam-a", 1, None)
    _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0))
    result = lsm.enumerate_eligible_recordings(tmp_path, recording_start_fn=_fake_recording_start)
    assert len(result) == 1


def test_enumerate_sorts_globally_chronological_across_cameras_not_per_camera(tmp_path):
    """The core "never let one camera hog the disk" requirement: the
    returned order must be a single global timeline, not cameras
    processed one-at-a-time."""
    _seed_camera("cam-a", 1, "motion")
    _seed_camera("cam-b", 2, "motion")
    a1 = _make_recording(tmp_path, 1, datetime(2026, 9, 1, 6, 0, 0))
    b1 = _make_recording(tmp_path, 2, datetime(2026, 9, 1, 7, 0, 0))
    a2 = _make_recording(tmp_path, 1, datetime(2026, 9, 1, 8, 0, 0))
    b2 = _make_recording(tmp_path, 2, datetime(2026, 9, 1, 9, 0, 0))
    result = lsm.enumerate_eligible_recordings(tmp_path, recording_start_fn=_fake_recording_start)
    assert [c["path"] for c in result] == [a1, b1, a2, b2]


def test_enumerate_ignores_malformed_filenames(tmp_path):
    _seed_camera("cam-a", 1, "motion")
    camera_folder = tmp_path / "camera1"
    camera_folder.mkdir()
    bad = camera_folder / "camera1_not-a-real-timestamp.mkv"
    bad.write_bytes(b"x" * 100)
    import os
    old_mtime = time.time() - 999
    os.utime(bad, (old_mtime, old_mtime))
    result = lsm.enumerate_eligible_recordings(tmp_path, recording_start_fn=_fake_recording_start)
    assert result == []


def test_enumerate_skips_camera_folder_that_does_not_exist(tmp_path):
    _seed_camera("cam-a", 1, "motion")
    result = lsm.enumerate_eligible_recordings(tmp_path, recording_start_fn=_fake_recording_start)
    assert result == []


# --------------------------------------------------------------- delete_one_recording (DB sync + audit)


def test_delete_one_recording_removes_file_and_matching_catalog_row(tmp_path):
    from partner_db import connection
    _seed_camera("cam-a", 1, "motion")
    path = _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0))
    s3_key = _fake_s3_key(path, 1)
    now = "2026-09-17T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p1','P',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','p1','C','c@example.test','active',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','S',?)", (now,))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at) VALUES('cam-a','cust-a','site-a','appl-a','Camera 1','configured',?)", (now,))
        db.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,duration_seconds,size_bytes,status,created_at) "
            "VALUES('rec-1','cust-a','site-a','appl-a','cam-a',?,?,?,300,1024,'available',?)",
            (s3_key, now, now, now),
        )

    candidate = {"path": path, "camera_number": 1, "camera_id": "cam-a", "started_at": datetime(2026, 9, 1, 7, 0, 0), "size_bytes": 1024, "mtime": time.time()}
    assert lsm.delete_one_recording(candidate, cloud_recording_s3_key_fn=_fake_s3_key) is True

    assert not path.exists()
    with connection() as db:
        assert db.execute("SELECT * FROM recordings WHERE id='rec-1'").fetchone() is None
        log_row = db.execute("SELECT * FROM local_storage_cleanup_log WHERE camera_id='cam-a'").fetchone()
    assert log_row is not None
    assert log_row["file_name"] == path.name
    assert log_row["size_bytes"] == 1024
    assert log_row["reason"] == "auto_cleanup_low_disk"


def test_delete_one_recording_never_touches_a_recordings_row_for_a_different_camera(tmp_path):
    from partner_db import connection
    _seed_camera("cam-a", 1, "motion")
    path = _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0))
    now = "2026-09-17T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p1','P',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','p1','C','c@example.test','active',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','S',?)", (now,))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at) VALUES('cam-a','cust-a','site-a','appl-a','Camera 1','configured',?)", (now,))
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at) VALUES('cam-OTHER','cust-a','site-a','appl-a','Camera 2','configured',?)", (now,))
        db.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,duration_seconds,size_bytes,status,created_at) "
            "VALUES('rec-other','cust-a','site-a','appl-a','cam-OTHER',?,?,?,300,1024,'available',?)",
            (_fake_s3_key(path, 1), now, now, now),
        )
    candidate = {"path": path, "camera_number": 1, "camera_id": "cam-a", "started_at": datetime(2026, 9, 1, 7, 0, 0), "size_bytes": 1024, "mtime": time.time()}
    lsm.delete_one_recording(candidate, cloud_recording_s3_key_fn=_fake_s3_key)
    with connection() as db:
        assert db.execute("SELECT * FROM recordings WHERE id='rec-other'").fetchone() is not None


def test_delete_one_recording_returns_false_for_an_already_gone_file(tmp_path):
    path = tmp_path / "camera1" / "camera1_2026-09-01_07-00-00.mkv"
    candidate = {"path": path, "camera_number": 1, "camera_id": "cam-a", "started_at": datetime(2026, 9, 1, 7, 0, 0), "size_bytes": 1024, "mtime": time.time()}
    assert lsm.delete_one_recording(candidate, cloud_recording_s3_key_fn=_fake_s3_key) is False


# --------------------------------------------------------------- run_cleanup_pass


def test_run_cleanup_pass_is_a_noop_when_already_above_target(tmp_path, monkeypatch):
    monkeypatch.setattr(lsm, "disk_usage_percent", lambda p: {"free_percent": 50.0})
    result = lsm.run_cleanup_pass(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key, policy={"reserved_free_percent": 10, "warning_free_percent": 20})
    assert result == {"deleted": 0, "freed_bytes": 0, "final_free_percent": 50.0, "exhausted": False}


def test_run_cleanup_pass_deletes_oldest_first_until_target_restored(tmp_path, monkeypatch):
    _seed_camera("cam-a", 1, "motion")
    files = [_make_recording(tmp_path, 1, datetime(2026, 9, 1, h, 0, 0), size_bytes=1000) for h in range(6, 10)]

    # Simulated disk: starts at 1000 used out of 10000 total (90% free is
    # NOT the trigger here -- start it low on purpose) -- each real
    # deletion actually shrinks disk usage by re-reading real file sizes
    # via shutil.disk_usage on tmp_path itself would be unreliable
    # cross-platform, so this test drives a controlled fake instead.
    state = {"used": 9200, "total": 10000}  # 8% free -- below the 10% reserved floor

    def fake_usage(_path):
        return _FakeUsage(total=state["total"], used=state["used"])

    monkeypatch.setattr(lsm.shutil, "disk_usage", fake_usage)

    original_delete = lsm.delete_one_recording

    def tracking_delete(candidate, *, cloud_recording_s3_key_fn):
        ok = original_delete(candidate, cloud_recording_s3_key_fn=cloud_recording_s3_key_fn)
        if ok:
            state["used"] -= candidate["size_bytes"]
        return ok

    monkeypatch.setattr(lsm, "delete_one_recording", tracking_delete)

    result = lsm.run_cleanup_pass(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key, policy={"reserved_free_percent": 10, "warning_free_percent": 20})

    assert result["deleted"] > 0
    assert result["final_free_percent"] >= 10 + lsm.CLEANUP_TARGET_MARGIN_PERCENT
    assert result["exhausted"] is False
    # Oldest files deleted first.
    remaining = sorted(tmp_path.glob("camera1/*.mkv"))
    deleted_files = [f for f in files if f not in remaining]
    assert deleted_files == files[: len(deleted_files)]


def test_run_cleanup_pass_reports_exhausted_when_nothing_left_to_delete(tmp_path, monkeypatch):
    _seed_camera("cam-a", 1, "motion")
    _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0), size_bytes=10)  # tiny -- won't move the needle

    monkeypatch.setattr(lsm.shutil, "disk_usage", lambda p: _FakeUsage(total=10000, used=9990))  # far below target throughout

    result = lsm.run_cleanup_pass(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key, policy={"reserved_free_percent": 10, "warning_free_percent": 20})
    assert result["deleted"] == 1
    assert result["exhausted"] is True


def test_run_cleanup_pass_never_deletes_a_continuous_tier_cameras_files(tmp_path, monkeypatch):
    _seed_camera("cam-continuous", 1, "continuous")
    _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0), size_bytes=1000)

    monkeypatch.setattr(lsm.shutil, "disk_usage", lambda p: _FakeUsage(total=10000, used=9500))

    result = lsm.run_cleanup_pass(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key, policy={"reserved_free_percent": 10, "warning_free_percent": 20})
    assert result["deleted"] == 0
    assert result["exhausted"] is True
    assert len(list((tmp_path / "camera1").glob("*.mkv"))) == 1


def test_run_cleanup_pass_respects_max_deletions_per_tick(tmp_path, monkeypatch):
    _seed_camera("cam-a", 1, "motion")
    for h in range(6, 12):
        _make_recording(tmp_path, 1, datetime(2026, 9, 1, h, 0, 0), size_bytes=1)
    monkeypatch.setattr(lsm, "MAX_DELETIONS_PER_TICK", 2)
    monkeypatch.setattr(lsm.shutil, "disk_usage", lambda p: _FakeUsage(total=10000, used=9990))  # never reaches target
    result = lsm.run_cleanup_pass(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key, policy={"reserved_free_percent": 10, "warning_free_percent": 20})
    assert result["deleted"] == 2


# --------------------------------------------------------------- _notify_critical_storage


def test_notify_critical_storage_posts_the_correct_event_shape(monkeypatch):
    posted = {}
    monkeypatch.setattr(lsm, "_control_plane_post", lambda path, payload: posted.update({"path": path, "payload": payload}))
    lsm._notify_critical_storage(3.5, exhausted=True)
    assert posted["path"] == "/api/appliance/events"
    event = posted["payload"]["events"][0]
    assert event["event_type"] == "storage_problem"
    assert event["camera_id"] is None
    assert event["severity"] == "critical"
    assert "3.5" in event["message"]


# --------------------------------------------------------------- the safety-critical flag gates


@pytest.mark.anyio
async def test_worker_does_nothing_at_all_when_management_disabled(monkeypatch, tmp_path):
    """P2P-equivalent safety-critical test: with the flag off, this
    worker must never touch the filesystem, the control plane, or the
    state file."""
    import asyncio
    monkeypatch.setattr(lsm, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(lsm, "MANAGEMENT_ENABLED", False)
    called = {"value": False}
    monkeypatch.setattr(lsm, "disk_usage_percent", lambda p: called.__setitem__("value", True) or {"free_percent": 100})

    task = asyncio.ensure_future(lsm.local_storage_manager_worker(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert called["value"] is False
    assert lsm.local_storage_manager_state["worker_status"] == "disabled"


@pytest.mark.anyio
async def test_worker_monitors_but_never_deletes_when_auto_delete_disabled(monkeypatch, tmp_path):
    """The explicit rollout requirement: monitoring can run safely with
    deletion still off -- files must survive untouched even while the
    worker correctly reports 'critical'."""
    import asyncio
    _seed_camera("cam-a", 1, "motion")
    path = _make_recording(tmp_path, 1, datetime(2026, 9, 1, 7, 0, 0))
    monkeypatch.setattr(lsm, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(lsm, "MANAGEMENT_ENABLED", True)
    monkeypatch.setattr(lsm, "AUTO_DELETE_ENABLED", False)
    monkeypatch.setattr(lsm, "SCAN_SECONDS", 0.01)
    monkeypatch.setattr(lsm, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(lsm, "_local_storage_policy", lambda: {"reserved_free_percent": 10, "warning_free_percent": 20})
    monkeypatch.setattr(lsm.shutil, "disk_usage", lambda p: _FakeUsage(total=10000, used=9500))  # 5% free -- below reserved
    notified = {"value": False}
    monkeypatch.setattr(lsm, "_notify_critical_storage", lambda *a, **k: notified.__setitem__("value", True))

    task = asyncio.ensure_future(lsm.local_storage_manager_worker(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert path.exists()  # never deleted
    assert lsm.local_storage_manager_state["storage_state"] == "critical"
    assert notified["value"] is True


@pytest.mark.anyio
async def test_worker_never_runs_on_cloud_role_regardless_of_flags(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(lsm, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(lsm, "MANAGEMENT_ENABLED", True)
    monkeypatch.setattr(lsm, "AUTO_DELETE_ENABLED", True)
    called = {"value": False}
    monkeypatch.setattr(lsm, "disk_usage_percent", lambda p: called.__setitem__("value", True) or {"free_percent": 100})

    task = asyncio.ensure_future(lsm.local_storage_manager_worker(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert called["value"] is False


@pytest.mark.anyio
async def test_worker_writes_local_state_file_for_the_agent_heartbeat_to_read(monkeypatch, tmp_path):
    import asyncio, json
    monkeypatch.setattr(lsm, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(lsm, "MANAGEMENT_ENABLED", True)
    monkeypatch.setattr(lsm, "AUTO_DELETE_ENABLED", False)
    monkeypatch.setattr(lsm, "SCAN_SECONDS", 0.01)
    monkeypatch.setattr(lsm, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(lsm, "_local_storage_policy", lambda: {"reserved_free_percent": 10, "warning_free_percent": 20})
    monkeypatch.setattr(lsm.shutil, "disk_usage", lambda p: _FakeUsage(total=10000, used=5000))  # healthy

    task = asyncio.ensure_future(lsm.local_storage_manager_worker(tmp_path, recording_start_fn=_fake_recording_start, cloud_recording_s3_key_fn=_fake_s3_key))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert lsm.LOCAL_STATE_FILE.exists()
    payload = json.loads(lsm.LOCAL_STATE_FILE.read_text())
    assert payload["storage_state"] == "healthy"
    assert payload["free_percent"] == pytest.approx(50.0)


@pytest.fixture
def anyio_backend():
    return "asyncio"
