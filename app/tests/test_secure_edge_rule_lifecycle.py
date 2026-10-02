"""Secure Edge state is cleared when a camera's rules go away (2026-10-02,
Codex finding on daca1d7).

The worker's empty-rules branch (every rule deleted/disabled, or Smart
Motion entitlement lost) reset analytics_rules_engine but not security_rules,
so crossing state and the per-rule alarm cooldown (_last_alarm_at) survived.
A security line restored moments later inherited the cooldown, and a real,
fresh armed crossing raised no alarm. Driven through real worker cycles.
"""
import asyncio
import json

import numpy as np
import pytest

import analytics_rules_engine
import customer_analytics_rule_worker as worker
import main
import recording_uploader
import security_modes
import security_rules
from database_backend import override_target
from partner_db import connection, initialize_database

LINE = [{"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}]  # "inbound" protects the left side


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    path = tmp_path / "edge.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            now = "2026-10-02"
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) "
                       "VALUES('yard','cust-1','site-1','appl-1','Yard',1,'configured',?)", (now,))
            db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,"
                       "enabled,created_at,updated_at) VALUES('sec-1','cust-1','site-1','appl-1','yard','security_line','Back fence','inbound',?,1,?,?)",
                       (json.dumps(LINE), now, now))
            security_modes.store_synced_state(db, "cust-1", "site-1", "away", {"alarm_cooldown_seconds": 600}, None)
        security_rules.reset_state()
        analytics_rules_engine.reset_camera(1)
        alarms = []
        state = {"entitled": True, "x": None}
        monkeypatch.setattr(recording_uploader, "_camera_identity",
                            lambda n: {"camera_id": "yard", "smart_motion_enabled": state["entitled"]})
        monkeypatch.setattr(main, "detect_objects_frame", lambda n: {
            "ok": True, "frame": np.zeros((1000, 1000, 3), dtype=np.uint8),
            "detections": [] if state["x"] is None else [
                {"class_name": "person", "confidence": 0.9, "x": state["x"], "y": 600, "width": 60, "height": 200}]})
        monkeypatch.setattr(worker, "save_rule_event_thumbnail", lambda *a, **k: None)
        monkeypatch.setattr(worker, "persist_rule_event", lambda camera, fired, now, thumb: alarms.append(fired) or {"id": f"e{len(alarms)}"})
        monkeypatch.setattr(worker, "speak_alarm_talkdowns", lambda *a, **k: 0)
        monkeypatch.setattr(main, "_local_recording_settings", lambda n: {"mode": "continuous"})
        yield state, alarms
        security_rules.reset_state()
        analytics_rules_engine.reset_camera(1)


async def _cycle(monkeypatch):
    async def stop(seconds):
        raise asyncio.CancelledError()
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    try:
        await worker.customer_analytics_rule_worker(1)
    except asyncio.CancelledError:
        pass


WALK_IN = tuple(range(700, 370, -15))  # 15 px steps: the tracker keeps one person
WALK_OUT = tuple(range(385, 720, 15))


def _walk_in(state, monkeypatch, xs=WALK_IN):
    for x in xs:
        state["x"] = x
        asyncio.run(_cycle(monkeypatch))


@pytest.mark.parametrize("removal", ["entitlement_lost", "rules_removed"])
def test_a_restored_security_line_alarms_on_a_fresh_crossing_within_the_old_cooldown(edge, monkeypatch, removal):
    state, alarms = edge
    _walk_in(state, monkeypatch)
    assert len(alarms) == 1 and alarms[0]["analytic_type"] == "intrusion_alarm"
    # the camera loses its rules for a cycle (entitlement loss, or every rule disabled)
    if removal == "entitlement_lost":
        state["entitled"] = False
    else:
        with connection() as db:
            db.execute("UPDATE customer_analytics_rules SET enabled=0 WHERE id='sec-1'")
    state["x"] = None
    asyncio.run(_cycle(monkeypatch))
    assert not any(key[0] == 1 for key in security_rules._last_alarm_at)
    assert not any(key[0] == 1 for key in security_rules._state)
    # restored well inside the previous 600 s cooldown; a person crosses again
    state["entitled"] = True
    with connection() as db:
        db.execute("UPDATE customer_analytics_rules SET enabled=1 WHERE id='sec-1'")
    _walk_in(state, monkeypatch)
    assert len(alarms) == 2


def test_without_a_removal_the_cooldown_still_applies(edge, monkeypatch):
    """Control: the cooldown itself is unchanged -- only stale state is cleared."""
    state, alarms = edge
    _walk_in(state, monkeypatch)
    _walk_in(state, monkeypatch, xs=WALK_OUT)          # leaves the protected side
    _walk_in(state, monkeypatch)                       # enters again within the cooldown
    assert len(alarms) == 1
