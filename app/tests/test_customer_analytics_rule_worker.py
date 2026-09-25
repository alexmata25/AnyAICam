"""Real edge execution for the tenant-safe customer_analytics_rules
table (2026-09-21, customer_analytics_rule_worker.py) -- the piece
this session's own runtime-path trace (docs/intrusion-line-crossing-
customer-ui-gap.md) documented as missing. Covers: loading/translating
locally-mirrored rules, Event-mode clip linkage, and Events/Investigate
visibility (a real AnalyticsEventModel row with correct camera/
timestamp/rule metadata, reaching the exact same ANALYTICS_EVENTS_FILE
read path every other event type already uses).

Duplicate/debounce behavior, line/intrusion geometry, and direction
handling are already proven at the pure-engine level by
test_analytics_rules_engine.py (ported alongside analytics_rules_
engine.py itself) -- this file is the integration layer on top: real
DB-backed rule loading, a real worker cycle, and real event persistence.

Reuses test_people_counting_worker_thumbnail.py's own established
harness pattern (monkeypatching main.detect_objects_frame/
append_analytics_event/AI_THUMBNAILS_FOLDER, driving one worker
iteration by making main.asyncio.sleep raise CancelledError).
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import customer_analytics_rule_worker as worker  # noqa: E402
import recording_uploader  # noqa: E402
from database_backend import override_target  # noqa: E402
from partner_db import connection, initialize_database  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_analytics_rule_worker.db"


@pytest.fixture(autouse=True)
def _seeded_db(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        now = "2026-09-21T00:00:00"
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Customer','cust1@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Test Site',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES('cam-1','cust-1','site-1','appl-1','Camera 1',1,'configured',?)", (now,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES('cam-2','cust-1','site-1','appl-1','Camera 2',2,'configured',?)", (now,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES('cam-other','cust-1','site-1','appl-1','Camera Other',3,'configured',?)", (now,))
    with override_target(sqlite_path=str(db_path)):
        yield


def _seed_rule(camera_id="cam-1", *, rule_id="rule-1", rule_type="line_crossing", direction="both", enabled=1, geometry=None, name="Test rule"):
    geometry = geometry if geometry is not None else [{"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}]
    now = "2026-09-21T00:00:00"
    with connection() as db:
        db.execute(
            "INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at,created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rule_id, "cust-1", "site-1", "appl-1", camera_id, rule_type, name, direction, json.dumps(geometry), enabled, now, now, None),
        )


# Intrusion/line-crossing rules are evaluated only for Smart Motion-entitled cameras.
ENTITLED_CAM_1 = {"camera_id": "cam-1", "smart_motion_enabled": True}


def _fake_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


# ------------------------------------------------------- load_rules_for_camera


def test_loads_only_this_cameras_own_enabled_rules_translated_for_the_engine(db_path):
    _seed_rule("cam-1", rule_id="rule-1")
    _seed_rule("cam-1", rule_id="rule-2", enabled=0)
    _seed_rule("cam-2", rule_id="rule-3")

    rules = worker.load_rules_for_camera("cam-1")

    assert [r["id"] for r in rules] == ["rule-1"]
    rule = rules[0]
    assert rule["analytic_type"] == "line_crossing"
    assert rule["direction"] == "both"
    assert rule["geometry"] == [{"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}]
    assert rule["enabled"] is True


def test_no_rules_for_this_camera_returns_an_empty_list(db_path):
    _seed_rule("cam-other")
    assert worker.load_rules_for_camera("cam-1") == []


def test_intrusion_rule_translates_with_no_direction(db_path):
    _seed_rule("cam-1", rule_type="intrusion", direction=None, geometry=[{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}])
    rules = worker.load_rules_for_camera("cam-1")
    assert rules[0]["analytic_type"] == "intrusion"


# --------------------------------------------------------------- event_type_for


def test_line_crossing_gets_the_pre_existing_flat_event_type():
    """"line_crossing" already exists as a dropdown option in main.py's
    own Investigate/analytics filters -- this must match it exactly,
    never a direction-suffixed variant, or those pre-existing filters
    would silently never match a real event again."""
    assert worker.event_type_for("line_crossing") == "line_crossing"


def test_intrusion_gets_the_pre_existing_intrusion_event_type():
    assert worker.event_type_for("intrusion") == "intrusion"


# ------------------------------------------------------- persist_rule_event


def test_persist_rule_event_links_the_event_mode_recording(monkeypatch, db_path):
    from datetime import datetime

    calls = []
    monkeypatch.setattr(main, "linked_recording_for", lambda camera_number, now: calls.append((camera_number, now)) or "/recordings/media/camera1/clip.mp4#t=1,5")
    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))

    now = datetime(2026, 9, 21, 12, 0, 0)
    fired = {"rule_id": "rule-1", "analytic_type": "line_crossing", "zone_name": "Driveway", "direction": "inbound", "track_id": 7, "confidence": 0.0}
    record = worker.persist_rule_event(1, fired, now, "/recordings/media/ai/2026-09-21/thumb.jpg")

    assert calls == [(1, now)]
    assert record["linked_recording"] == "/recordings/media/camera1/clip.mp4#t=1,5"
    assert recorded == [record]


def test_persist_rule_event_carries_correct_camera_timestamp_and_rule_metadata(monkeypatch, db_path):
    from datetime import datetime

    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))

    now = datetime(2026, 9, 21, 12, 0, 0)
    fired = {"rule_id": "rule-9", "analytic_type": "intrusion", "zone_name": "Backyard", "direction": None, "track_id": 3, "confidence": 0.87}
    record = worker.persist_rule_event(4, fired, now, None)

    assert record["camera"] == 4
    assert record["event_type"] == "intrusion"
    assert record["rule_id"] == "rule-9"
    assert record["track_id"] == 3
    assert "Backyard" in record["rule_name"]
    assert record["confidence"] == 0.87
    assert record["mock"] is False


def test_persist_rule_event_appears_in_investigate_via_the_existing_read_path(monkeypatch, db_path, tmp_path):
    """The real "appears in Events/Investigate" proof: once persisted,
    the event is readable back through analytics_events() (the exact
    function the Investigate page's own route already calls) and is
    correctly categorized by the existing _aaco_event_category()
    bucketing -- no Investigate-page-specific code needed for this
    event type."""
    from datetime import datetime

    fake_events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", fake_events_file)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)

    now = datetime(2026, 9, 21, 12, 0, 0)
    fired = {"rule_id": "rule-1", "analytic_type": "line_crossing", "zone_name": "Driveway", "direction": "inbound", "track_id": 1, "confidence": 0.0}
    worker.persist_rule_event(1, fired, now, None)

    events = main.analytics_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "line_crossing"
    assert main._aaco_event_category(events[0]["event_type"]) == "line_crossing"


def test_intrusion_event_categorizes_correctly_too(monkeypatch, db_path, tmp_path):
    from datetime import datetime

    fake_events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", fake_events_file)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)

    now = datetime(2026, 9, 21, 12, 0, 0)
    fired = {"rule_id": "rule-2", "analytic_type": "intrusion", "zone_name": "Backyard", "direction": None, "track_id": 2, "confidence": 0.0}
    worker.persist_rule_event(1, fired, now, None)

    events = main.analytics_events()
    assert main._aaco_event_category(events[0]["event_type"]) == "intrusion"


# ----------------------------------------------------- full worker cycle


async def _run_one_cycle(camera_number: int, monkeypatch) -> None:
    async def fake_sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    try:
        await worker.customer_analytics_rule_worker(camera_number)
    except asyncio.CancelledError:
        pass


def test_worker_idles_harmlessly_with_no_camera_id_resolved(monkeypatch, db_path):
    """No cloud identity yet resolved for this camera -- must never
    call detect_objects_frame() at all, matching people_counting_
    worker()'s own established "idle harmlessly" shape."""
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: None)
    called = {"n": 0}

    def fake_detect(camera_number):
        called["n"] += 1
        return {"ok": False}

    monkeypatch.setattr(main, "detect_objects_frame", fake_detect)
    asyncio.run(_run_one_cycle(1, monkeypatch))
    assert called["n"] == 0


def test_worker_idles_harmlessly_with_no_rules_for_this_camera(monkeypatch, db_path):
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: ENTITLED_CAM_1)
    called = {"n": 0}

    def fake_detect(camera_number):
        called["n"] += 1
        return {"ok": False}

    monkeypatch.setattr(main, "detect_objects_frame", fake_detect)
    asyncio.run(_run_one_cycle(1, monkeypatch))
    assert called["n"] == 0


def test_full_cycle_a_real_line_crossing_produces_a_thumbnailed_event(monkeypatch, db_path, tmp_path):
    """End-to-end: a rule saved through the customer portal and mirrored
    locally, a real detection crossing the line, a real persisted event
    with a real thumbnail file on disk -- the complete chain this
    session's runtime-path trace previously documented as absent."""
    _seed_rule("cam-1", geometry=[{"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}], direction="both")
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: ENTITLED_CAM_1)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(worker, "CUSTOMER_ANALYTICS_RULE_INTERVAL_SECONDS", 0.01)

    frame = _fake_frame()
    y_steps = [0.30, 0.40, 0.50, 0.60]
    calls = {"n": 0}

    def fake_detect_objects_frame(camera_number):
        calls["n"] += 1
        y = y_steps[min(calls["n"] - 1, len(y_steps) - 1)]
        return {
            "ok": True, "frame": frame, "error": None,
            "detections": [{"class_name": "person", "confidence": 0.9, "x": 270, "y": int(y * 480) - 50, "width": 100, "height": 100}],
        }

    monkeypatch.setattr(main, "detect_objects_frame", fake_detect_objects_frame)

    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))

    async def driver():
        task = asyncio.create_task(worker.customer_analytics_rule_worker(1))
        for _ in range(200):
            if recorded:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(driver())

    assert recorded, "expected a real crossing event to be recorded"
    event = recorded[0]
    assert event["event_type"] == "line_crossing"
    assert event["thumbnail"], "crossing event must carry a real thumbnail URL"
    assert list(tmp_path.rglob("*.jpg")), "no thumbnail file was actually written"


def test_a_lingering_person_inside_an_intrusion_zone_does_not_fire_before_the_real_dwell_threshold(monkeypatch, db_path, tmp_path):
    """Duplicate/debounce integration proof: analytics_rules_engine's
    own dwell-timer state machine (test_analytics_rules_engine.py's own
    unit tests already cover the full fire-once/leave-and-reenter
    behavior in isolation, using an injected `now`) is genuinely wired
    through the real worker -- driven here for a bounded burst of real
    wall-clock cycles, well under the real DEFAULT_DWELL_SECONDS, a
    person standing still inside the zone must produce ZERO events yet.
    This proves the worker passes a real, live time.monotonic() into
    evaluate_rules() rather than a frozen or fake value that would
    fire immediately."""
    import analytics_rules_engine
    _seed_rule("cam-1", rule_type="intrusion", direction=None, geometry=[{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}])
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: ENTITLED_CAM_1)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "detect_objects_frame", lambda camera_number: {
        "ok": True, "frame": _fake_frame(), "error": None,
        "detections": [{"class_name": "person", "confidence": 0.9, "x": 270, "y": 190, "width": 100, "height": 100}],
    })
    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))
    monkeypatch.setattr(worker, "CUSTOMER_ANALYTICS_RULE_INTERVAL_SECONDS", 0.01)

    async def driver():
        task = asyncio.create_task(worker.customer_analytics_rule_worker(1))
        await asyncio.sleep(0.3)  # many cycles, all well under the real dwell threshold
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert 0.3 < analytics_rules_engine.DEFAULT_DWELL_SECONDS
    asyncio.run(driver())

    assert recorded == [], "an intrusion event fired before the real dwell threshold was reached"


# ------------------------------------------------------- entitlement + rule types (2026-09-25)


def test_rules_are_not_evaluated_for_a_camera_without_smart_motion(monkeypatch, db_path):
    _seed_rule("cam-1")
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: {"camera_id": "cam-1", "smart_motion_enabled": False})
    called = {"n": 0}

    def fake_detect(camera_number):
        called["n"] += 1
        return {"ok": False}

    monkeypatch.setattr(main, "detect_objects_frame", fake_detect)
    asyncio.run(_run_one_cycle(1, monkeypatch))
    assert called["n"] == 0  # no AI inference, no events: the zone is stored, not a free analytic


def test_entitlement_gate_follows_the_synced_smart_motion_flag():
    assert worker.camera_rules_entitled({"camera_id": "c", "smart_motion_enabled": True}) is True
    assert worker.camera_rules_entitled({"camera_id": "c", "smart_motion_enabled": False}) is False
    assert worker.camera_rules_entitled({"camera_id": "c"}) is False
    assert worker.camera_rules_entitled(None) is False


def test_a_people_counting_line_is_never_evaluated_as_an_alert_rule(db_path):
    _seed_rule("cam-1", rule_id="count-line", rule_type="people_counting")
    _seed_rule("cam-1", rule_id="alert-line", rule_type="line_crossing")
    assert [rule["id"] for rule in worker.load_rules_for_camera("cam-1")] == ["alert-line"]
