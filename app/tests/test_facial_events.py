"""AAC match-event pipeline -- Phase 1. End-to-end (real SQLite
database, real facial_people.py + facial_recognition.py + relay_control.py
logic) but with an injected fixed-vector face engine, so every test
controls the exact cosine similarity a "detected face" produces instead
of depending on real face photos or the real Haar cascade succeeding on
a synthetic image. Only synthetic numpy arrays stand in for camera
frames anywhere in this file.
"""

import math

import numpy as np
import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_facial_events_import.db"):
    import facial_events
    import facial_people
    import facial_recognition as fr
    from partner_db import connection, initialize_database
    from relay_control import MockRelayProvider

NOW = "2026-09-08T00:00:00"


class _FixedVectorEngine(fr.FaceEngine):
    """Always reports exactly one face (a fixed bbox) whose embedding is
    a caller-chosen vector -- lets tests dial in an exact cosine
    similarity against an enrolled embedding instead of depending on
    real pixel content."""

    name = "haar_intensity"  # matches the production engine name so facial_people's engine-scoped queries apply
    version = "1"

    def __init__(self, vector, faces: int = 1):
        self.vector = vector
        self.faces = faces

    def detect_faces(self, image_bgr):
        # Spaced well apart (150px) so distinct faces never share the
        # same coarse debounce grid cell (see facial_events.py's own
        # _UNKNOWN_POSITION_GRID_PX) -- this is what makes
        # test_multiple_faces_in_one_frame_each_produce_an_event a real
        # test of "distinct faces", not identical, overlapping ones.
        return [fr.FaceDetection(index * 150, 0, 10, 10) for index in range(self.faces)]

    def embed(self, face_crop_bgr):
        return self.vector

    def capability(self):
        return {"engine": self.name, "version": self.version, "available": True, "gpu": False, "reason": None}


def _unit_vector_at_similarity(similarity: float) -> tuple[float, float]:
    """A unit vector whose cosine similarity against the fixed enrolled
    reference vector (1.0, 0.0) is exactly `similarity`."""
    return (similarity, math.sqrt(max(0.0, 1.0 - similarity**2)))


def _frame():
    return np.zeros((50, 50, 3), dtype=np.uint8)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_facial_events.db"


@pytest.fixture()
def db(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as conn:
            conn.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C1','c1@example.test','active','real',?)", (NOW,))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Site 1',?)", (NOW,))
            conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST0001',?)", (NOW,))
            # door_access_enabled=1 (2026-09-17): the automatic-unlock
            # evaluation path below is now scoped to configured door
            # cameras only -- see facial_events.record_facial_events()'s
            # own comment. cam-1 is the one camera in this fixture the
            # relay-rule tests below exercise; cam-2 deliberately stays
            # a non-door camera.
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number,door_access_enabled,door_relay_channel) VALUES('cam-1','cust-1','site-1','appl-1','Camera 1','active',?,1,1,1)", (NOW,))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES('cam-2','cust-1','site-1','appl-1','Camera 2','active',?,2)", (NOW,))
        with connection() as conn:
            yield conn
    facial_events.reset_state()


def _entitle(db, camera_id="cam-1"):
    db.execute(
        "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,?,?,?)",
        (camera_id, "facial_recognition", "active", NOW, NOW),
    )


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_RECOGNITION_ENABLED", True)
    facial_events.reset_state()
    yield
    facial_events.reset_state()


# --------------------------------------------------------------- gating


def test_returns_empty_when_facial_recognition_disabled(db, monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_RECOGNITION_ENABLED", False)
    _entitle(db)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events == []


def test_returns_empty_when_camera_not_entitled(db):
    # No camera_analytics_entitlements row inserted for cam-1.
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events == []


def test_returns_empty_when_camera_number_unmapped(db):
    events = facial_events.record_facial_events(db, camera_number=999, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events == []


def test_returns_empty_when_no_face_detected(db):
    _entitle(db)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0), faces=0))
    assert events == []


def test_respects_per_camera_scoping(db, monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_CAMERAS", frozenset({2}))
    _entitle(db)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events == []


# --------------------------------------------------------------- matching states


def test_known_person_match_creates_known_event(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert len(events) == 1
    assert events[0]["match_state"] == "known"
    assert events[0]["matched_person_id"] == person_id
    assert events[0]["matched_person_name"] == "Alice"


def test_unmatched_face_is_unknown_by_default(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    # Orthogonal vector -> similarity 0.0, far below default threshold.
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    assert len(events) == 1
    assert events[0]["match_state"] == "unknown"
    assert events[0]["matched_person_id"] is None


def test_unknown_events_can_be_disabled_via_settings(db):
    _entitle(db)
    facial_people.update_settings(db, customer_id="cust-1", unknown_person_events_enabled=False, now=NOW)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    assert events == []


def test_watchlist_member_match_creates_watchlist_event(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Mallory", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events[0]["match_state"] == "watchlist"
    assert events[0]["matched_watchlist_id"] == watchlist_id
    assert events[0]["matched_watchlist_name"] == "Banned"


# --------------------------------------------------------------- threshold boundaries


def test_confidence_just_below_configured_threshold_is_unknown(db):
    _entitle(db)
    facial_people.update_settings(db, customer_id="cust-1", min_confidence=0.9, now=NOW)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    vector = _unit_vector_at_similarity(0.89)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine(vector))
    assert events[0]["match_state"] == "unknown"
    assert events[0]["matched_person_id"] is None


def test_confidence_at_exactly_configured_threshold_is_known(db):
    _entitle(db)
    facial_people.update_settings(db, customer_id="cust-1", min_confidence=0.9, now=NOW)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    vector = _unit_vector_at_similarity(0.9)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine(vector))
    assert events[0]["match_state"] == "known"
    assert events[0]["matched_person_id"] == person_id


# --------------------------------------------------------------- multiple faces / duplicate suppression


def test_multiple_faces_in_one_frame_each_produce_an_event(db):
    _entitle(db)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0), faces=3))
    assert len(events) == 3


def test_duplicate_sighting_within_debounce_window_is_suppressed(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    engine = _FixedVectorEngine((1.0, 0.0))
    first = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=engine)
    second = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=engine)
    assert len(first) == 1
    assert len(second) == 0  # same person, same camera, within the debounce window


def test_different_cameras_do_not_share_debounce_state(db):
    _entitle(db, camera_id="cam-1")
    _entitle(db, camera_id="cam-2")
    engine = _FixedVectorEngine((0.0, 1.0))
    first = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=engine)
    second = facial_events.record_facial_events(db, camera_number=2, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=engine)
    assert len(first) == 1
    assert len(second) == 1


# --------------------------------------------------------------- tenant isolation


def test_events_are_scoped_to_the_cameras_own_customer(db):
    """A second customer's camera must never be able to match against
    -- or be confused with -- cust-1's enrolled people, even though
    both live in the same physical database."""
    with connection() as second_db:
        second_db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
        second_db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Site 2',?)", (NOW,))
        second_db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-2','cust-2','site-2','AIC-TEST0002',?)", (NOW,))
        second_db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES('cam-3','cust-2','site-2','appl-2','Camera 3','active',?,3)", (NOW,))
        second_db.execute(
            "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("cam-3", "facial_recognition", "active", NOW, NOW),
        )
    _entitle(db, camera_id="cam-1")
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    events = facial_events.record_facial_events(db, camera_number=3, appliance_id="appl-2", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events[0]["match_state"] == "unknown"  # cust-1's Alice must not leak into cust-2's camera


# --------------------------------------------------------------- event query / detail


def test_list_events_filters_by_match_state(db):
    _entitle(db)
    facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    events = facial_events.list_events(db, customer_id="cust-1", match_state="unknown")
    assert len(events) == 1
    assert facial_events.list_events(db, customer_id="cust-1", match_state="known") == []


def test_list_events_never_returns_another_customers_rows(db):
    with connection() as second_db:
        second_db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
        second_db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Site 2',?)", (NOW,))
        second_db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-2','cust-2','site-2','AIC-TEST0002',?)", (NOW,))
        second_db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES('cam-3','cust-2','site-2','appl-2','Camera 3','active',?,3)", (NOW,))
        second_db.execute(
            "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("cam-3", "facial_recognition", "active", NOW, NOW),
        )
    _entitle(db, camera_id="cam-1")
    facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    facial_events.record_facial_events(db, camera_number=3, appliance_id="appl-2", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    cust1_events = facial_events.list_events(db, customer_id="cust-1")
    assert all(event["customer_id"] == "cust-1" for event in cust1_events)
    assert len(cust1_events) == 1


# --------------------------------------------------------------- appliance-scoped camera_number


def _seed_second_appliance_with_camera_number_1(db):
    """A second appliance (different customer) whose own Camera 1 shares
    camera_number=1 with appl-1's cam-1 -- camera_number is only unique
    per appliance, never globally."""
    with connection() as second_db:
        second_db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
        second_db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Site 2',?)", (NOW,))
        second_db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-2','cust-2','site-2','AIC-TEST0002',?)", (NOW,))
        second_db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES('cam-9','cust-2','site-2','appl-2','Other Camera 1','active',?,1)", (NOW,))
    _entitle(db, camera_id="cam-1")
    _entitle(db, camera_id="cam-9")


def test_same_camera_number_on_two_appliances_resolves_only_to_each_appliances_own_camera(db):
    _seed_second_appliance_with_camera_number_1(db)
    first = facial_events._camera_tenant_context(db, 1, "appl-1")
    second = facial_events._camera_tenant_context(db, 1, "appl-2")
    assert (first["id"], first["customer_id"], first["appliance_id"]) == ("cam-1", "cust-1", "appl-1")
    assert (second["id"], second["customer_id"], second["appliance_id"]) == ("cam-9", "cust-2", "appl-2")


def test_record_facial_events_attributes_a_detection_to_the_detecting_appliances_camera(db):
    _seed_second_appliance_with_camera_number_1(db)
    on_appl_1 = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    on_appl_2 = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-2", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    assert [(e["camera_id"], e["customer_id"]) for e in on_appl_1] == [("cam-1", "cust-1")]
    assert [(e["camera_id"], e["customer_id"]) for e in on_appl_2] == [("cam-9", "cust-2")]


def test_missing_appliance_identity_resolves_no_camera_and_records_nothing(db):
    _entitle(db)
    assert facial_events._camera_tenant_context(db, 1, None) is None
    assert facial_events._camera_tenant_context(db, 1, "") is None
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id=None, person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events == []
    assert db.execute("SELECT COUNT(*) FROM facial_events").fetchone()[0] == 0


def test_unknown_appliance_resolves_no_camera(db):
    _entitle(db)
    assert facial_events._camera_tenant_context(db, 1, "appl-does-not-exist") is None


def test_ambiguous_camera_number_on_one_appliance_is_logged_and_resolves_no_camera(db, caplog):
    # idx_cameras_appliance_camera_number normally makes this state
    # impossible; dropped here to simulate a database that never got it,
    # which is exactly the case the lookup's own guard exists for.
    db.execute("DROP INDEX idx_cameras_appliance_camera_number")
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES('cam-dup','cust-1','site-1','appl-1','Duplicate 1','active',?,1)", (NOW,))
    _entitle(db)
    with caplog.at_level("WARNING", logger="anyaicam.facial_events"):
        assert facial_events._camera_tenant_context(db, 1, "appl-1") is None
        events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert events == []
    assert "ambiguous_camera_number" in caplog.text


def test_main_detection_hook_scopes_facial_events_to_the_persisted_appliance_identity():
    """main.py's one live call site must pass this appliance's own
    persisted identity -- never omit it or pass a hardcoded id."""
    from pathlib import Path

    main_source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    start = main_source.index("facial_events.record_facial_events(")
    call_site = " ".join(main_source[start:start + 500].split())
    assert "appliance_id=active_appliance_id()" in call_site


def test_get_event_detail_returns_none_for_wrong_customer(db):
    _entitle(db)
    created = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)))
    event_id = created[0]["id"]
    assert facial_events.get_event_detail(db, customer_id="cust-2", event_id=event_id) is None
    assert facial_events.get_event_detail(db, customer_id="cust-1", event_id=event_id) is not None


def test_event_survives_person_deletion_with_denormalized_name(db):
    """Event/audit integrity: deleting the enrolled person (which hard-
    deletes their biometric templates) must never delete or blank out
    past match history -- the event keeps its own snapshot of the name
    taken at match time."""
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    created = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    facial_people.delete_person(db, customer_id="cust-1", person_id=person_id)
    detail = facial_events.get_event_detail(db, customer_id="cust-1", event_id=created[0]["id"])
    assert detail is not None
    assert detail["matched_person_name"] == "Alice"


# --------------------------------------------------------------- relay integration (mock only)


def test_relay_not_evaluated_when_no_provider_given(db):
    """Matches main.py's own live hook, which never passes a
    relay_provider -- Phase 1's live detection path only ever records
    history, it never triggers a relay on its own."""
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    assert "relay_outcomes" not in events[0]


def test_relay_rule_triggers_mock_channel_1_on_known_match(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    db.execute(
        "INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,min_confidence,created_at,updated_at) "
        "VALUES('rule-1','cust-1',NULL,'Open door','known_person',1,3000,10,0,1,0.5,?,?)",
        (NOW, NOW),
    )
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    assert events[0]["relay_outcomes"][0]["channel"] == 1
    assert events[0]["relay_outcomes"][0]["activated"] is True
    assert provider.calls[0].dry_run is False


def test_relay_rule_in_dry_run_never_activates(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    db.execute(
        "INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,min_confidence,created_at,updated_at) "
        "VALUES('rule-1','cust-1',NULL,'Open door','known_person',2,3000,10,1,1,0.5,?,?)",
        (NOW, NOW),
    )
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    assert events[0]["relay_outcomes"][0]["channel"] == 2
    assert events[0]["relay_outcomes"][0]["activated"] is False
    assert events[0]["relay_outcomes"][0]["dry_run"] is True


def test_relay_rule_never_triggers_for_unknown_faces(db):
    _entitle(db)
    db.execute(
        "INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,min_confidence,created_at,updated_at) "
        "VALUES('rule-1','cust-1',NULL,'Open door','known_person',3,3000,10,0,1,0.5,?,?)",
        (NOW, NOW),
    )
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((0.0, 1.0)), relay_provider=provider)
    assert events[0]["match_state"] == "unknown"
    assert provider.calls == []  # relay is never even consulted for an unknown face


def test_access_outcomes_are_persisted_on_the_facial_events_row(db):
    """2026-09-16: the identity match -> authorization decision ->
    access-control command chain was already correctly separated and
    tested, but its outcome was only ever returned in-memory and never
    written to the database -- so "was this person granted or denied
    access" could not be answered after the fact. Proves it now lands
    on the real facial_events row, not just the return value."""
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    db.execute(
        "INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,min_confidence,created_at,updated_at) "
        "VALUES('rule-1','cust-1',NULL,'Open door','known_person',1,3000,10,0,1,0.5,?,?)",
        (NOW, NOW),
    )
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    row = db.execute("SELECT access_outcomes_json FROM facial_events WHERE id=?", (events[0]["id"],)).fetchone()
    import json
    stored = json.loads(row["access_outcomes_json"])
    assert stored == events[0]["relay_outcomes"]
    assert stored[0]["activated"] is True


def test_access_outcomes_column_stays_null_when_no_provider_is_given(db):
    """The common case today (ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED
    defaults to false): NULL must mean "never evaluated", distinguishable
    from an empty list meaning "evaluated, nothing applied"."""
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)))
    row = db.execute("SELECT access_outcomes_json FROM facial_events WHERE id=?", (events[0]["id"],)).fetchone()
    assert row["access_outcomes_json"] is None


def test_no_real_hardware_is_touched_by_relay_evaluation(db):
    """Structural safety check: MockRelayProvider is the only relay
    provider this whole test suite (and this Phase 1 codebase) ever
    constructs -- there is no hardware-backed provider class anywhere
    to accidentally use instead."""
    import relay_control

    provider_classes = [
        value
        for value in vars(relay_control).values()
        if isinstance(value, type) and issubclass(value, relay_control.RelayProvider)
    ]
    assert provider_classes == [relay_control.RelayProvider, relay_control.MockRelayProvider]


# --------------------------------------------------------------------------
# Face Access -- Door/Relay Control (2026-09-17): mode 1/2/3 behavior
# --------------------------------------------------------------------------


def _door_rule(db, *, relay_channel=1, dry_run=0, min_confidence=0.5):
    db.execute(
        "INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,min_confidence,created_at,updated_at) "
        "VALUES('door-rule-1','cust-1',NULL,'Open door','known_person',?,3000,10,?,1,?,?,?)",
        (relay_channel, dry_run, min_confidence, NOW, NOW),
    )


def test_mode1_authorized_auto_unlock_has_no_notify_message_and_audits_success(db):
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    _door_rule(db, dry_run=0)
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    assert "door_notify_message" not in events[0]
    audit = db.execute("SELECT * FROM door_access_events WHERE facial_event_id=?", (events[0]["id"],)).fetchone()
    assert audit is not None
    assert audit["trigger_type"] == "automatic"
    assert audit["door_name"] == "Camera 1"
    assert audit["matched_person_name"] == "Alice"
    assert audit["authorization_result"] == "authorized"
    assert audit["relay_result"] == "activated"
    assert audit["success"] == 1


def test_mode2_recognized_without_authorization_notifies_and_never_unlocks(db):
    """No facial_rules row at all for this customer -- a real person is
    recognized but no rule authorizes automatic entry."""
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Bob", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    assert provider.calls == []
    assert events[0]["door_notify_message"] == "Bob is at Camera 1."
    audit = db.execute("SELECT * FROM door_access_events WHERE facial_event_id=?", (events[0]["id"],)).fetchone()
    assert audit["matched_person_name"] == "Bob"
    assert audit["authorization_result"] == "not_authorized"
    assert audit["relay_result"] == "skipped"
    assert audit["success"] == 0


def test_mode2_a_dry_run_rule_matching_still_notifies_and_never_unlocks(db):
    """A rule DOES match (this person IS in principle authorized) but
    dry_run=1 means it never actually activates -- still mode 2, not
    mode 1: nothing unlocked, so the customer still needs to know."""
    _entitle(db)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Carol", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    _door_rule(db, dry_run=1)
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    assert events[0]["door_notify_message"] == "Carol is at Camera 1."
    audit = db.execute("SELECT * FROM door_access_events WHERE facial_event_id=?", (events[0]["id"],)).fetchone()
    assert audit["authorization_result"] == "authorized"
    assert audit["relay_result"] == "dry_run"
    assert audit["success"] == 0


def test_mode3_unknown_person_at_door_camera_notifies_and_never_unlocks(db):
    _entitle(db)
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((9.0, 9.0)), relay_provider=provider)
    assert provider.calls == []
    assert events[0]["door_notify_message"] == "Unknown person at Camera 1."
    audit = db.execute("SELECT * FROM door_access_events WHERE facial_event_id=?", (events[0]["id"],)).fetchone()
    assert audit["matched_person_id"] is None
    assert audit["authorization_result"] == "unknown_person"
    assert audit["relay_result"] == "skipped"
    assert audit["success"] == 0


def test_non_door_camera_never_evaluates_rules_or_notifies_even_with_a_provider(db):
    """cam-2 in this fixture is deliberately NOT door_access_enabled --
    a facial-recognition camera that isn't a configured door must
    behave exactly as it always did (match recorded, nothing more),
    regardless of relay_provider being supplied."""
    db.execute(
        "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("cam-2", "facial_recognition", "active", NOW, NOW),
    )
    _door_rule(db, dry_run=0)
    provider = MockRelayProvider()
    events = facial_events.record_facial_events(db, camera_number=2, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((1.0, 0.0)), relay_provider=provider)
    assert provider.calls == []
    assert "door_notify_message" not in events[0]
    assert db.execute("SELECT COUNT(*) c FROM door_access_events").fetchone()["c"] == 0


def test_door_access_events_written_even_without_a_relay_provider(db):
    """FACIAL_ACCESS_CONTROL_ENABLED defaults to false, so main.py's real
    hook passes relay_provider=None today -- mode 2/3 notification and
    auditing must still work in that default configuration; only the
    ACTUAL relay evaluation (evaluate_access_rules()) is skipped."""
    _entitle(db)
    events = facial_events.record_facial_events(db, camera_number=1, appliance_id="appl-1", person_crop_bgr=_frame(), now=NOW, engine=_FixedVectorEngine((9.0, 9.0)))
    assert events[0]["door_notify_message"] == "Unknown person at Camera 1."
    audit = db.execute("SELECT * FROM door_access_events WHERE facial_event_id=?", (events[0]["id"],)).fetchone()
    assert audit["authorization_result"] == "unknown_person"
