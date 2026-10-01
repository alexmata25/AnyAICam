"""Face Access for apartments and businesses (2026-10-01).

Enroll tenant -> face template -> unit, door and schedule -> recognized face
-> permission check -> door command -> audit; plus revoke, sync to the
appliance, and the portal's remote Unlock reaching the appliance.
"""
import asyncio
import json
import threading
from datetime import datetime

import pytest

import appliance_control
import door_access
import face_access_people
import facial_embedding_sync
import facial_events
import facial_people
import relay_control
from database_backend import override_target
from partner_db import connection, initialize_database

NOW = "2026-10-01T12:00:00"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    path = tmp_path / "face_access.db"
    with override_target(sqlite_path=path):
        initialize_database()
        with connection() as conn:
            conn.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p','P','approved','real',?)", (NOW,))
            for cid in ("cust-a", "cust-b"):
                conn.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
                             (cid, "p", cid, f"{cid}@example.test", "active", "real", NOW))
                conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{cid}", cid, f"Building {cid}", NOW))
            conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-cust-a','AIC-A',?)", (NOW,))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
                         "VALUES('lobby','cust-a','site-cust-a','appl-a','Lobby Door','configured',1,1,1,4000,?)", (NOW,))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,door_access_enabled,door_relay_channel,created_at) "
                         "VALUES('garage','cust-a','site-cust-a','appl-a','Garage Door','configured',2,1,2,?)", (NOW,))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('yard','cust-a','site-cust-a','Yard','configured',3,?)", (NOW,))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,created_at) "
                         "VALUES('other-door','cust-b','site-cust-b','Other Door','configured',1,1,1,?)", (NOW,))
            person = facial_people.enroll_person(conn, customer_id="cust-a", display_name="Ana Tenant", now=NOW)
        yield {"person": person, "provider": provider, "path": path}


def _save(person_id, **payload):
    with connection() as conn:
        return face_access_people.save_access(conn, customer_id="cust-a", person_id=person_id, payload=payload, actor="owner@example.test", now=NOW)


def _rules(person_id):
    with connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM facial_rules WHERE person_id=? ORDER BY camera_id", (person_id,)).fetchall()]


# ------------------------------------------------------------------ unit, site, dates, doors

def test_unit_site_dates_and_door_grants_are_saved(db):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    out = _save(pid, unit="4B", site_id="site-cust-a", access_starts_on="2026-10-01", access_expires_on="2027-09-30",
                doors=[{"camera_id": "lobby", "allowed": True, "days": ["fri", "mon", "wed"], "start": "08:00", "end": "18:00"},
                       {"camera_id": "garage", "allowed": False}])
    s = out["settings"]
    assert (s["unit"], s["site_id"], s["access_starts_on"], s["access_expires_on"], s["access_enabled"]) == ("4B", "site-cust-a", "2026-10-01", "2027-09-30", True)
    lobby = next(d for d in s["doors"] if d["camera_id"] == "lobby")
    assert lobby["allowed"] and lobby["days"] == ["mon", "wed", "fri"] and (lobby["start"], lobby["end"]) == ("08:00", "18:00")
    assert [d["camera_id"] for d in s["doors"]] == ["garage", "lobby"] or {d["camera_id"] for d in s["doors"]} == {"lobby", "garage"}
    (rule,) = _rules(pid)
    assert (rule["camera_id"], rule["trigger_type"], rule["origin"], rule["dry_run"], rule["relay_channel"], rule["pulse_ms"]) == \
           ("lobby", "specific_person", "cloud", 0, 1, 4000)


@pytest.mark.parametrize("payload,message", [
    ({"site_id": "site-cust-b"}, "sites"),
    ({"access_starts_on": "10/01/2026"}, "date"),
    ({"access_starts_on": "2026-10-02", "access_expires_on": "2026-10-01"}, "before the start"),
    ({"doors": [{"camera_id": "yard", "allowed": True}]}, "not a door"),
    ({"doors": [{"camera_id": "other-door", "allowed": True}]}, "not a door"),
    ({"doors": [{"camera_id": "lobby", "allowed": True, "start": "08:00"}]}, "both a start and an end"),
    ({"doors": [{"camera_id": "lobby", "allowed": True, "days": ["funday"]}]}, "Days must be"),
])
def test_bad_settings_are_refused_and_nothing_changes(db, payload, message):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    with pytest.raises(face_access_people.AccessSettingsError, match=message):
        _save(pid, **payload)
    assert _rules(pid) == []


def test_revoking_is_flagged_for_an_immediate_appliance_resync(db):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    assert _save(pid, doors=[{"camera_id": "lobby", "allowed": True}])["access_reduced"] is False  # adding access
    assert _save(pid, doors=[{"camera_id": "lobby", "allowed": True}, {"camera_id": "garage", "allowed": True}])["access_reduced"] is False
    assert _save(pid, doors=[{"camera_id": "lobby", "allowed": True}])["access_reduced"] is True  # a door removed
    assert _save(pid, access_enabled=False, doors=[{"camera_id": "lobby", "allowed": True}])["access_reduced"] is True


# ------------------------------------------------------------------ the engine

def _decide(db, pid, *, now_weekday="thu", today="2026-10-01", time="12:00"):
    with connection() as conn:
        return facial_events.evaluate_access_rules(
            conn, customer_id="cust-a", camera_id="lobby", match_state="known", confidence=0.95,
            matched_person_id=pid, matched_watchlist_id=None, relay_provider=relay_control.get_provider(),
            detection_event_id="det-1", current_time=time, current_weekday=now_weekday, today=today)


def test_an_authorized_face_opens_the_door_and_nothing_else_does(db):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    _save(pid, doors=[{"camera_id": "lobby", "allowed": True, "days": ["thu"], "start": "08:00", "end": "18:00"}])
    assert [o["activated"] for o in _decide(db, pid)] == [True]
    assert _decide(db, pid, now_weekday="fri") == []          # not one of the days
    assert _decide(db, pid, time="19:30") == []               # outside the hours


@pytest.mark.parametrize("change", [
    {"access_enabled": False},
    {"access_starts_on": "2026-10-02"},
    {"access_expires_on": "2026-09-30"},
])
def test_face_access_off_or_outside_its_dates_opens_nothing(db, change):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    _save(pid, doors=[{"camera_id": "lobby", "allowed": True}], **change)
    assert _decide(db, pid) == []


def test_removing_a_person_removes_their_grants_and_keeps_door_history(db):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    _save(pid, doors=[{"camera_id": "lobby", "allowed": True}])
    with connection() as conn:
        door_access.record_door_access_event(conn, customer_id="cust-a", camera_id="lobby", door_name="Lobby Door", relay_channel=1,
                                             trigger_type="facial", actor_user_id=None, actor_email=None, authorization_result="authorized",
                                             relay_result="activated", success=True, error=None, now=datetime.now(),
                                             matched_person_id=pid, matched_person_name="Ana Tenant") if "matched_person_id" in door_access.record_door_access_event.__code__.co_varnames else \
            door_access.record_door_access_event(conn, customer_id="cust-a", camera_id="lobby", door_name="Lobby Door", relay_channel=1,
                                                 trigger_type="facial", actor_user_id=None, actor_email=None, authorization_result="authorized",
                                                 relay_result="activated", success=True, error=None, now=datetime.now())
        assert facial_people.delete_person(conn, customer_id="cust-a", person_id=pid)
    assert _rules(pid) == []
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM door_access_events WHERE camera_id='lobby'").fetchone()[0] == 1


# ------------------------------------------------------------------ to the appliance

def test_the_appliance_mirrors_portal_grants_and_keeps_its_own(db):
    pid = db["person"]["id"] if isinstance(db["person"], dict) else db["person"]
    with connection() as conn:
        conn.execute("INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,min_confidence,relay_channel,pulse_ms,cooldown_seconds,dry_run,enabled,origin,created_at,updated_at) "
                     "VALUES('local-1','cust-a','garage','Installer rule','known_person',0.9,2,3000,10,1,1,'local',?,?)", (NOW, NOW))
    directory = {
        "people": [{"id": pid, "display_name": "Ana Tenant", "status": "active", "unit": "4B", "access_enabled": 0,
                    "access_starts_on": None, "access_expires_on": "2027-01-01"}],
        "embeddings": [], "watchlists": [], "watchlist_members": [],
        "door_grants": [{"id": "g1", "camera_id": "lobby", "person_id": pid, "relay_channel": 1, "pulse_ms": 4000,
                         "days_of_week": "mon,tue", "schedule_start": "07:00", "schedule_end": "19:00", "enabled": 1},
                        {"id": "g2", "camera_id": "lobby", "person_id": "someone-else", "relay_channel": 1}],
    }
    summary = facial_embedding_sync._replace_local_directory("cust-a", directory)
    assert summary["door_grants"] == 2
    rules = {r["id"]: r for r in _rules(pid)}
    assert set(rules) == {"g1"} and rules["g1"]["origin"] == "cloud" and rules["g1"]["days_of_week"] == "mon,tue"
    with connection() as conn:
        person = dict(conn.execute("SELECT * FROM facial_people WHERE id=?", (pid,)).fetchone())
        assert (person["unit"], person["access_enabled"], person["access_expires_on"]) == ("4B", 0, "2027-01-01")
        assert conn.execute("SELECT COUNT(*) FROM facial_rules WHERE id='local-1'").fetchone()[0] == 1  # never touched
    facial_embedding_sync._replace_local_directory("cust-a", {**directory, "door_grants": []})
    assert _rules(pid) == []


# ------------------------------------------------------------------ remote Unlock, cloud -> appliance

class _Channel:
    def __init__(self, answer):
        self.answer, self.sent = answer, []

    async def send_text(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message.get("type") == "door_unlock" and self.answer is not None:
            asyncio.get_running_loop().call_soon(appliance_control.resolve, self.answer.pop("_from", "appl-a"),
                                                 {**self.answer, "type": "door_unlock_result", "request_id": message["request_id"]})


@pytest.fixture()
def channel_loop(monkeypatch):
    import talk_audio_relay
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    appliance_control.bind_loop(loop)
    monkeypatch.setattr(talk_audio_relay, "_appliance_channels", {})
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    yield talk_audio_relay._appliance_channels
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)


def test_portal_unlock_reaches_the_appliance_and_returns_its_answer(db, channel_loop):
    channel_loop["appl-a"] = _Channel({"status": "ok", "channel": 1, "activated": True, "dry_run": False})
    with connection() as conn:
        camera = dict(conn.execute("SELECT * FROM cameras WHERE id='lobby'").fetchone())
    result = door_access.dispatch_manual_unlock(camera, actor="owner@example.test", pulse_ms=4000)
    assert result.activated and result.channel == 1
    sent = channel_loop["appl-a"].sent[0]
    assert (sent["type"], sent["camera_id"], sent["actor"]) == ("door_unlock", "lobby", "owner@example.test")
    assert db["provider"].calls == []  # nothing fired in the cloud itself


def test_portal_unlock_fails_closed_when_the_appliance_is_not_connected(db, channel_loop):
    with connection() as conn:
        camera = dict(conn.execute("SELECT * FROM cameras WHERE id='lobby'").fetchone())
    with pytest.raises(door_access.DoorNotReachable, match="not connected"):
        door_access.dispatch_manual_unlock(camera, actor="owner@example.test", pulse_ms=None)


def test_another_appliances_answer_is_never_accepted(db, channel_loop, monkeypatch):
    channel_loop["appl-a"] = _Channel({"status": "ok", "channel": 1, "activated": True, "_from": "appl-evil"})
    with connection() as conn:
        camera = dict(conn.execute("SELECT * FROM cameras WHERE id='lobby'").fetchone())
    monkeypatch.setattr(appliance_control, "request", lambda appliance_id, message, timeout=10.0: _real_request(appliance_id, message, timeout=1.0))
    with pytest.raises(door_access.DoorNotReachable, match="did not answer"):
        door_access.dispatch_manual_unlock(camera, actor="owner@example.test", pulse_ms=None)


_real_request = appliance_control.request


def test_the_appliance_opens_its_door_through_trigger_door(db):
    import talk_audio_relay_client
    answer = talk_audio_relay_client._door_unlock_on_appliance({"camera_id": "lobby", "actor": "owner@example.test", "pulse_ms": 4000})
    assert answer["status"] == "ok" and answer["activated"] is True and answer["channel"] == 1
    assert talk_audio_relay_client._door_unlock_on_appliance({"camera_id": "yard"})["reason"] == "not_a_door_here"


def test_a_revoke_asks_every_connected_appliance_to_resync_now(db, channel_loop):
    channel_loop["appl-a"] = _Channel(None)
    assert appliance_control.notify_facial_directory_changed("cust-a") == 1
    import time
    for _ in range(50):
        if channel_loop["appl-a"].sent:
            break
        time.sleep(0.02)
    assert channel_loop["appl-a"].sent == [{"type": "facial_directory_changed"}]
