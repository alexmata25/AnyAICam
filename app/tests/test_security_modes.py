"""Arm Stay / Arm Away / Disarm (security_modes)."""
import sqlite3

import pytest

import security_modes as sm


@pytest.fixture()
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "sec.db")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT)")
    for cam in ("front", "yard", "living"):
        con.execute("INSERT INTO cameras VALUES(?,?,?)", (cam, "cust-1", "site-1"))
    con.execute("INSERT INTO cameras VALUES('other','cust-2','site-9')")
    yield con
    con.close()


def test_disarmed_arms_nothing_stay_arms_only_chosen_cameras_away_arms_all_by_default():
    settings = {"stay_camera_ids": ["front", "yard"]}
    assert not any(sm.camera_is_armed("disarmed", c, settings) for c in ("front", "yard", "living"))
    assert [c for c in ("front", "yard", "living") if sm.camera_is_armed("stay", c, settings)] == ["front", "yard"]
    assert all(sm.camera_is_armed("away", c, settings) for c in ("front", "yard", "living"))
    assert not sm.camera_is_armed("away", "living", dict(settings, away_camera_ids=["front"]))


def test_new_sites_start_disarmed(db):
    assert sm.get_state(db, "cust-1", "site-1")["mode"] == "disarmed"
    assert sm.camera_armed_now(db, "front") == (False, sm.get_state(db, "cust-1", "site-1"))


def test_mode_changes_are_recorded_with_actor(db):
    state = sm.set_mode(db, "cust-1", "site-1", "Arm_Away", actor="owner@example.test")
    assert state["mode"] == "away" and state["changed_by"] == "owner@example.test" and state["changed_at"]
    assert sm.camera_armed_now(db, "living")[0] is True
    sm.set_mode(db, "cust-1", "site-1", "disarm" if False else "disarmed", actor="owner@example.test")
    assert sm.camera_armed_now(db, "living")[0] is False
    with pytest.raises(ValueError):
        sm.set_mode(db, "cust-1", "site-1", "panic", actor="x")


def test_settings_only_accept_this_sites_cameras_and_clamp_values(db):
    state = sm.save_settings(db, "cust-1", "site-1", {"stay_camera_ids": ["front", "other", "nope"], "alarm_cooldown_seconds": 1,
                                                       "talkdown_message": "x" * 999, "evil": "ignored"},
                             actor="owner", valid_camera_ids={"front", "yard", "living"})
    s = state["settings"]
    assert s["stay_camera_ids"] == ["front"] and s["alarm_cooldown_seconds"] == 10 and len(s["talkdown_message"]) == 300
    assert "evil" not in s


def test_arm_state_is_per_customer_site(db):
    sm.set_mode(db, "cust-1", "site-1", "away", actor="owner")
    assert sm.camera_armed_now(db, "other")[0] is False  # another customer's camera is unaffected


def test_edge_mirror_keeps_the_last_synced_state(db):
    sm.store_synced_state(db, "cust-1", "site-1", "stay", {"stay_camera_ids": ["yard"]}, "2026-09-28T12:00:00")
    assert sm.camera_armed_now(db, "yard")[0] is True and sm.camera_armed_now(db, "front")[0] is False
    assert sm.get_state(db, "cust-1", "site-1")["changed_by"] == "cloud-sync"
