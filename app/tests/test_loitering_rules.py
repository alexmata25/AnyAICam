"""Loitering (2026-10-01): a polygon rule that fires when a person stays
inside it for the rule's own dwell time. It extends the existing customer
rule path end to end -- rules API/editor, migration, cloud configuration,
edge mirror, analytics_rules_engine's zone dwell, the rule worker, analytics
sync and cloud fan-out -- rather than adding a pipeline of its own.

Also covers the zone-dwell lifecycle fixes that came with it: bounded
occlusion, outage reset, removed rules / lost entitlement, and a restart
starting dwell at the first observed presence."""
import asyncio
import json
import secrets
import sqlite3
import time

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import analytics_rules_engine as engine
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash

ZONE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]


@pytest.fixture(autouse=True)
def _isolated_engine():
    def clear():
        engine.reset_tracker()
        for state in engine._RULE_STATE_DICTS:
            state.clear()
        engine._last_cycle_at.clear()
    clear()
    yield
    clear()


def _person(cls="person", x=80, y=80, conf=0.9):
    return {"class_id": 0, "class_name": cls, "confidence": conf, "x": x, "y": y, "width": 40, "height": 40}


def _loitering(dwell=30, rule_id="loiter-1", geometry=ZONE):
    return {"id": rule_id, "analytic_type": "loitering", "name": "Porch", "enabled": True,
            "geometry": geometry, "confidence_threshold": 0.0, "dwell_seconds": dwell}


def _intrusion(rule_id="zone-1"):
    return {"id": rule_id, "analytic_type": "intrusion", "name": "Yard", "enabled": True, "geometry": ZONE, "confidence_threshold": 0.0}


def _cycle(rules, detections, now, camera=1):
    engine.start_cycle(camera, now)
    tracked = engine.update_tracker(camera, detections)
    return engine.evaluate_rules(camera, tracked, rules, 200, 200, now=now)


def _run(rules, readings, step=5.0):
    """One reading per cycle (a list of detections, or [] for a missed
    cycle); returns [(cycle_index, analytic_type)] for everything fired."""
    fired = []
    for index, detections in enumerate(readings):
        for event in _cycle(rules, detections, index * step):
            fired.append((index, event["analytic_type"]))
    return fired


# ------------------------------------------------------------------ state machine

def test_loitering_fires_once_after_the_rules_own_dwell():
    readings = [[_person()]] * 10  # 0, 5, ... 45 s
    assert _run([_loitering(dwell=30)], readings) == [(6, "loitering")]  # 30 s after first seen, once


def test_a_longer_dwell_is_respected_and_carried_on_the_event():
    assert _run([_loitering(dwell=60)], [[_person()]] * 12) == []
    engine.reset_camera(1)
    events = [e for i in range(14) for e in _cycle([_loitering(dwell=60)], [_person()], i * 5.0)]
    assert len(events) == 1
    assert events[0]["dwell_seconds"] == 60 and events[0]["dwell_threshold_seconds"] == 60
    assert events[0]["event_type"] == "person" and events[0]["zone_name"] == "Porch"


@pytest.mark.parametrize("cls", ["car", "truck", "dog", "cat", "bicycle"])
def test_only_people_loiter(cls):
    assert _run([_loitering(dwell=10)], [[_person(cls=cls)]] * 10) == []


def test_intrusion_keeps_its_built_in_dwell_and_any_class():
    readings = [[_person(cls="car")]] * 3
    assert _run([_intrusion()], readings) == [(1, "intrusion")]  # 5 s, unchanged


HALF = [{"x": 0.0, "y": 0.0}, {"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}, {"x": 0.0, "y": 1.0}]  # left half of a 200 px frame


def _walk(xs, dwell, step=5.0):
    """A person 40 px wide walking in small steps (the tracker keeps one
    track); None is a missed cycle. Returns the cycle indexes that fired."""
    rule = _loitering(dwell=dwell, geometry=HALF)
    fired = []
    for index, x in enumerate(xs):
        if _cycle([rule], [_person(x=x)] if x is not None else [], index * step):
            fired.append(index)
    return fired


def test_someone_walking_through_never_loiters():
    # walks in and straight out (20 s), unseen twice, briefly back in: never 30 s inside in one stay
    assert _walk([50, 60, 70, 80, 90, None, None, 70, 70], dwell=30) == []


@pytest.mark.parametrize("dwell,expected", [(None, 30.0), ("abc", 30.0), (2, 10.0), (99999, 1800.0), (45, 45.0)])
def test_dwell_is_bounded_and_defaults_safely(dwell, expected):
    assert engine.rule_dwell_seconds(_loitering(dwell=dwell)) == expected
    assert engine.rule_dwell_seconds(_intrusion()) == engine.DEFAULT_DWELL_SECONDS


def test_a_brief_occlusion_keeps_the_dwell_running():
    """Previously one missed detection restarted the clock; the tracker
    itself tolerates TRACK_MAX_MISSED_CYCLES."""
    readings = [[_person()], [_person()], [], [], [_person()], [_person()], [_person()]]
    assert _run([_loitering(dwell=30)], readings) == [(6, "loitering")]  # measured from the first sighting


def test_an_occlusion_past_the_grace_starts_over():
    rule = _loitering(dwell=30)
    _cycle([rule], [_person()], 0.0)
    _cycle([rule], [_person()], 5.0)
    for t in (10.0, 15.0, 20.0, 25.0, 28.0):  # gone > ZONE_OCCLUSION_GRACE_SECONDS and > 3 missed cycles
        _cycle([rule], [], t)
    assert engine._dwell_entered_at == {}
    fired = [e for t in (30.0, 35.0, 40.0) for e in _cycle([rule], [_person()], t)]
    assert fired == []  # a fresh 30 s stay is needed


def test_leaving_and_coming_back_needs_a_full_new_dwell_and_fires_again():
    # fires once at 15 s; walks out (x=90 is outside); comes back at 40 s; a new full 15 s stay fires again at 55 s
    assert _walk([50, 50, 50, 50, 60, 70, 80, 90, 70, 70, 70, 70], dwell=15) == [3, 11]


def test_a_detection_outage_resets_the_camera():
    rule = _loitering(dwell=30)
    _cycle([rule], [_person()], 0.0)
    _cycle([rule], [_person()], 5.0)
    # detections fail for a minute: no cycle reaches the engine
    assert engine.start_cycle(1, 70.0) is True
    assert engine._dwell_entered_at == {} and engine._tracker_state.get(1) is None
    tracked = engine.update_tracker(1, [_person()])
    assert engine.evaluate_rules(1, tracked, [rule], 200, 200, now=70.0) == []  # dwell restarts at 70 s


def test_a_short_gap_is_not_an_outage():
    _cycle([_loitering()], [_person()], 0.0)
    assert engine.start_cycle(1, 5.0 + engine.OUTAGE_RESET_SECONDS - 6) is False


def test_removed_rules_and_lost_entitlement_drop_their_state():
    keep, gone = _loitering(rule_id="keep"), _loitering(rule_id="gone")
    _cycle([keep, gone], [_person()], 0.0)
    assert {k[1] for k in engine._dwell_entered_at} == {"keep", "gone"}
    engine.forget_rules_except(1, ["keep"])
    assert {k[1] for k in engine._dwell_entered_at} == {"keep"}
    engine.reset_camera(1)
    assert engine._dwell_entered_at == {} and engine._tracker_state.get(1) is None


def test_a_restart_starts_the_dwell_at_the_first_observed_presence():
    """All dwell state is in memory: someone already standing there when the
    worker starts is timed from that first sighting, never backdated."""
    assert _run([_loitering(dwell=30)], [[_person()]] * 6) == []  # 25 s since first seen: not yet


def test_cameras_and_rules_are_independent():
    _cycle([_loitering(rule_id="a")], [_person()], 0.0, camera=1)
    _cycle([_loitering(rule_id="a")], [_person()], 0.0, camera=2)
    engine.reset_camera(2)
    assert {k[0] for k in engine._dwell_entered_at} == {1}


# ------------------------------------------------------------------ rules API + editor

import main  # noqa: E402
import partner_portal  # noqa: E402


@pytest.fixture()
def api_db(tmp_path):
    return tmp_path / "loitering_api.db"


@pytest.fixture()
def api(api_db):
    with override_target(sqlite_path=api_db):
        initialize_database()
        conn = sqlite3.connect(api_db)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active','2026-01-01')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-cust-1','cust-1','Home','2026-01-01')")
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('app-cust-1','cust-1','site-cust-1','cloud-1','2026-01-01')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,created_at) "
                     "VALUES('cam-1','cust-1','site-cust-1','app-cust-1','Front Door','configured',1,'2026-01-01')")
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as client:
            client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None))
            yield client


URL = "/api/customer/cameras/cam-1/analytics-rules"
LOITER = {"rule_type": "loitering", "name": "Porch", "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}]}


def test_a_loitering_zone_saves_with_the_default_dwell(api):
    created = api.post(URL, json=LOITER)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rule_type"] == "loitering" and body["dwell_seconds"] == 30 and body["notifications_enabled"] is True
    assert body["direction"] is None


def test_dwell_and_notifications_save_update_and_list(api):
    rule = api.post(URL, json={**LOITER, "dwell_seconds": 120, "notifications_enabled": False}).json()
    assert rule["dwell_seconds"] == 120 and rule["notifications_enabled"] is False
    updated = api.put(f"{URL}/{rule['id']}", json={"dwell_seconds": 45}).json()
    assert updated["dwell_seconds"] == 45 and updated["notifications_enabled"] is False  # untouched fields kept
    updated = api.put(f"{URL}/{rule['id']}", json={"notifications_enabled": True}).json()
    assert updated["notifications_enabled"] is True and updated["dwell_seconds"] == 45
    listed = api.get(URL).json()["rules"]
    assert [(r["dwell_seconds"], r["notifications_enabled"]) for r in listed] == [(45, True)]


@pytest.mark.parametrize("dwell", [5, 9, 1801, "abc", 30.5, True, -1])
def test_an_invalid_dwell_is_rejected(api, dwell):
    response = api.post(URL, json={**LOITER, "dwell_seconds": dwell})
    assert response.status_code == 400


def test_dwell_does_not_apply_to_other_rule_types(api):
    zone = {**LOITER, "rule_type": "intrusion", "dwell_seconds": 60}
    assert api.post(URL, json=zone).status_code == 400
    intrusion = api.post(URL, json={**LOITER, "rule_type": "intrusion"}).json()
    assert intrusion["dwell_seconds"] is None  # intrusion keeps its built-in behaviour


def test_a_loitering_zone_needs_a_polygon(api):
    line = {**LOITER, "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}]}
    assert api.post(URL, json=line).status_code == 400
    assert api.post(URL, json={**LOITER, "direction": "inbound"}).status_code == 400


def test_notifications_can_only_be_muted_on_alerting_rules(api):
    assert api.post(URL, json={**LOITER, "rule_type": "intrusion", "notifications_enabled": False}).status_code == 200
    excluded = {**LOITER, "rule_type": "exclusion", "notifications_enabled": False}
    assert api.post(URL, json=excluded).status_code == 400
    assert api.post(URL, json={**LOITER, "notifications_enabled": "no"}).status_code == 400


def test_the_editor_offers_loitering_with_its_controls(api):
    page = api.get("/customer/cameras/cam-1/analytics-rules")
    assert page.status_code == 200
    html = page.text
    assert '<option value="loitering">' in html
    assert 'id="rule-dwell"' in html and 'min="10"' in html and 'max="1800"' in html
    assert 'id="rule-notifications"' in html
    assert "const NOTIFYING=[\"intrusion\", \"line_crossing\", \"loitering\"];" in html
    assert "payload.dwell_seconds=dwell" in html


def test_the_migration_is_additive(api_db, api):
    conn = sqlite3.connect(api_db)
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(customer_analytics_rules)")}
    assert columns["dwell_seconds"][3] == 0  # nullable
    assert columns["notifications_enabled"][4] == "1"
    # an older row written without the new columns reads back with notifications on
    conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at) "
                 "VALUES('old','cust-1','site-cust-1','app-cust-1','cam-1','line_crossing','Old',NULL,'[]',1,'2026-01-01','2026-01-01')")
    conn.commit()
    assert conn.execute("SELECT dwell_seconds,notifications_enabled FROM customer_analytics_rules WHERE id='old'").fetchone() == (None, 1)


# ------------------------------------------------------------------ cloud configuration + edge mirror

with override_target(sqlite_path="/tmp/test_loitering_rules_cloud.db"):
    import appliance_cloud  # noqa: E402


def _headers(appliance_id="appl-1", credential="cred-1"):
    return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"}


def _seed_cloud(db, *, customer_id="cust-1", appliance_id="appl-1", camera_id="cam-1", credential="cred-1"):
    now = "2026-10-01T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (now,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
               (customer_id, "partner-1", "C", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Home", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
               (appliance_id, customer_id, f"site-{customer_id}", f"AIC-{appliance_id}", now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
               (f"cred-{appliance_id}", appliance_id, password_hash(credential), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (camera_id, customer_id, f"site-{customer_id}", appliance_id, "Front Door", 1, "configured", now))
    db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)", (f"owner-{customer_id}", "partner-1", f"owner-{customer_id}@example.test", "Owner",
                                               "customer_owner", password_hash("x"), 1, customer_id, "active", now))


def _seed_cloud_rule(db, rule_id="loiter-1", *, customer_id="cust-1", appliance_id="appl-1", camera_id="cam-1",
                     rule_type="loitering", dwell=90, notifications=1, enabled=1):
    db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,"
               "enabled,created_at,updated_at,created_by,dwell_seconds,notifications_enabled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (rule_id, customer_id, f"site-{customer_id}", appliance_id, camera_id, rule_type, "Porch", None, json.dumps(ZONE),
                enabled, "2026-10-01", "2026-10-01", None, dwell, notifications))


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    path = tmp_path / "loitering_cloud.db"
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(path)):
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as client:
            yield client, path


def test_the_appliance_configuration_carries_dwell_and_notifications(cloud):
    client, path = cloud
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            _seed_cloud(db)
            _seed_cloud_rule(db, dwell=90, notifications=0)
            _seed_cloud_rule(db, "zone-1", rule_type="intrusion", dwell=None)
            _seed_cloud_rule(db, "off", enabled=0)
    rules = {r["id"]: r for r in client.get("/api/appliance/configuration", headers=_headers()).json()["analytics_rules"]}
    assert set(rules) == {"loiter-1", "zone-1"}
    assert rules["loiter-1"]["rule_type"] == "loitering" and rules["loiter-1"]["dwell_seconds"] == 90
    assert rules["loiter-1"]["notifications_enabled"] is False
    assert rules["zone-1"]["dwell_seconds"] is None and rules["zone-1"]["notifications_enabled"] is True


import edge_camera_sync  # noqa: E402


def test_the_edge_mirrors_dwell_and_notifications(tmp_path):
    path = tmp_path / "edge.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P','now')")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active','now')")
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home','now')")
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1','now')")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) "
                       "VALUES('cam-1','cust-1','site-1','appl-1','Front Door',1,'configured','now')")
            base = {"customer_id": "cust-1", "site_id": "site-1", "camera_id": "cam-1", "name": "Porch", "geometry": ZONE, "updated_at": "x"}
            edge_camera_sync._reconcile_analytics_rules(db, "appl-1", [
                {**base, "id": "new", "rule_type": "loitering", "dwell_seconds": 120, "notifications_enabled": False},
                {**base, "id": "older-cloud", "rule_type": "intrusion"},
                {**base, "id": "bad", "rule_type": "loitering", "dwell_seconds": "x"},
            ], "now")
            rows = {r["id"]: dict(r) for r in db.execute("SELECT id,dwell_seconds,notifications_enabled FROM customer_analytics_rules")}
            assert rows["new"]["dwell_seconds"] == 120 and rows["new"]["notifications_enabled"] == 0
            assert rows["older-cloud"]["dwell_seconds"] is None and rows["older-cloud"]["notifications_enabled"] == 1
            assert rows["bad"]["dwell_seconds"] is None  # the engine then uses the default
            # an edit in the cloud updates the mirror in place
            edge_camera_sync._reconcile_analytics_rules(db, "appl-1", [
                {**base, "id": "new", "rule_type": "loitering", "dwell_seconds": 60, "notifications_enabled": True}], "now")
            assert dict(db.execute("SELECT dwell_seconds,notifications_enabled FROM customer_analytics_rules WHERE id='new'").fetchone()) == \
                {"dwell_seconds": 60, "notifications_enabled": 1}


# ------------------------------------------------------------------ worker, clip, sync payload

import customer_analytics_rule_worker as worker  # noqa: E402
import recording_uploader  # noqa: E402


@pytest.fixture()
def edge_db(tmp_path):
    path = tmp_path / "edge_worker.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            now = "2026-10-01"
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-cust-1','cust-1','Home',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-cust-1','AIC-1',?)", (now,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) "
                       "VALUES('cam-1','cust-1','site-cust-1','appl-1','Front Door',1,'configured',?)", (now,))
            _seed_cloud_rule(db, dwell=45, notifications=0)
        yield path


def test_the_worker_loads_loitering_with_its_dwell(edge_db):
    rules = worker.load_rules_for_camera("cam-1")
    assert rules == [{"id": "loiter-1", "analytic_type": "loitering", "name": "Porch", "direction": "both", "confidence_threshold": 0.0,
                      "enabled": True, "geometry": ZONE, "dwell_seconds": 45, "notifications_enabled": False}]
    assert worker.event_type_for("loitering") == "loitering"


def test_a_loitering_event_is_saved_with_its_clip_and_metadata(monkeypatch):
    from datetime import datetime
    saved, scheduled = [], []
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_analytics_event", saved.append)
    monkeypatch.setattr(main, "_analytics_media_owner", lambda camera, event_id, now: event_id)
    monkeypatch.setattr(main, "_schedule_owned_analytics_clip", lambda *a: scheduled.append(a))
    fired = {"rule_id": "loiter-1", "analytic_type": "loitering", "zone_name": "Porch", "confidence": 0.9, "track_id": "t1",
             "direction": None, "dwell_seconds": 47, "dwell_threshold_seconds": 45}
    record = worker.persist_rule_event(1, fired, datetime(2026, 10, 1, 12, 0, 0), "/thumb.jpg")
    assert record["event_type"] == "loitering" and record["rule_name"] == "Loitering (Porch)"
    assert record["rule_id"] == "loiter-1" and record["dwell_seconds"] == 47 and record["dwell_threshold_seconds"] == 45
    assert saved == [record] and scheduled  # the event owns and builds its clip when nothing covers the moment


async def _one_cycle(monkeypatch):
    async def stop(seconds):
        raise asyncio.CancelledError()
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    try:
        await worker.customer_analytics_rule_worker(1)
    except asyncio.CancelledError:
        pass


def test_losing_smart_motion_resets_the_cameras_dwell(edge_db, monkeypatch):
    _cycle([_loitering(rule_id="loiter-1")], [_person()], 0.0)
    assert engine._dwell_entered_at
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda n: {"camera_id": "cam-1", "smart_motion_enabled": False})
    monkeypatch.setattr(main, "detect_objects_frame", lambda n: pytest.fail("no inference without the entitlement"))
    asyncio.run(_one_cycle(monkeypatch))
    assert engine._dwell_entered_at == {} and engine._tracker_state.get(1) is None


def test_a_deleted_rule_loses_its_state_on_the_next_cycle(edge_db, monkeypatch):
    _cycle([_loitering(rule_id="deleted-rule")], [_person()], 0.0)
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda n: {"camera_id": "cam-1", "smart_motion_enabled": True})
    monkeypatch.setattr(main, "detect_objects_frame", lambda n: {"ok": False})  # detection failing this cycle
    asyncio.run(_one_cycle(monkeypatch))
    assert all(key[1] != "deleted-rule" for key in engine._dwell_entered_at)


def test_loitering_runs_whether_or_not_the_site_is_armed(edge_db, monkeypatch):
    """Not coupled to Armed Stay/Away: the general rule path evaluates it;
    security_modes is consulted only for security lines."""
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda n: {"camera_id": "cam-1", "smart_motion_enabled": True})
    import security_modes
    monkeypatch.setattr(security_modes, "camera_armed_now", lambda *a, **k: pytest.fail("loitering must not consult the armed state"))
    seen = []
    monkeypatch.setattr(engine, "evaluate_rules", lambda camera, tracked, rules, w, h, now=None: seen.append([r["analytic_type"] for r in rules]) or [])
    monkeypatch.setattr(main, "detect_objects_frame", lambda n: {"ok": True, "frame": np.zeros((200, 200, 3), dtype=np.uint8), "detections": [_person()]})
    asyncio.run(_one_cycle(monkeypatch))
    assert seen == [["loitering"]]


import analytics_sync  # noqa: E402


def test_the_sync_payload_carries_the_rule_and_the_stay():
    event = {"id": "e1", "event_type": "loitering", "timestamp": "2026-10-01T12:00:00", "confidence": 0.9, "rule_id": "loiter-1",
             "rule_name": "Loitering (Porch)", "zone_name": "Porch", "dwell_seconds": 47, "dwell_threshold_seconds": 45}
    payload = analytics_sync._build_payload(event)
    assert payload["event_type"] == "loitering"
    assert payload["detections"] == [{"rule_name": "Loitering (Porch)", "rule_id": "loiter-1", "zone_name": "Porch",
                                      "dwell_seconds": 47, "dwell_threshold_seconds": 45}]
    notification = analytics_sync._build_notification_payload(event, "cam-1")
    assert notification["event_type"] == "loitering" and notification["rule_id"] == "loiter-1"


# ------------------------------------------------------------------ cloud fan-out, dedupe, media parents

def _ingest(client, local_id="loc-1", rule_id="loiter-1", event_type="loitering", camera_id="cam-1", headers=None):
    return client.post(f"/api/appliance/analytics/{camera_id}/events", headers=headers or _headers(), json={
        "local_event_id": local_id, "event_type": event_type, "confidence": 0.9, "object_count": 1,
        "detections": [{"rule_name": "Loitering (Porch)", "rule_id": rule_id, "zone_name": "Porch", "dwell_seconds": 47}],
        "event_timestamp": "2026-10-01T12:00:00"})


def _rows(path, query, params=()):
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            return [dict(r) for r in db.execute(query, params).fetchall()]


def test_a_loitering_event_notifies_once_and_a_replay_does_not(cloud):
    client, path = cloud
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            _seed_cloud(db)
            _seed_cloud_rule(db, notifications=1)
    assert _ingest(client).json()["status"] == "accepted"
    assert _ingest(client).status_code == 200  # the appliance retries
    assert len(_rows(path, "SELECT id FROM detection_events WHERE event_type='loitering'")) == 1
    assert len(_rows(path, "SELECT id FROM notifications WHERE customer_id='cust-1'")) == 1


def test_a_rule_with_notifications_off_keeps_the_event_but_sends_nothing(cloud, monkeypatch):
    client, path = cloud
    sent = []
    monkeypatch.setattr(appliance_cloud, "fanout_appliance_event", lambda *a, **k: sent.append(a) or 1)
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            _seed_cloud(db)
            _seed_cloud_rule(db, notifications=0)
    assert _ingest(client).json()["status"] == "accepted"
    assert len(_rows(path, "SELECT id FROM detection_events WHERE event_type='loitering'")) == 1  # event + clip kept
    assert sent == []  # no push, in-app, email or SMS
    # the older /api/appliance/events route honours it too
    response = client.post("/api/appliance/events", headers=_headers(), json={"events": [
        {"id": "legacy-1", "event_type": "loitering", "camera_id": "cam-1", "timestamp": "2026-10-01T12:00:00", "rule_id": "loiter-1"}]})
    assert response.json()["notifications_created"] == 0 and sent == []


def test_another_tenants_rule_cannot_mute_this_customers_alerts(cloud, monkeypatch):
    client, path = cloud
    sent = []
    monkeypatch.setattr(appliance_cloud, "fanout_appliance_event", lambda *a, **k: sent.append(a) or 1)
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            _seed_cloud(db)
            _seed_cloud(db, customer_id="cust-2", appliance_id="appl-2", camera_id="cam-2", credential="cred-2")
            _seed_cloud_rule(db, "theirs", customer_id="cust-2", appliance_id="appl-2", camera_id="cam-2", notifications=0)
    _ingest(client, rule_id="theirs")
    assert len(sent) == 1


def test_an_unknown_or_missing_rule_still_notifies(cloud, monkeypatch):
    client, path = cloud
    sent = []
    monkeypatch.setattr(appliance_cloud, "fanout_appliance_event", lambda *a, **k: sent.append(a) or 1)
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            _seed_cloud(db)
    _ingest(client, local_id="a", rule_id="deleted-since")
    _ingest(client, local_id="b", rule_id=None)
    assert len(sent) == 2


def test_a_loitering_event_may_reuse_the_persons_activity_clip():
    parents = appliance_cloud.ANALYTICS_MEDIA_PARENT_TYPES["loitering"]
    assert "person" in parents and "loitering" in parents and "intrusion" in parents
    assert "loitering" in appliance_cloud.ANALYTICS_MEDIA_PARENT_TYPES["intrusion"]


# ------------------------------------------------------------------ notifications, email, labels, SMS default

import customer_analytics_panel  # noqa: E402
import notification_email  # noqa: E402
import notification_engine  # noqa: E402
import notification_preferences  # noqa: E402


def test_loitering_is_a_registered_notification_type():
    assert "loitering" in notification_engine.SUPPORTED
    assert notification_preferences.EVENT_TYPES["loitering"] == "Loitering"
    assert "loitering" in notification_email.MEDIA_EVENT_TYPES  # the email waits for its picture and links to the clip
    assert customer_analytics_panel.event_type_label("loitering") == "Loitering"
    assert customer_analytics_panel.event_type_message("loitering") == "Person loitering"


def test_sms_defaults_are_unchanged_and_loitering_is_not_an_emergency(monkeypatch):
    monkeypatch.delenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", raising=False)
    assert notification_engine.DEFAULT_SMS_ALERT_EVENT_TYPES == frozenset(
        {"intrusion_alarm", "aac_voice_call", "camera_offline", "appliance_offline", "storage_problem"})
    assert notification_engine.sms_alert_allowed("loitering") is False
    assert "loitering" not in notification_engine.EMERGENCY_EVENT_TYPES
    monkeypatch.setenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", "loitering")  # an operator can still opt it in
    assert notification_engine.sms_alert_allowed("loitering") is True


def test_the_email_links_to_the_events_clip(monkeypatch):
    monkeypatch.setattr(main, "_customer_event_playback_href", lambda camera_id, ts, event_id, has_clip: f"/playback?camera={camera_id}&event={event_id}")
    path = notification_email.event_path({"event_type": "loitering", "event_id": "ev-1", "camera_id": "cam-1", "timestamp": "2026-10-01T12:00:00"})
    assert path == "/playback?camera=cam-1&event=ev-1"


def test_investigate_and_playback_filters_know_loitering():
    assert main._aaco_event_category("loitering") == "intrusion"
    source = open(main.__file__, encoding="utf-8").read()
    assert "eventType==='intrusion'||eventType==='loitering'" in source
    assert source.count('<option value="loitering">Loitering</option>') >= 3
