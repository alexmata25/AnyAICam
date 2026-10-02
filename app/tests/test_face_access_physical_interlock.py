"""Automatic physical Face Access, end to end (2026-10-02, Codex Face
Recognition / Face Access audit; owner policies approved 2026-10-02).

The real chain -- facial_events.record_facial_events() ->
door_access.CameraDoorProvider -> access_control.AccessControlService ->
door adapter -- with a fake physical adapter that records every hardware
call. Covers launch blockers 1-4:

1. mock/development FR never reaches a physical adapter; the explicit
   production gate is required on top of the general FR flags;
2. only the access-approved engine (ArcFace/ONNX) can open a door -- Haar,
   a failed/fallen-back engine, missing/mismatched provenance or an invalid
   observation never do, while FR events are still recorded;
3. replay and re-arm protection is durable: a person still at the door after
   relock, a replayed facial event, and a process restart never pulse again;
   a genuinely new arrival after the cooldown does;
4. cloud-origin grants authorize only within 15 minutes of the last
   successful authoritative sync, durably across restarts.
"""
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

import access_adapters as aa
import access_control as ac
import door_access
import face_access_guard as guard
import facial_events
import facial_people
import facial_recognition as fr
import relay_control
from database_backend import override_target
from face_access_helpers import ApprovedEngine, HaarNamedEngine, enable_physical_face_access, observation
from partner_db import connection, initialize_database
from test_access_control import FakeScheduler, make_service, mock_door

T0 = datetime(2026, 10, 2, 12, 0, 0)
ARCFACE = ("onnx_yunet_arcface", "1")


@pytest.fixture()
def site(tmp_path, monkeypatch):
    """cust-1 with door camera cam-1 (camera number 1) behind an
    access-control door (door-1, fake adapter), Alice enrolled with the
    approved engine, and a local Face Access rule for her (cooldown 10 s)."""
    path = tmp_path / "face_access.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real','2026-01-01')")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real','2026-01-01')")
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home','2026-01-01')")
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1','2026-01-01')")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number,door_access_enabled,door_relay_channel) "
                       "VALUES('cam-1','cust-1','site-1','appl-1','Front Door','active','2026-01-01',1,1,1)")
            db.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
                       "VALUES('cam-1','facial_recognition','active','2026-01-01','2026-01-01')")
            person = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now="2026-01-01")
            facial_people.add_reference_image(db, customer_id="cust-1", person_id=person, embedding=(1.0, 0.0),
                                              engine=ARCFACE[0], engine_version=ARCFACE[1], now="2026-01-01")
            facial_people.add_reference_image(db, customer_id="cust-1", person_id=person, embedding=(1.0, 0.0),
                                              engine="haar_intensity", engine_version="1", now="2026-01-01")
            db.execute("INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,person_id,relay_channel,pulse_ms,cooldown_seconds,"
                       "dry_run,enabled,min_confidence,created_at,updated_at) VALUES('rule-1','cust-1','cam-1','Alice','specific_person',?,1,3000,10,0,1,0.5,"
                       "'2026-01-01','2026-01-01')", (person,))
        monkeypatch.setattr(fr, "FACIAL_RECOGNITION_ENABLED", True)
        adapter = aa.MockDoorAdapter()
        scheduler = FakeScheduler()
        service = make_service(mock_adapters={"door-1": adapter}, scheduler=scheduler)
        mock_door(service, door_id="door-1", camera_id="cam-1")
        ac.set_service(service)
        facial_events.reset_state()
        yield {"person": person, "adapter": adapter, "service": service, "scheduler": scheduler, "path": path}
        ac.set_service(None)
        facial_events.reset_state()


def _see(at, engine=None, *, fresh_debounce=True):
    """One camera sighting at time `at`; returns the FR events it created."""
    if fresh_debounce:
        facial_events.reset_state()  # FR's in-memory re-report window, as after it elapses
    with connection() as db:
        return facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1",
                                                  person_crop_bgr=np.zeros((50, 50, 3), dtype=np.uint8), now=at,
                                                  relay_provider=relay_control.MockRelayProvider(simulated=True),
                                                  engine=engine or ApprovedEngine())


def _unlocks(site):
    return [call for call in site["adapter"].calls if call[0] == "unlock"]


def _attempts():
    with connection() as db:
        return [(r["result"], r["reason"]) for r in db.execute("SELECT result,reason FROM face_access_attempts ORDER BY id")]


def _relock(site):
    site["scheduler"].fire("relock:door-1")


# ================================================================ blocker 1: production gate

def test_without_the_production_gate_nothing_physical_happens_but_fr_events_do(site):
    events = _see(T0)
    assert len(events) == 1 and events[0]["match_state"] == "known"  # recognition unaffected
    assert _unlocks(site) == []
    assert _attempts() == [("refused", "face_access_physical_disabled")]
    with connection() as db:
        row = db.execute("SELECT authorization_result,relay_result,success,error FROM door_access_events WHERE facial_event_id=?",
                         (events[0]["id"],)).fetchone()
    assert (row["success"], row["error"]) == (0, "face_access_physical_disabled")


def test_the_general_fr_flags_alone_are_not_enough(site, monkeypatch):
    monkeypatch.setattr(relay_control, "FACIAL_ACCESS_CONTROL_ENABLED", True)
    monkeypatch.setenv("ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED", "true")
    monkeypatch.setenv("ANYAICAM_FACIAL_RECOGNITION_ENABLED", "true")
    _see(T0)
    assert _unlocks(site) == []


def test_with_the_gate_and_every_authorization_the_door_unlocks_once(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _see(T0)
    assert len(_unlocks(site)) == 1
    assert _attempts() == [("accepted", None)]


def test_the_gate_never_bypasses_the_rules(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    with connection() as db:
        db.execute("UPDATE facial_rules SET enabled=0")
    _see(T0)
    assert _unlocks(site) == []


def test_manual_and_other_door_paths_are_not_affected_by_the_face_gate(site):
    with connection() as db:
        camera = door_access.door_camera(db, customer_id="cust-1", camera_id="cam-1")
    result = door_access.trigger_door(dict(camera), reason="manual", actor="owner@example.test", trigger_type="manual")
    assert result.activated and len(_unlocks(site)) == 1  # gate is off; manual unlock still works


# ================================================================ blocker 2: approved engine only

@pytest.mark.parametrize("engine,why", [
    (HaarNamedEngine(), "engine_not_access_approved"),
    (ApprovedEngine(available=False), "engine_unavailable"),
])
def test_haar_or_an_unavailable_engine_never_opens_the_door(site, monkeypatch, engine, why):
    enable_physical_face_access(monkeypatch)
    events = _see(T0, engine)
    assert events and events[0]["match_state"] == "known"
    assert _unlocks(site) == [] and _attempts() == [("refused", why)]


def test_a_requested_arcface_that_falls_back_to_haar_never_opens_the_door(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "arcface")
    monkeypatch.setattr(fr, "_build_named_onnx_engine", lambda name: None)  # model missing / init failed
    fr.reset_engine()
    try:
        assert isinstance(fr.get_engine(), fr.HaarEmbeddingFaceEngine)  # the silent fallback
        provider = door_access.CameraDoorProvider(
            {"id": "cam-1", "customer_id": "cust-1", "name": "Front Door"}, relay_control.MockRelayProvider(),
            person_id=site["person"], db=None, observation=None, engine=fr.get_engine(), now=T0)
        with connection() as db:
            provider.db = db
            provider.observation = fr.FaceObservation(bbox=fr.FaceDetection(0, 0, 10, 10), embedding=(1.0, 0.0),
                                                      engine="haar_intensity", engine_version=fr.HaarEmbeddingFaceEngine.version, quality=0.5)
            result = provider.trigger(relay_control.RelayRequest(channel=1, reason="facial_event:fallback-1", dry_run=False))
    finally:
        fr.reset_engine()
    assert not result.activated and result.suppressed_reason == "engine_not_access_approved"
    assert _unlocks(site) == []


@pytest.mark.parametrize("claimed,produced_by,why", [
    (("onnx_yunet_arcface", "1"), HaarNamedEngine(), "engine_provenance_mismatch"),  # a Haar match labelled ArcFace
    (("onnx_yunet_arcface", "2"), ApprovedEngine(), "engine_not_access_approved"),   # unknown version
    (("", ""), ApprovedEngine(), "engine_not_access_approved"),                       # no provenance
    (("onnx_yunet_arcface", "1"), None, "engine_provenance_missing"),                 # no engine instance
])
def test_missing_or_incorrect_provenance_never_opens_the_door(site, monkeypatch, claimed, produced_by, why):
    enable_physical_face_access(monkeypatch)
    with connection() as db:
        provider = door_access.CameraDoorProvider(
            {"id": "cam-1", "customer_id": "cust-1", "name": "Front Door"}, relay_control.MockRelayProvider(),
            person_id=site["person"], db=db, engine=produced_by, now=T0,
            observation=fr.FaceObservation(bbox=fr.FaceDetection(0, 0, 10, 10), embedding=(1.0, 0.0),
                                           engine=claimed[0], engine_version=claimed[1], quality=0.5))
        result = provider.trigger(relay_control.RelayRequest(channel=1, reason="facial_event:prov-1", dry_run=False))
    assert not result.activated and result.suppressed_reason == why
    assert _unlocks(site) == []


@pytest.mark.parametrize("obs", [
    observation(quality=0.0), observation(quality=float("nan")), observation(quality=1.5),
    observation(width=0), observation(vector=(float("nan"), 0.0)),
])
def test_an_invalid_observation_never_opens_the_door(site, monkeypatch, obs):
    enable_physical_face_access(monkeypatch)
    with connection() as db:
        provider = door_access.CameraDoorProvider(
            {"id": "cam-1", "customer_id": "cust-1", "name": "Front Door"}, relay_control.MockRelayProvider(),
            person_id=site["person"], db=db, engine=ApprovedEngine(), observation=obs, now=T0)
        result = provider.trigger(relay_control.RelayRequest(channel=1, reason="facial_event:q-1", dry_run=False))
    assert not result.activated and result.suppressed_reason == "observation_quality_invalid"
    assert _unlocks(site) == []


# ================================================================ blocker 3: replay and re-arm

def test_a_person_still_at_the_door_after_relock_does_not_unlock_it_again(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _see(T0)
    assert len(_unlocks(site)) == 1
    _relock(site)
    for second in range(1, 90):  # standing there for a minute and a half, seen every second
        _see(T0 + timedelta(seconds=second), fresh_debounce=(second % 30 == 0))
    assert len(_unlocks(site)) == 1
    assert ("refused", "not_rearmed_still_present") in _attempts()


def test_within_the_cooldown_nothing_unlocks_even_after_relock(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _see(T0)
    _relock(site)
    _see(T0 + timedelta(seconds=5))
    assert len(_unlocks(site)) == 1 and ("refused", "cooldown") in _attempts()


def test_a_replayed_facial_event_never_pulses_again(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    events = _see(T0)
    detection_event_id = events[0]["detection_event_id"]
    _relock(site)
    engine = ApprovedEngine()
    for _ in range(3):
        with connection() as db:
            provider = door_access.CameraDoorProvider(
                {"id": "cam-1", "customer_id": "cust-1", "name": "Front Door"}, relay_control.MockRelayProvider(),
                person_id=site["person"], db=db, engine=engine, observation=observation(engine),
                now=T0 + timedelta(minutes=10))  # long after the cooldown: only the replay rule stops it
            facial_events.evaluate_access_rules(db, customer_id="cust-1", camera_id="cam-1", match_state="known", confidence=0.99,
                                                matched_person_id=site["person"], matched_watchlist_id=None, relay_provider=provider,
                                                detection_event_id=detection_event_id)
    assert len(_unlocks(site)) == 1
    assert _attempts().count(("refused", "duplicate_facial_event")) == 3


def test_protection_survives_a_restart(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    events = _see(T0)
    _relock(site)
    # Restart-equivalent: a new service on the same database, FR's memory gone.
    restarted = make_service(mock_adapters={"door-1": site["adapter"]}, scheduler=FakeScheduler())
    ac.set_service(restarted)
    facial_events.reset_state()
    for second in range(1, 40):
        _see(T0 + timedelta(seconds=second), fresh_debounce=(second == 39))
    assert len(_unlocks(site)) == 1
    with connection() as db:  # and the original event can never be replayed after the restart either
        engine = ApprovedEngine()
        provider = door_access.CameraDoorProvider(
            {"id": "cam-1", "customer_id": "cust-1", "name": "Front Door"}, relay_control.MockRelayProvider(),
            person_id=site["person"], db=db, engine=engine, observation=observation(engine), now=T0 + timedelta(hours=2))
        result = provider.trigger(relay_control.RelayRequest(channel=1, reason=f"facial_event:{events[0]['detection_event_id']}", dry_run=False))
    assert result.suppressed_reason == "duplicate_facial_event" and len(_unlocks(site)) == 1


def test_a_genuinely_new_arrival_after_the_cooldown_unlocks(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _see(T0)
    _relock(site)
    _see(T0 + timedelta(seconds=3), fresh_debounce=False)  # still there a moment
    # Gone for two minutes (not seen), then back.
    _see(T0 + timedelta(seconds=123))
    assert len(_unlocks(site)) == 2
    assert [a for a in _attempts() if a[0] == "accepted"] == [("accepted", None), ("accepted", None)]


# ================================================================ blocker 4: 15-minute cloud grants

def _cloud_grant(site):
    with connection() as db:
        db.execute("UPDATE facial_rules SET origin='cloud'")


def _stamp(at):
    with connection() as db:
        guard.mark_grants_synced(db, customer_id="cust-1", now=at)


@pytest.mark.parametrize("age_minutes,unlocks", [(1, 1), (14, 1), (15, 1), (16, 0), (240, 0)])
def test_a_cloud_grant_authorizes_only_within_15_minutes_of_the_last_sync(site, monkeypatch, age_minutes, unlocks):
    enable_physical_face_access(monkeypatch)
    _cloud_grant(site)
    _stamp(T0 - timedelta(minutes=age_minutes))
    events = _see(T0)
    assert events and events[0]["match_state"] == "known"  # recognition and events continue regardless
    assert len(_unlocks(site)) == unlocks
    if not unlocks:
        assert _attempts()[-1] == ("refused", "cloud_grant_stale")


def test_a_never_synced_cloud_grant_does_not_authorize(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _cloud_grant(site)
    _see(T0)
    assert _unlocks(site) == [] and _attempts()[-1] == ("refused", "cloud_grant_never_synced")


def test_local_grants_are_not_subject_to_the_cloud_window(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _see(T0)  # origin 'local', never synced: Local-mode grants keep working
    assert len(_unlocks(site)) == 1


def _directory(site, *, with_grant=True):
    person = {"id": site["person"], "customer_id": "cust-1", "display_name": "Alice", "status": "active", "access_enabled": 1,
              "created_at": "2026-01-01", "updated_at": "2026-01-01"}
    embedding = {"id": "emb-cloud-1", "person_id": site["person"], "customer_id": "cust-1", "engine": ARCFACE[0],
                 "engine_version": ARCFACE[1], "embedding_json": "[1.0, 0.0]", "quality": 0.9, "created_at": "2026-01-01"}
    grant = {"id": "grant-cloud-1", "camera_id": "cam-1", "person_id": site["person"], "relay_channel": 1, "pulse_ms": 3000,
             "cooldown_seconds": 10, "min_confidence": 0.5, "enabled": 1}
    return {"people": [person], "embeddings": [embedding], "watchlists": [], "watchlist_members": [],
            "door_grants": [grant] if with_grant else []}


def test_a_successful_resync_restores_and_a_revoking_resync_keeps_denying(site, monkeypatch):
    import facial_embedding_sync
    enable_physical_face_access(monkeypatch)
    now = datetime.now().replace(microsecond=0)
    with connection() as db:
        db.execute("DELETE FROM facial_rules")
    _stamp(now - timedelta(minutes=30))
    facial_embedding_sync._replace_local_directory("cust-1", _directory(site))  # sync works again: stamped now
    _see(now)
    assert len(_unlocks(site)) == 1
    _relock(site)
    facial_embedding_sync._replace_local_directory("cust-1", _directory(site, with_grant=False))  # grant revoked
    _see(now + timedelta(minutes=5))
    assert len(_unlocks(site)) == 1
    with connection() as db:
        assert db.execute("SELECT COUNT(*) FROM facial_rules WHERE origin='cloud'").fetchone()[0] == 0


def test_a_stale_snapshot_on_disk_cannot_authorize_after_a_restart(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    _cloud_grant(site)
    _stamp(T0 - timedelta(hours=3))  # the appliance was offline; the old grant is still on disk
    restarted = make_service(mock_adapters={"door-1": site["adapter"]}, scheduler=FakeScheduler())
    ac.set_service(restarted)
    facial_events.reset_state()
    import facial_embedding_sync
    facial_embedding_sync.reset_sync_state()  # process memory gone
    events = _see(T0)
    assert events and _unlocks(site) == [] and _attempts()[-1] == ("refused", "cloud_grant_stale")


# ================================================================ deployment gate (2026-10-02, Codex follow-up)
# The dedicated physical flag alone is not enough: automatic facial unlock
# also needs the general Face Access flags, ANYAICAM_ENV=production and an
# EXPLICIT appliance runtime role (edge or combined). Everything else fails
# closed before the access-control service is called.

@pytest.mark.parametrize("environment", ["development", "staging", "local", None])
def test_a_non_production_environment_never_reaches_the_adapter_even_with_the_flag_on(site, monkeypatch, environment):
    enable_physical_face_access(monkeypatch, environment=environment)
    events = _see(T0)  # the real chain: record_facial_events -> ... -> AccessControlService
    assert events and events[0]["match_state"] == "known"
    assert _unlocks(site) == [] and _attempts()[-1] == ("refused", "face_access_not_production")


def test_a_cloud_runtime_never_reaches_the_adapter_even_with_the_flag_on(site, monkeypatch):
    enable_physical_face_access(monkeypatch, role="cloud")
    _see(T0)
    assert _unlocks(site) == [] and _attempts()[-1] == ("refused", "face_access_runtime_role_not_approved")


@pytest.mark.parametrize("role", [None, "", "appliance", "EDGE-x", "worker"])
def test_a_missing_or_unknown_runtime_role_never_reaches_the_adapter(site, monkeypatch, role):
    enable_physical_face_access(monkeypatch, role=role)
    _see(T0)
    assert _unlocks(site) == [] and _attempts()[-1] == ("refused", "face_access_runtime_role_not_approved")


def test_the_general_face_access_flag_is_still_required(site, monkeypatch):
    enable_physical_face_access(monkeypatch)
    monkeypatch.setattr(relay_control, "FACIAL_ACCESS_CONTROL_ENABLED", False)
    with connection() as db:  # called directly: main.py would not even pass a relay provider
        engine = ApprovedEngine()
        provider = door_access.CameraDoorProvider({"id": "cam-1", "customer_id": "cust-1", "name": "Front Door"},
                                                  relay_control.MockRelayProvider(), person_id=site["person"], db=db,
                                                  engine=engine, observation=observation(engine), now=T0)
        result = provider.trigger(relay_control.RelayRequest(channel=1, reason="facial_event:flag-1", dry_run=False))
    assert result.suppressed_reason == "face_access_not_enabled" and _unlocks(site) == []


@pytest.mark.parametrize("role", ["edge", "combined", " Edge "])
def test_an_approved_production_appliance_with_arcface_reaches_the_adapter(site, monkeypatch, role):
    enable_physical_face_access(monkeypatch, role=role)
    _see(T0)
    assert len(_unlocks(site)) == 1 and _attempts() == [("accepted", None)]


def test_full_pipeline_arcface_fallback_to_haar_records_the_event_but_never_unlocks(site, monkeypatch):
    """Production appliance, flag on -- but ArcFace failed to initialize and
    get_engine() fell back to Haar. The FR event is recorded; the door is not."""
    enable_physical_face_access(monkeypatch)
    with connection() as db:
        facial_people.add_reference_image(db, customer_id="cust-1", person_id=site["person"], embedding=(1.0, 0.0),
                                          engine=fr.HaarEmbeddingFaceEngine.name, engine_version=fr.HaarEmbeddingFaceEngine.version,
                                          now="2026-01-01")
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "arcface")
    monkeypatch.setattr(fr, "_build_named_onnx_engine", lambda name: None)  # model missing / init failed
    monkeypatch.setattr(fr.HaarEmbeddingFaceEngine, "detect_faces", lambda self, image: [fr.FaceDetection(0, 0, 10, 10)])
    monkeypatch.setattr(fr.HaarEmbeddingFaceEngine, "embed", lambda self, crop: (1.0, 0.0))
    fr.reset_engine()
    try:
        facial_events.reset_state()
        with connection() as db:  # engine=None: production resolves get_engine() itself
            events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1",
                                                        person_crop_bgr=np.zeros((50, 50, 3), dtype=np.uint8), now=T0,
                                                        relay_provider=relay_control.MockRelayProvider(simulated=True))
        assert isinstance(fr.get_engine(), fr.HaarEmbeddingFaceEngine)
    finally:
        fr.reset_engine()
    assert events and events[0]["match_state"] == "known" and events[0]["engine"] == "haar_intensity"
    assert _unlocks(site) == [] and _attempts()[-1] == ("refused", "engine_not_access_approved")
