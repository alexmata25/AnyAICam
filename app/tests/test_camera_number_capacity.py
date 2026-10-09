"""Stable camera-number capacity, exhaustion and request-model boundaries."""
import sqlite3
from datetime import datetime, timezone

from database_backend import override_target
from partner_db import connection
from test_camera_slot_enforcement import (
    _appliance_headers, _grant_slots, _owner_cookie, _provision_request, _seed_and_activate_appliance, _seed_tenant,
    _session_cookie_name,
    client, db_path,
)


def test_camera_mapping_supports_the_licensed_numbering_range_and_rejects_overflow():
    from camera_mapping import MAX_CAMERA_NUMBER, assign_camera_number, next_available_camera_number
    import pytest

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_number INTEGER)")
    db.execute("INSERT INTO cameras(id,customer_id,appliance_id) VALUES('cam','cust','appl')")
    assert MAX_CAMERA_NUMBER == 10_000
    assign_camera_number(db, "cam", MAX_CAMERA_NUMBER, appliance_id="appl", customer_id="cust")
    assert db.execute("SELECT camera_number FROM cameras WHERE id='cam'").fetchone()[0] == MAX_CAMERA_NUMBER
    with pytest.raises(ValueError):
        assign_camera_number(db, "cam", MAX_CAMERA_NUMBER + 1, appliance_id="appl", customer_id="cust")
    assert next_available_camera_number(range(1, MAX_CAMERA_NUMBER + 1)) is None
    assert next_available_camera_number(range(1, 257)) == 257


def test_vms_camera_request_models_and_live_relay_accept_the_supported_upper_bound():
    import main
    from live_relay_uploader import _validate_camera_number

    main.EventSettingsModel(camera=10_000)
    main.AlertRuleModel(camera=10_000)
    main.AnalyticsRuleModel(camera=10_000, name="Upper-bound camera", analytic_type="motion")
    main.SnapshotRequest(camera=10_000, image_data="data:image/jpeg;base64,AA==")
    now = datetime.now(timezone.utc)
    main.ClipRequest(camera=10_000, start_time=now, end_time=now)
    assert _validate_camera_number(10_000) == 10_000
    assert _validate_camera_number(10_001) is None


def test_provisioning_assigns_last_valid_number_then_fails_closed_on_exhaustion(client, db_path):
    _seed_tenant(db_path)
    _grant_slots(db_path, "cust-1", 1)
    token = _seed_and_activate_appliance(db_path)
    activation = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    assert activation.status_code == 200, activation.text
    credential = activation.json()["credential"]

    # Fill the first 9,999 historical slots as removed cameras; numbering is
    # never reused because recordings remain keyed by their former number.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.executemany(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,former_camera_number,created_at) "
                "VALUES(?, 'cust-1','site-1','appl-1','Old camera','removed',?,'2026-01-01')",
                [(f"removed-{number}", number) for number in range(1, 10_000)],
            )
    queued = _provision_request(client, appliance_id="appl-1", device_key="device-last-slot")
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job_id"]
    confirmed = client.post(
        f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}", json={"success": True},
        headers=_appliance_headers("appl-1", credential),
    )
    assert confirmed.status_code == 200, confirmed.text
    with override_target(sqlite_path=str(db_path)):
        row = sqlite3.connect(db_path).execute(
            "SELECT camera_number,status FROM cameras WHERE device_key='device-last-slot'"
        ).fetchone()
    assert row == (10_000, "configured")

    # Deleting camera 10,000 frees a license slot, but its recording identity
    # remains reserved. The next add must fail before creating or marking a
    # camera provisioned without a number.
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    last_camera_id = sqlite3.connect(db_path).execute(
        "SELECT id FROM cameras WHERE device_key='device-last-slot'"
    ).fetchone()[0]
    removed = client.delete(f"/api/customer/cameras/{last_camera_id}")
    assert removed.status_code == 200, removed.text
    blocked = _provision_request(client, appliance_id="appl-1", device_key="device-after-exhaustion")
    assert blocked.status_code == 200, blocked.text
    blocked_job_id = blocked.json()["job_id"]
    final = client.post(
        f"/api/appliance/AIC-SLOT1/provisioning-jobs/{blocked_job_id}", json={"success": True},
        headers=_appliance_headers("appl-1", credential),
    )
    assert final.status_code == 200, final.text
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        job = conn.execute("SELECT status,failure_code FROM camera_provisioning_requests WHERE id=?", (blocked_job_id,)).fetchone()
        camera = conn.execute("SELECT id FROM cameras WHERE device_key='device-after-exhaustion'").fetchone()
    assert job == ("failed", "camera_number_exhausted")
    assert camera is None
