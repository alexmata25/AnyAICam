"""Regression coverage for the Samsung UI walkthrough finding: readiness
reported cameras_total: 4 on an appliance with zero real cameras, and
Investigate showed four fake "Camera 1"-"Camera 4" selectors. Root cause,
confirmed via direct read-only queries on Samsung (0 rows in `cameras`,
0 legacy CAMERA[0-9]_* env vars set): get_camera_numbers() fell back to
an UNCONDITIONAL range(1, LEGACY_DEFAULT_CAMERA_COUNT + 1) (4) whenever
the `cameras` table was empty, regardless of whether any legacy
CAMERA{n}_HOST/USERNAME/PASSWORD env var was actually configured.

The fix, entirely in get_camera_numbers()/legacy_camera_numbers_in_use()
(main.py): the fallback now only returns the legacy slot numbers that
have at least one CAMERA{n}_* env var genuinely set. Every consumer --
readiness's cameras_total, Investigate's/Camera Health's/Analytics's/
LPR's/people-counting's camera selectors, cloud_administrator_bridge()'s
synthesized camera_ids, etc. -- routes through this single function, so
this file tests the root function directly (plus the two most visible
downstream surfaces: readiness_snapshot() and Investigate's legacy
selector) rather than duplicating coverage across every one of the 60+
call sites already audited to route through it.

Five scenarios exactly as requested: fresh dynamic appliance with zero
cameras; one real dynamic camera; five real dynamic cameras; actual
legacy CAMERA1-4 configuration; no legacy env vars at all.
"""
import sqlite3

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


_LEGACY_SUFFIXES = ("HOST", "USERNAME", "PASSWORD")
_LEGACY_KEYS = [
    f"CAMERA{camera}_{suffix}"
    for camera in range(1, main.LEGACY_DEFAULT_CAMERA_COUNT + 1)
    for suffix in _LEGACY_SUFFIXES
]


@pytest.fixture(autouse=True)
def _clear_legacy_env(monkeypatch):
    """Every test starts from a clean slate -- no test leaks a legacy
    env var into another via the real process environment."""
    for key in _LEGACY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _seed_dynamic_cameras(db_path, camera_numbers):
    """Real, DB-provisioned dynamic cameras -- exactly what Wizard A/B
    provisioning and camera discovery actually write to the `cameras`
    table, matching test_customer_investigate_and_alerts.py's own
    seeding shape."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            ("cust-1", "partner-1", "Customer", "cust1@example.com", "active", "2026-01-01"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
            ("site-1", "cust-1", "Main Site", "2026-01-01"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
            ("appl-1", "cust-1", "site-1", "AIC-1", "2026-01-01"),
        )
        for number in camera_numbers:
            conn.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"cam-{number}", "cust-1", "site-1", "appl-1", number, f"Camera {number}", "2026-01-01"),
            )
        conn.commit()
        conn.close()


def _fresh_db(db_path):
    """Zero-camera baseline: real schema, no camera rows at all."""
    with override_target(sqlite_path=db_path):
        initialize_database()


def _set_legacy_env(monkeypatch, camera_numbers):
    for number in camera_numbers:
        monkeypatch.setenv(f"CAMERA{number}_HOST", f"192.168.1.{100 + number}")
        monkeypatch.setenv(f"CAMERA{number}_USERNAME", "admin")
        monkeypatch.setenv(f"CAMERA{number}_PASSWORD", "secret")


# =============================================================== legacy_camera_numbers_in_use() -- the isolated env-var check


def test_legacy_numbers_in_use_is_empty_with_no_env_vars_set():
    assert main.legacy_camera_numbers_in_use() == []


def test_legacy_numbers_in_use_returns_only_the_configured_slots(monkeypatch):
    _set_legacy_env(monkeypatch, [2])
    assert main.legacy_camera_numbers_in_use() == [2]


def test_legacy_numbers_in_use_counts_a_partially_configured_slot(monkeypatch):
    # Even one of the three env vars set is proof the operator is
    # actively relying on that legacy slot -- matches
    # legacy_camera_numbers_in_use()'s own docstring ("any", not "all").
    monkeypatch.setenv("CAMERA3_HOST", "192.168.1.103")
    assert main.legacy_camera_numbers_in_use() == [3]


def test_legacy_numbers_in_use_returns_all_four_when_fully_configured(monkeypatch):
    _set_legacy_env(monkeypatch, [1, 2, 3, 4])
    assert main.legacy_camera_numbers_in_use() == [1, 2, 3, 4]


# =============================================================== get_camera_numbers() / get_camera_count() -- the five required scenarios


def test_fresh_dynamic_appliance_with_zero_cameras_reports_zero(tmp_path):
    db_path = tmp_path / "zero.db"
    _fresh_db(db_path)
    with override_target(sqlite_path=db_path):
        assert main.get_camera_numbers() == []
        assert main.get_camera_count() == 0


def test_one_real_dynamic_camera(tmp_path):
    db_path = tmp_path / "one.db"
    _seed_dynamic_cameras(db_path, [1])
    with override_target(sqlite_path=db_path):
        assert main.get_camera_numbers() == [1]
        assert main.get_camera_count() == 1


def test_five_real_dynamic_cameras(tmp_path):
    db_path = tmp_path / "five.db"
    _seed_dynamic_cameras(db_path, [1, 2, 3, 4, 5])
    with override_target(sqlite_path=db_path):
        assert main.get_camera_numbers() == [1, 2, 3, 4, 5]
        assert main.get_camera_count() == 5


def test_actual_legacy_camera1_to_4_configuration_still_works(tmp_path, monkeypatch):
    # A real legacy installation -- no `cameras` DB rows at all, but all
    # four CAMERA1-4 env-var slots genuinely configured -- must be
    # completely unaffected by this fix.
    db_path = tmp_path / "legacy.db"
    _fresh_db(db_path)
    _set_legacy_env(monkeypatch, [1, 2, 3, 4])
    with override_target(sqlite_path=db_path):
        assert main.get_camera_numbers() == [1, 2, 3, 4]
        assert main.get_camera_count() == 4


def test_no_legacy_env_vars_returns_empty_not_four_phantom_slots(tmp_path):
    # The exact bug this fix closes: previously this scenario returned
    # range(1, 5) = [1, 2, 3, 4] unconditionally.
    db_path = tmp_path / "no_legacy.db"
    _fresh_db(db_path)
    with override_target(sqlite_path=db_path):
        assert main.get_camera_numbers() == []


def test_real_dynamic_cameras_take_precedence_over_stray_legacy_env_vars(tmp_path, monkeypatch):
    # If both a real DB camera and a legacy env var happen to be present
    # (e.g. mid-migration), the real provisioned row wins -- the legacy
    # fallback is only ever consulted when the `cameras` table is empty.
    db_path = tmp_path / "mixed.db"
    _seed_dynamic_cameras(db_path, [1])
    _set_legacy_env(monkeypatch, [1, 2, 3, 4])
    with override_target(sqlite_path=db_path):
        assert main.get_camera_numbers() == [1]


# =============================================================== readiness_snapshot()'s cameras_total -- the exact field Samsung reported as 4


def test_readiness_reports_zero_cameras_total_for_a_fresh_appliance(monkeypatch):
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    snapshot = main.readiness_snapshot()
    assert snapshot["cameras_total"] == 0


def test_readiness_reports_the_real_count_for_dynamic_cameras(monkeypatch):
    # Kept within get_supervisor_slot_count()'s guaranteed LEGACY_DEFAULT_
    # CAMERA_COUNT-slot floor -- camera_status()'s camera_process_state/
    # camera_reconnect_counts dicts are preallocated at process import
    # time and only sized past that floor by the real (import-time)
    # camera count, an unrelated, pre-existing characteristic of the
    # process-supervisor plumbing this task doesn't touch.
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2])
    snapshot = main.readiness_snapshot()
    assert snapshot["cameras_total"] == 2


# =============================================================== downstream UI surfaces: no fake Camera 1-4 selectors


def _administrator():
    return {"id": "u-admin", "email": "amata@anyaicam.com", "role": "administrator", "enabled": True, "camera_ids": []}


def test_investigate_legacy_branch_shows_no_fake_cameras_when_none_are_configured(monkeypatch):
    import partner_portal

    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    monkeypatch.setattr(main, "current_user", lambda request: _administrator())
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    result = main.investigation_page(object())
    for camera_number in range(1, main.LEGACY_DEFAULT_CAMERA_COUNT + 1):
        assert f'value="{camera_number}">Camera {camera_number}</option>' not in result


def test_analytics_shows_no_fake_cameras_when_none_are_configured(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _administrator())
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    result = main.analytics(object())
    for camera_number in range(1, main.LEGACY_DEFAULT_CAMERA_COUNT + 1):
        assert f'value="{camera_number}">Camera {camera_number}</option>' not in result


def test_camera_health_shows_a_clear_no_cameras_configured_state(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _administrator())
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [])
    result = main.camera_health_page(object())
    assert "No cameras configured" in result
    for camera_number in range(1, main.LEGACY_DEFAULT_CAMERA_COUNT + 1):
        assert f'id="camera-health-row-{camera_number}"' not in result


def test_camera_health_still_lists_real_cameras_when_configured(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _administrator())
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2])
    result = main.camera_health_page(object())
    assert "No cameras configured" not in result
    assert 'id="camera-health-row-1"' in result
    assert 'id="camera-health-row-2"' in result
