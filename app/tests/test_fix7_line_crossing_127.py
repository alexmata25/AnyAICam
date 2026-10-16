"""Fix 7 (owner decision 2026-10-16): the Line Crossing switch also governs security lines.

OFF: line-crossing AND security-line rules stop processing and alerting, on the cloud's rule delivery and on the
edge (offline too). ON: the customer's previously configured rules return, each subject to its own enabled setting.
Rule configuration and historical events are never deleted.
"""
import json

import pytest

import analytics_control_center as acc
import customer_analytics_rule_worker as worker
from database_backend import override_target
from partner_db import connection, initialize_database

LINE = json.dumps([{"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}])
NOW = "2026-10-16"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "fix7.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('p','P',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('c','p','C','c@example.test','active',?)", (NOW,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s','c','Home',?)", (NOW,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('a','c','s','AIC-1',?)", (NOW,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) "
                       "VALUES('yard','c','s','a','Yard',1,'configured',?)", (NOW,))
            for rid, rtype, enabled, direction in (("line-1", "line_crossing", 1, "both"), ("sec-1", "security_line", 1, "inbound"),
                                                   ("sec-off", "security_line", 0, "inbound"), ("zone-1", "intrusion", 1, None)):
                db.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,"
                           "geometry_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (rid, "c", "s", "a", "yard", rtype, rid, direction, LINE, enabled, NOW, NOW))
        yield path


def _switch(db, key, on):
    db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) VALUES('yard',?,?,?,'owner') "
               "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET enabled=excluded.enabled", (key, int(on), NOW))


RULES = [{"id": "line-1", "camera_id": "yard", "rule_type": "line_crossing"},
         {"id": "sec-1", "camera_id": "yard", "rule_type": "security_line"},
         {"id": "zone-1", "camera_id": "yard", "rule_type": "intrusion"}]
CAMERA = [{"id": "yard", "smart_motion_enabled": True, "people_counting_enabled": False}]


# --------------------------------------------------------------------------------- cloud rule delivery
def test_line_crossing_off_stops_security_lines_in_the_delivered_configuration(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            assert [r["id"] for r in acc.filter_rule_items(db, RULES, CAMERA)] == ["line-1", "sec-1", "zone-1"]
            _switch(db, "line_crossing", False)
            assert [r["id"] for r in acc.filter_rule_items(db, RULES, CAMERA)] == ["zone-1"]
            _switch(db, "line_crossing", True)
            assert [r["id"] for r in acc.filter_rule_items(db, RULES, CAMERA)] == ["line-1", "sec-1", "zone-1"]


def test_smart_motion_off_still_stops_security_lines(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _switch(db, "smart_motion", False)
            assert "sec-1" not in [r["id"] for r in acc.filter_rule_items(db, RULES, CAMERA)]


# --------------------------------------------------------------------------------- edge runtime (offline too)
def test_on_the_edge_line_crossing_off_stops_both_and_on_restores_each_rules_own_setting(db_path):
    with override_target(sqlite_path=str(db_path)):
        assert sorted(r["id"] for r in worker.load_rules_for_camera("yard")) == ["line-1", "sec-1", "zone-1"]
        with connection() as db:
            _switch(db, "line_crossing", False)
        assert [r["id"] for r in worker.load_rules_for_camera("yard")] == ["zone-1"]
        with connection() as db:
            _switch(db, "line_crossing", True)
        restored = sorted(r["id"] for r in worker.load_rules_for_camera("yard"))
        assert restored == ["line-1", "sec-1", "zone-1"]  # "sec-off" stays off: its own setting is respected
        assert {r["analytic_type"] for r in worker.load_rules_for_camera("yard") if r["id"] == "sec-1"} == {"security_line"}


def test_turning_off_never_deletes_configuration_or_history(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            before = db.execute("SELECT id,rule_type,enabled,geometry_json FROM customer_analytics_rules ORDER BY id").fetchall()
            _switch(db, "line_crossing", False)
            after = db.execute("SELECT id,rule_type,enabled,geometry_json FROM customer_analytics_rules ORDER BY id").fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]


# --------------------------------------------------------------------------------- the switch itself
def test_line_crossing_can_be_turned_back_on_when_only_security_lines_are_configured():
    """Re-enabling requires a configured rule of the switch's types; security lines count, so a customer whose only
    lines are security lines can restore them."""
    assert "security_line" in acc.RULE_TYPES["line_crossing"]


def test_the_control_center_says_the_switch_covers_security_lines():
    assert "security-line alarms" in acc.ANALYTICS["line_crossing"]["dependency"]
