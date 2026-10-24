"""R-REC-1 (Linux 1.2.7.1): event-only recording is an invariant -- cloud,
edge-sync, migration and upload half. Green U-R2 / U-R3 / U-R4 / U-R6.

The approved product records motion/event clips only. Continuous recording
(local 24/7 segments or continuous cloud upload) must never be selected by
a missing, null, legacy, invalid or explicit value on any path:

- U-R2: camera creation through cloud provisioning (new camera, rediscovered
  camera, placeholder consumed) and through the edge sync insert persists
  local_recording_mode='event'; the configuration route transmits 'event'.
- U-R3: the admin routes reject 'continuous' (400); null local mode is stored
  as 'event'; a customer can set no mode; pricing offers no continuous plan.
- U-R4: the startup migration converts NULL/invalid/continuous rows to
  'event' (cloud mode to NULL, provisioning requests to 'motion'), audits the
  counts and is idempotent.
- U-R6: the uploader never relays a recording segment for any mode; the
  server issues only event-media credentials and refuses the continuous
  catalog route; the effective cloud mode is only 'motion' or 'disabled'.

Bare FastAPI app + appliance_cloud routes + a throwaway sqlite per test,
the same pattern as test_camera_local_recording_mode.py (no main import).
"""

import asyncio
import inspect
import json
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_r_rec_1_event_only_cloud.db"):
    import appliance_cloud
    import db_migrations
    import edge_camera_sync
    import event_media_policy
    import partner_portal
    import recording_uploader
    from partner_db import connection, initialize_database, password_hash


NOW = "2026-10-24T00:00:00"


def _seed(db, camera_rows=(("cam-1", 1),), *, local_mode=None, cloud_mode=None):
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (NOW,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','partner-1','C','c@example.test','active','real',?)", (NOW,))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S',?)", (NOW,))
    db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-RREC0001',?)", (NOW,))
    db.execute("INSERT OR IGNORE INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-1','appl-1',?,?)", (password_hash("cred"), NOW))
    for camera_id, number in camera_rows:
        db.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,local_recording_mode,cloud_recording_mode,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (camera_id, "cust-1", "site-1", "appl-1", f"Camera {number}", number, "configured", local_mode, cloud_mode, NOW),
        )


def _headers():
    return {
        "X-Appliance-Id": "appl-1", "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16), "Authorization": "Bearer cred",
    }


def _admin_cookie():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("admin@example.test", "administrator")}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "r_rec_1.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _camera(db_path, camera_id="cam-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT local_recording_mode,cloud_recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
            return dict(row) if row else None


# ============================================================ U-R3: admin / API


@pytest.mark.parametrize("value", ["continuous", "CONTINUOUS", "sometimes", "", 1])
def test_local_mode_route_rejects_everything_but_event(client, db_path, value):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, local_mode="event")
    response = client.post("/api/admin/cameras/cam-1/local-recording-mode", cookies=_admin_cookie(),
                           json={"local_recording_mode": value})
    assert response.status_code == 400
    assert _camera(db_path)["local_recording_mode"] == "event"


@pytest.mark.parametrize("payload", [{"local_recording_mode": None}, {}, {"local_recording_mode": "event"}])
def test_local_mode_route_stores_event_for_null_missing_or_event(client, db_path, payload):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db)
    response = client.post("/api/admin/cameras/cam-1/local-recording-mode", cookies=_admin_cookie(), json=payload)
    assert response.status_code == 200
    assert response.json()["local_recording_mode"] == "event"
    assert _camera(db_path)["local_recording_mode"] == "event"


def test_a_customer_cannot_set_any_recording_mode(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db)
    customer = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1")}
    for path, body in (("local-recording-mode", {"local_recording_mode": "event"}),
                       ("cloud-recording-mode", {"cloud_recording_mode": "motion"})):
        assert client.post(f"/api/admin/cameras/cam-1/{path}", cookies=customer, json=body).status_code == 403
    assert _camera(db_path) == {"local_recording_mode": None, "cloud_recording_mode": None}


def test_cloud_mode_route_rejects_continuous(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, cloud_mode="motion")
    response = client.post("/api/admin/cameras/cam-1/cloud-recording-mode", cookies=_admin_cookie(),
                           json={"cloud_recording_mode": "continuous"})
    assert response.status_code == 400
    assert _camera(db_path)["cloud_recording_mode"] == "motion"


@pytest.mark.parametrize("value", ["motion", "disabled", None])
def test_cloud_mode_route_still_accepts_motion_disabled_and_null(client, db_path, value):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db)
    response = client.post("/api/admin/cameras/cam-1/cloud-recording-mode", cookies=_admin_cookie(),
                           json={"cloud_recording_mode": value})
    assert response.status_code == 200
    assert _camera(db_path)["cloud_recording_mode"] == value


def test_pricing_offers_no_continuous_plan_and_refuses_a_continuous_quote(tmp_path, monkeypatch):
    import pricing_config
    for plan in pricing_config.DEFAULT_PRICING["plans"].values():
        assert "continuous" not in plan
    assert not [key for key in pricing_config.DEFAULT_PRICING["conflicts"] if ".continuous." in key]
    # A pricing file saved before 1.2.7.1 may still hold continuous rows: never offered again.
    saved = tmp_path / "pricing_config.json"
    saved.write_text(json.dumps({"plans": {"2mp": {"continuous": {"7": 9.79}}}}), encoding="utf-8")
    monkeypatch.setattr(pricing_config, "CONFIG_FILE", saved)
    config = pricing_config.load_pricing()
    assert all("continuous" not in plan for plan in config["plans"].values())
    assert not [key for key in config["partner"]["plan_terms"] if ".continuous." in key]
    with pytest.raises(ValueError):
        pricing_config.calculate_quote({"resolution": "4mp", "recording": "continuous", "retention": "7", "quantity": 1}, config)


# ============================================================ U-R2: transmit 'event'


@pytest.mark.parametrize("stored", [None, "continuous", "garbage", "event"])
def test_configuration_route_transmits_event_whatever_is_stored(client, db_path, stored):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, local_mode=stored, cloud_mode="continuous")
    response = client.get("/api/appliance/configuration", headers=_headers())
    assert response.status_code == 200
    camera = response.json()["cameras"][0]
    assert camera["local_recording_mode"] == "event"
    assert camera["recording_mode"] in ("motion", "disabled")


# ============================================================ U-R6: cloud effective mode


def _grant_legacy_plan(db, recording_mode, retention_days=30):
    db.execute(
        "INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,status,created_at) "
        "VALUES('plan-1','cust-1','2mp',?,?,2,50.0,25.0,'active',?)", (recording_mode, retention_days, NOW))


def _grant_v2_plan(db, plan_key):
    cloud = 1 if plan_key == "hybrid" else 0
    db.execute(
        "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
        "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,cloud_event_storage,"
        "cloud_event_retention_days,created_at,updated_at) VALUES('v2-1','cust-1',?,2,'cus_t','sub_t','si_t','price_t','active','active',7,?,?,"
        "'2026-10-10','2026-10-10')", (plan_key, cloud, 14 if cloud else None))


@pytest.mark.parametrize("explicit", [None, "continuous", "motion", "disabled", "garbage"])
@pytest.mark.parametrize("plan", ["none", "legacy-continuous-30", "legacy-continuous-2", "legacy-motion", "hybrid", "basic_local"])
def test_effective_cloud_mode_is_only_motion_or_disabled(db_path, explicit, plan):
    assert event_media_policy.CLOUD_RECORDING_MODES == ("motion", "disabled")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            _seed(db)
            if plan == "legacy-continuous-30":
                _grant_legacy_plan(db, "continuous", 30)
            elif plan == "legacy-continuous-2":
                _grant_legacy_plan(db, "continuous", 2)
            elif plan == "legacy-motion":
                _grant_legacy_plan(db, "motion", 30)
            elif plan in ("hybrid", "basic_local"):
                _grant_v2_plan(db, plan)
        with connection() as db:
            mode = event_media_policy.effective_cloud_recording_mode(db, "cust-1", explicit)
    assert mode in ("motion", "disabled")
    if explicit == "disabled":
        assert mode == "disabled"


def test_a_qualifying_legacy_continuous_plan_maps_to_motion_and_a_non_qualifying_one_to_disabled(db_path):
    """Red decision 1: a legacy 'continuous' plan keeps cloud event protection when
    its retention qualifies (7/14/30 days), otherwise its cameras are 'disabled'."""
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            _seed(db)
            _grant_legacy_plan(db, "continuous", 30)
        with connection() as db:
            assert event_media_policy.effective_cloud_recording_mode(db, "cust-1", None) == "motion"
            assert event_media_policy.effective_cloud_recording_mode(db, "cust-1", "continuous") == "motion"
            db.execute("UPDATE plans SET retention_days=2")
        with connection() as db:
            assert event_media_policy.effective_cloud_recording_mode(db, "cust-1", None) == "disabled"


# ============================================================ U-R6: server refuses the continuous tier


class _FakeSTS:
    last_policy = None
    last_tags = None

    def __init__(self, *a, **k):
        pass

    def assume_role(self, RoleArn, RoleSessionName, Policy, DurationSeconds, Tags=None):
        from datetime import datetime, timezone
        _FakeSTS.last_policy = json.loads(Policy)
        _FakeSTS.last_tags = Tags
        return {"Credentials": {"AccessKeyId": "FAKE", "SecretAccessKey": "fake", "SessionToken": "fake",
                                "Expiration": datetime.now(timezone.utc)}}


def _configure_upload(monkeypatch, *, recording_enabled, event_media_enabled, pilot):
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ENABLED", recording_enabled)
    monkeypatch.setattr(appliance_cloud, "EVENT_MEDIA_UPLOAD_ENABLED", event_media_enabled)
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_PILOT_CAMERAS", frozenset({"cam-1"}) if pilot else frozenset())
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ROLE_ARN", "arn:aws:iam::000000000000:role/test")
    monkeypatch.setattr(appliance_cloud, "RECORDING_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(appliance_cloud, "RECORDING_AWS_REGION", "us-east-1")
    monkeypatch.setattr(appliance_cloud, "RECORDING_SESSION_TAGS_ENABLED", True)
    monkeypatch.setattr(appliance_cloud, "boto3", type("_B", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())}))


@pytest.mark.parametrize("recording_enabled,event_media_enabled,pilot", [
    (True, False, False), (True, True, False), (False, False, True), (False, True, True)])
def test_credentials_are_never_the_continuous_tier(client, db_path, monkeypatch, recording_enabled, event_media_enabled, pilot):
    _configure_upload(monkeypatch, recording_enabled=recording_enabled, event_media_enabled=event_media_enabled, pilot=pilot)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, cloud_mode="motion")
            _grant_legacy_plan(db, "motion", 30)
    _FakeSTS.last_policy = None
    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_headers())
    assert response.status_code == 200
    resources = json.dumps(_FakeSTS.last_policy)
    assert "/events/" in resources, "only the narrow event-media prefix may be granted"
    assert {t["Key"]: t["Value"] for t in _FakeSTS.last_tags}["tier"] == "events"


def test_credentials_are_refused_without_cloud_event_protection_even_for_bulk_or_pilot(client, db_path, monkeypatch):
    _configure_upload(monkeypatch, recording_enabled=True, event_media_enabled=False, pilot=True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db)  # no plan: no cloud event protection
    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_headers())
    assert response.status_code == 403


@pytest.mark.parametrize("recording_enabled,pilot", [(True, False), (False, True)])
def test_continuous_recording_catalog_route_is_refused(client, db_path, monkeypatch, recording_enabled, pilot):
    _configure_upload(monkeypatch, recording_enabled=recording_enabled, event_media_enabled=True, pilot=pilot)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, cloud_mode="motion")
    key = "recordings/cust-1/site-1/appl-1/cam-1/camera1_2026-10-24_00-00-00.mkv"
    response = client.post("/api/appliance/recordings/cam-1/available", headers=_headers(),
                           json={"s3_key": key, "started_at": NOW, "ended_at": NOW})
    assert response.status_code == 403
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            assert db.execute("SELECT COUNT(*) AS n FROM recordings").fetchone()["n"] == 0


# ============================================================ U-R6: the edge uploader


@pytest.mark.parametrize("mode", [None, "continuous", "motion", "disabled", "garbage"])
def test_upload_worker_never_relays_a_recording_segment(monkeypatch, mode):
    ru = recording_uploader
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", None)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.01)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2])
    monkeypatch.setattr(ru, "_camera_at_or_over_total_cap", lambda n: False)
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": f"cam-{n}", "site_id": "site-1", "cloud_recording_mode": mode})
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    async def scenario():
        task = asyncio.ensure_future(ru.recording_upload_worker())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert calls == []


def test_upload_worker_source_cannot_reach_the_segment_relay():
    """Red decision 2: the continuous relay is kept in the code but unreachable."""
    assert "_relay_camera_once(" not in inspect.getsource(recording_uploader.recording_upload_worker)
    assert "_relay_camera_once," not in inspect.getsource(recording_uploader.recording_upload_worker)


@pytest.mark.parametrize("received,expected", [("continuous", None), (None, None), ("garbage", None),
                                               ("motion", "motion"), ("disabled", "disabled")])
def test_edge_camera_map_never_holds_continuous(received, expected):
    assert recording_uploader._edge_cloud_recording_mode(received) == expected


# ============================================================ U-R2: edge sync insert


IDENTITY = {"appliance_id": "appl-1", "cloud_id": "AIC-RREC0001", "credential": "cred", "customer_id": "cust-1", "site_id": "site-1"}


@pytest.fixture()
def edge(db_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: IDENTITY)
    with override_target(sqlite_path=str(db_path)):
        yield


def _cloud_cameras(monkeypatch, cameras):
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get",
                        lambda path, appliance_id, credential: {"cameras": cameras, "configuration_version": "x"})


@pytest.mark.parametrize("sent", [None, "continuous", "garbage", "", "event"])
def test_edge_sync_insert_persists_event_whatever_the_cloud_sends(edge, db_path, monkeypatch, sent):
    item = {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
            "device_key": "urn:uuid:a", "recording_mode": "continuous"}
    if sent is not None:
        item["local_recording_mode"] = sent
    _cloud_cameras(monkeypatch, [item])
    assert edge_camera_sync.sync_provisioned_cameras()["status"] == "ok"
    assert _camera(db_path) == {"local_recording_mode": "event", "cloud_recording_mode": None}


def test_edge_sync_reports_the_cameras_whose_recording_mode_changed(edge, db_path, monkeypatch):
    with connection() as db:
        _seed(db, (("cam-1", 1), ("cam-2", 2)), local_mode=None)
        db.execute("UPDATE cameras SET local_recording_mode='event' WHERE id='cam-2'")
    _cloud_cameras(monkeypatch, [
        {"id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured", "local_recording_mode": None},
        {"id": "cam-2", "name": "Camera 2", "camera_number": 2, "status": "configured", "local_recording_mode": "event"},
        {"id": "cam-3", "name": "Camera 3", "camera_number": 3, "status": "configured"},
    ])
    edge_camera_sync.sync_provisioned_cameras()
    assert sorted(edge_camera_sync.sync_state["recording_mode_changed"]) == [1, 3]
    # The second cycle changes nothing.
    edge_camera_sync.sync_provisioned_cameras()
    assert edge_camera_sync.sync_state["recording_mode_changed"] == []


def test_edge_sync_worker_notifies_the_recorder_of_changed_cameras(monkeypatch):
    notified = []
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "SYNC_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(edge_camera_sync, "refresh_analytics_camera_map", lambda: None)
    monkeypatch.setattr(edge_camera_sync.product_mode, "restart_if_running_flags_stale", lambda: None)

    def fake_sync():
        edge_camera_sync.sync_state["recording_mode_changed"] = [4]
        return {"status": "ok"}

    monkeypatch.setattr(edge_camera_sync, "sync_provisioned_cameras", fake_sync)
    monkeypatch.setattr(edge_camera_sync, "_recording_mode_listeners", [notified.extend])

    async def scenario():
        task = asyncio.ensure_future(edge_camera_sync.camera_configuration_sync_worker())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if notified:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert notified == [4]


# ============================================================ U-R2: cloud provisioning branches


def _provision(db_path, *, device_key="urn:uuid:new"):
    """Runs appliance_submit_provisioning() for one queued job of this appliance."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            job_id = secrets.token_hex(6)
            db.execute(
                "INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,recording_mode,"
                "analytics_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, "cust-1", "appl-1", "site-1", device_key, "Front", "motion", "[]", "verifying", NOW, NOW))
    return job_id


@pytest.fixture()
def licensed(monkeypatch):
    import customer_entitlements
    monkeypatch.setattr(customer_entitlements, "total_camera_slots", lambda customer_id: 64)
    monkeypatch.setattr(customer_entitlements, "usable_camera_capacity", lambda customer_id: 64)


@pytest.mark.parametrize("branch", ["new", "rediscovered", "placeholder"])
def test_every_cloud_provisioning_branch_persists_event(client, db_path, licensed, branch):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, ())
            if branch == "rediscovered":
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,device_key,local_recording_mode,created_at) "
                           "VALUES('cam-old','cust-1','site-1','appl-1','Old','configured','urn:uuid:new','continuous',?)", (NOW,))
            elif branch == "placeholder":
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at) "
                           "VALUES('cam-slot','cust-1','site-1','appl-1','Camera 1','pending_installation',?)", (NOW,))
    job_id = _provision(db_path)
    response = client.post(f"/api/appliance/AIC-RREC0001/provisioning-jobs/{job_id}", headers=_headers(), json={"success": True})
    assert response.status_code == 200, response.text
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            rows = db.execute("SELECT id,local_recording_mode FROM cameras WHERE device_key='urn:uuid:new'").fetchall()
    assert len(rows) == 1
    if branch == "rediscovered":
        assert rows[0]["id"] == "cam-old"
    if branch == "placeholder":
        assert rows[0]["id"] == "cam-slot"
    assert rows[0]["local_recording_mode"] == "event"


# ============================================================ U-R4: startup migration


def _migration_audits(db):
    return [json.loads(r["details_json"]) for r in db.execute(
        "SELECT details_json FROM audit_logs WHERE action='recording.event_only_migration' ORDER BY id").fetchall()]


def test_startup_migration_enforces_event_only_and_is_idempotent(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            _seed(db, ())
            for camera_id, local, cloud in (("c-null", None, None), ("c-cont", "continuous", "continuous"),
                                            ("c-junk", "{\"mode\": \"contin", "junk"), ("c-event", "event", "motion"),
                                            ("c-off", "", "disabled")):
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,local_recording_mode,cloud_recording_mode,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (camera_id, "cust-1", "site-1", "appl-1", camera_id, "configured", local, cloud, NOW))
            for job_id, mode in (("j-cont", "continuous"), ("j-null", None), ("j-motion", "motion")):
                db.execute("INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,recording_mode,"
                           "analytics_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, "cust-1", "appl-1", "site-1", f"dk-{job_id}", "Cam", mode, "[]", "queued", NOW, NOW))

        db_migrations.apply_migrations()  # what every startup runs

        with connection() as db:
            cameras = {r["id"]: dict(r) for r in db.execute("SELECT id,local_recording_mode,cloud_recording_mode FROM cameras").fetchall()}
            jobs = {r["id"]: r["recording_mode"] for r in db.execute("SELECT id,recording_mode FROM camera_provisioning_requests").fetchall()}
            audits = _migration_audits(db)
        assert {c["local_recording_mode"] for c in cameras.values()} == {"event"}
        assert cameras["c-cont"]["cloud_recording_mode"] is None
        assert cameras["c-junk"]["cloud_recording_mode"] is None
        assert cameras["c-event"]["cloud_recording_mode"] == "motion"
        assert cameras["c-off"]["cloud_recording_mode"] == "disabled"
        assert jobs == {"j-cont": "motion", "j-null": "motion", "j-motion": "motion"}
        assert audits == [{"local_recording_mode_to_event": 4, "cloud_recording_mode_cleared": 2, "provisioning_requests_to_motion": 2}]

        db_migrations.apply_migrations()  # the next restart: nothing left to change, nothing new audited
        with connection() as db:
            assert len(_migration_audits(db)) == 1
            assert {r["local_recording_mode"] for r in db.execute("SELECT local_recording_mode FROM cameras").fetchall()} == {"event"}
