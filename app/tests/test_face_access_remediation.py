"""Focused fail-closed tests for the physical Face Access remediation.

Only a simulated adapter is used here; no physical relay or lock is
connected by these tests.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_face_access_remediation_import.db"):
    import access_control
    import door_access
    import face_access_safety
    import facial_embedding_sync
    import facial_events
    import facial_people
    import facial_recognition as fr
    from partner_db import connection, initialize_database
    from relay_control import MockRelayProvider

NOW = "2026-09-08T00:00:00"


class ApprovedEngine(fr.FaceEngine):
    name = face_access_safety.APPROVED_ACCESS_ENGINE
    version = face_access_safety.APPROVED_ACCESS_ENGINE_VERSION

    def __init__(self, *, available=True, version=None, face_size=10):
        self.available = available
        self.version = self.version if version is None else version
        self.face_size = face_size

    def detect_faces(self, image_bgr):
        return [fr.FaceDetection(0, 0, self.face_size, self.face_size)]

    def embed(self, face_crop_bgr):
        return (1.0, 0.0)

    def capability(self):
        return {"engine": self.name, "version": self.version, "available": self.available}


class HaarFallbackEngine(ApprovedEngine):
    name = "haar_intensity"
    version = "1"


class FakePhysicalAdapter:
    def __init__(self):
        self.unlock_calls = 0

    def health(self):
        return {"online": True, "controller_online": True}

    def get_state(self):
        return access_control.LOCKED

    def unlock(self, duration_seconds):
        self.unlock_calls += 1

    def lock(self):
        return None


class NoopScheduler:
    def schedule(self, *args, **kwargs):
        return None

    def cancel(self, *args, **kwargs):
        return None


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "face_access.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as conn:
            conn.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (NOW,))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S',?)", (NOW,))
            conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST0001',?)", (NOW,))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number,door_access_enabled,door_relay_channel) VALUES('cam-1','cust-1','site-1','appl-1','Door','active',?,1,1,1)", (NOW,))
            yield conn
    facial_events.reset_state()
    service = access_control.get_service()
    if service is not None:
        service._executor.shutdown(wait=True)
    access_control.set_service(None)


def _enable_production_face_access(monkeypatch, *, enabled=True, general=True):
    monkeypatch.setenv("ANYAICAM_ENV", "production")
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setenv("ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED", "true" if general else "false")
    monkeypatch.setenv("ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED", "true" if enabled else "false")


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"ANYAICAM_ENV": "development", "ANYAICAM_RUNTIME_ROLE": "edge", "ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED": "true", "ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED": "true"},
        {"ANYAICAM_ENV": "production", "ANYAICAM_RUNTIME_ROLE": "cloud", "ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED": "true", "ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED": "true"},
        {"ANYAICAM_ENV": "production", "ANYAICAM_RUNTIME_ROLE": "edge", "ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED": "true", "ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED": "false"},
        {"ANYAICAM_ENV": "production", "ANYAICAM_RUNTIME_ROLE": "edge", "ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED": "false", "ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED": "true"},
    ],
)
def test_physical_gate_fails_closed_unless_all_explicit_gates_are_open(monkeypatch, environment):
    for key in ("ANYAICAM_ENV", "ANYAICAM_RUNTIME_ROLE", "ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED", "ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert face_access_safety.physical_face_access_gate_open() is (environment == {
        "ANYAICAM_ENV": "production", "ANYAICAM_RUNTIME_ROLE": "edge",
        "ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED": "true", "ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED": "true",
    })


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {"engine": "haar_intensity", "engine_version": "1", "active_engine": "haar_intensity", "active_engine_version": "1", "engine_available": True, "quality": 0.8},
        {"engine": "onnx_yunet_arcface", "engine_version": "1", "active_engine": "haar_intensity", "active_engine_version": "1", "engine_available": False, "quality": 0.8},
        {"engine": "onnx_yunet_arcface", "engine_version": "wrong", "active_engine": "onnx_yunet_arcface", "active_engine_version": "1", "engine_available": True, "quality": 0.8},
        {"engine": "onnx_yunet_arcface", "engine_version": "1", "active_engine": "onnx_yunet_arcface", "active_engine_version": "1", "engine_available": True, "quality": 0},
        {"engine": "onnx_yunet_arcface", "engine_version": "1", "active_engine": "onnx_yunet_arcface", "active_engine_version": "1", "engine_available": True, "quality": float("nan")},
    ],
)
def test_unapproved_engine_provenance_and_quality_fail_closed(evidence):
    assert not face_access_safety.observation_is_access_approved(evidence)


def test_face_grant_snapshot_expires_at_fifteen_minutes(db):
    now = time.time()
    facial_embedding_sync._record_authoritative_sync(db, "cust-1", "v1", now - 900)
    assert facial_embedding_sync.cloud_access_grant_is_fresh(db, customer_id="cust-1", now_epoch=now)
    assert not facial_embedding_sync.cloud_access_grant_is_fresh(db, customer_id="cust-1", now_epoch=now + 0.01)
    assert not facial_embedding_sync.cloud_access_grant_is_fresh(db, customer_id="other-customer", now_epoch=now)


def test_persistent_dispatch_blocks_replay_cooldown_and_continuous_presence_and_allows_reentry(db):
    context = {"customer_id": "cust-1", "camera_id": "cam-1", "rule_id": "r1", "person_id": "p1", "facial_event_id": "e1"}
    assert face_access_safety.reserve_physical_dispatch(context, cooldown_seconds=10, rearm_seconds=30, now_epoch=100)[0]
    replay = {**context}
    ok, reason = face_access_safety.reserve_physical_dispatch(replay, cooldown_seconds=10, rearm_seconds=30, now_epoch=131)
    assert (ok, reason) == (False, "duplicate_facial_event")
    continuing = {**context, "facial_event_id": "e2", "presence_gap_seconds": 29.9}
    ok, reason = face_access_safety.reserve_physical_dispatch(continuing, cooldown_seconds=10, rearm_seconds=30, now_epoch=131)
    assert (ok, reason) == (False, "person_still_present")
    later = {**context, "facial_event_id": "e3", "presence_gap_seconds": 30}
    assert face_access_safety.reserve_physical_dispatch(later, cooldown_seconds=10, rearm_seconds=30, now_epoch=132)[0]


def _install_fake_physical_door(db, clock):
    adapter = FakePhysicalAdapter()
    service = access_control.AccessControlService(
        store=access_control.DoorStore(connection),
        adapters=access_control.AdapterFactory(mock_adapters={"door-1": adapter}),
        clock=lambda: clock[0], scheduler=NoopScheduler(), command_timeout=1,
    )
    service.configure_door(access_control.Door(
        id="door-1", customer_id="cust-1", camera_id="cam-1", name="Door",
        kind="mock", config={}, unlock_seconds=2, dry_run=False,
    ))
    access_control.set_service(service)
    camera = dict(db.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone())
    return adapter, camera


def _prepare_match(db, engine_name=ApprovedEngine.name, engine_version=ApprovedEngine.version):
    db.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1','facial_recognition','active',?,?)", (NOW, NOW))
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    db.execute("UPDATE facial_people SET access_enabled=1 WHERE id=?", (person_id,))
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine=engine_name, engine_version=engine_version, now=NOW)
    db.execute("INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,person_id,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,min_confidence,created_at,updated_at) VALUES('rule-1','cust-1','cam-1','Door','specific_person',?,1,2000,10,0,1,0.5,?,?)", (person_id, NOW, NOW))
    return person_id


@pytest.mark.parametrize("all_gates_open", [False, True])
def test_integrated_facial_event_reaches_fake_physical_adapter_only_with_explicit_gate(db, monkeypatch, all_gates_open):
    _enable_production_face_access(monkeypatch, enabled=all_gates_open)
    person_id = _prepare_match(db)
    now_epoch = time.time()
    clock = [now_epoch]
    adapter, camera = _install_fake_physical_door(db, clock)
    events = facial_events.record_facial_events(
        db, camera_number=1, appliance_id="appl-1", person_crop_bgr=np.zeros((50, 50, 3), dtype=np.uint8),
        now=NOW, engine=ApprovedEngine(), relay_provider=door_access.CameraDoorProvider(camera, MockRelayProvider(), person_id=person_id),
    )
    assert len(events) == 1
    assert bool(adapter.unlock_calls) is all_gates_open
    assert events[0]["relay_outcomes"][0]["activated"] is all_gates_open
    stored = db.execute("SELECT match_state FROM facial_events WHERE id=?", (events[0]["id"],)).fetchone()
    assert stored["match_state"] == "known"
    access_rows = db.execute("SELECT * FROM door_access_commands WHERE trigger_type='automatic'").fetchall()
    assert len(access_rows) == 1
    assert access_rows[0]["result"] == ("unlocked" if all_gates_open else "denied")


@pytest.mark.parametrize("grant_age,expected_unlock", [(10, True), (901, False)])
def test_cloud_grant_freshness_gates_only_physical_access_not_recognition_event(db, monkeypatch, grant_age, expected_unlock):
    _enable_production_face_access(monkeypatch)
    person_id = _prepare_match(db)
    db.execute("UPDATE facial_rules SET origin='cloud' WHERE id='rule-1'")
    now_epoch = time.time()
    facial_embedding_sync._record_authoritative_sync(db, "cust-1", "directory-v1", now_epoch - grant_age)
    clock = [now_epoch]
    adapter, camera = _install_fake_physical_door(db, clock)
    events = facial_events.record_facial_events(
        db, camera_number=1, appliance_id="appl-1", person_crop_bgr=np.zeros((50, 50, 3), dtype=np.uint8),
        now=NOW, engine=ApprovedEngine(), relay_provider=door_access.CameraDoorProvider(camera, MockRelayProvider(), person_id=person_id),
    )
    assert len(events) == 1 and events[0]["match_state"] == "known"
    assert bool(adapter.unlock_calls) is expected_unlock


@pytest.mark.parametrize("engine", [HaarFallbackEngine(), ApprovedEngine(available=False), ApprovedEngine(version="future"), ApprovedEngine(face_size=0)])
def test_unapproved_or_fallback_engine_never_unlocks_but_recognition_event_remains(db, monkeypatch, engine):
    _enable_production_face_access(monkeypatch)
    person_id = _prepare_match(db, engine.name, engine.version)
    clock = [time.time()]
    adapter, camera = _install_fake_physical_door(db, clock)
    events = facial_events.record_facial_events(
        db, camera_number=1, appliance_id="appl-1", person_crop_bgr=np.zeros((50, 50, 3), dtype=np.uint8),
        now=NOW, engine=engine, relay_provider=door_access.CameraDoorProvider(camera, MockRelayProvider(), person_id=person_id),
    )
    assert len(events) == 1
    assert adapter.unlock_calls == 0
    assert db.execute("SELECT COUNT(*) AS n FROM facial_events").fetchone()["n"] == 1


def test_integrated_door_replay_cooldown_and_reentry_survive_service_recreation(db, monkeypatch):
    _enable_production_face_access(monkeypatch)
    person_id = _prepare_match(db)
    initial = time.time()
    clock = [initial]
    adapter, camera = _install_fake_physical_door(db, clock)

    def evaluate(at):
        clock[0] = at
        facial_events.reset_state()
        result = facial_events.record_facial_events(
            db, camera_number=1, appliance_id="appl-1", person_crop_bgr=np.zeros((50, 50, 3), dtype=np.uint8),
            now=at, engine=ApprovedEngine(), relay_provider=door_access.CameraDoorProvider(camera, MockRelayProvider(), person_id=person_id),
        )
        return result

    first = evaluate(initial)
    assert first and adapter.unlock_calls == 1
    # A new service instance models process restart; the durable dispatch
    # and last-seen rows are intentionally retained in SQLite.
    access_control.get_service()._executor.shutdown(wait=True)
    access_control.set_service(None)
    adapter2, camera2 = _install_fake_physical_door(db, clock)
    db.execute("UPDATE access_door_state SET state='locked',relock_due_at=NULL WHERE door_id='door-1'")
    # The detector keeps seeing this person after relock and after the
    # service object has been recreated; those observations refresh the
    # durable presence clock, so elapsed time alone cannot re-arm.
    face_access_safety.note_person_presence(customer_id="cust-1", camera_id="cam-1", person_id=person_id, now_epoch=initial + 10)
    face_access_safety.note_person_presence(customer_id="cust-1", camera_id="cam-1", person_id=person_id, now_epoch=initial + 20)
    face_access_safety.note_person_presence(customer_id="cust-1", camera_id="cam-1", person_id=person_id, now_epoch=initial + 30)
    continued = evaluate(initial + 31)
    assert continued and adapter2.unlock_calls == 0
    assert db.execute("SELECT COUNT(*) AS n FROM facial_access_dispatches").fetchone()["n"] == 1
    # Once a genuine observation gap satisfies the configured re-arm
    # interval, a later distinct event can be dispatched.
    reentry = evaluate(initial + 62)
    assert reentry and adapter2.unlock_calls == 1
