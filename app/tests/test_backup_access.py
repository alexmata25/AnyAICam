"""Face Access Backup Mobile Access + audible door feedback (2026-10-01).

Software-only: no relay, strike or maglock is touched. Physical door
validation is deferred until hardware or a customer installation exists."""
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_household_users import _home, _invite, _join, _member_id, _q, _token, mail  # noqa: F401 -- fixtures
from test_pricing_ff_commission import OWNER, _cookie, db_path, portal  # noqa: F401 -- fixtures

PIN = "482916"
VIEWER = ("maria@example.test", "customer_viewer", "cust-1")


def _person(db_path, *, person_id="p-1", customer_id="cust-1", door="cam-3", enabled=1, starts=None, expires=None,
            days=None, start=None, end=None, grant=True):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO facial_people(id,customer_id,display_name,status,created_at,updated_at,access_enabled,"
                 "access_starts_on,access_expires_on) VALUES(?,?,?,'active','2026-01-01','2026-01-01',?,?,?)",
                 (person_id, customer_id, "Alex", enabled, starts, expires))
    if grant:
        conn.execute("INSERT INTO facial_rules(id,customer_id,camera_id,name,trigger_type,person_id,relay_channel,dry_run,enabled,"
                     "schedule_start,schedule_end,days_of_week,origin,created_at,updated_at) VALUES(?,?,?,'g','specific_person',?,1,0,1,?,?,?,"
                     "'cloud','2026-01-01','2026-01-01')", (f"rule-{person_id}-{door}", customer_id, door, person_id, start, end, days))
    conn.commit()
    conn.close()


@pytest.fixture()
def home(portal, db_path, mail):
    import backup_access
    backup_access._actor_limiter.events.clear()
    client, _, _ = portal
    _home(db_path)
    _person(db_path)
    return client


def _set_pin(client, pin=PIN, person="p-1", who=OWNER):
    return client.put(f"/api/aac/people/{person}/backup-pin", json={"pin": pin}, cookies=_cookie(*who))


def _unlock(client, pin=PIN, door="cam-3", person="p-1", who=OWNER):
    return client.post(f"/api/aac/people/{person}/backup-unlock", json={"camera_id": door, "pin": pin}, cookies=_cookie(*who))


def _audits(db_path):
    return _q(db_path, "SELECT authorization_result,relay_result,success,trigger_type,matched_person_id FROM door_access_events "
                       "WHERE trigger_type='backup_mobile' ORDER BY created_at")


def _household_member(client, db_path, mail, **permissions):
    _invite(client, permissions={"camera_ids": ["cam-1", "cam-3"], **permissions})
    _join(client, _token(mail))
    return _member_id(db_path)


# ------------------------------------------------------------------ PIN

@pytest.mark.parametrize("bad", ["12345", "12345678901", "12a456", "111111", "123456", "987654", ""])
def test_weak_or_malformed_pins_are_refused(home, bad):
    assert _set_pin(home, bad).status_code == 400


def test_a_pin_is_stored_hashed_and_never_shown(home, db_path):
    assert _set_pin(home).json()["message"] == "Backup Access PIN set."
    stored = _q(db_path, "SELECT pin_hash FROM facial_person_backup_pins WHERE person_id='p-1'")[0]["pin_hash"]
    assert PIN not in stored and len(stored) > 30
    status = home.get("/api/aac/people/p-1/backup-access", cookies=_cookie(*OWNER)).json()
    assert status["pin_set"] is True and PIN not in str(status)
    assert _set_pin(home, "730185").json()["message"] == "Backup Access PIN changed."
    assert home.delete("/api/aac/people/p-1/backup-pin", cookies=_cookie(*OWNER)).status_code == 200
    assert home.get("/api/aac/people/p-1/backup-access", cookies=_cookie(*OWNER)).json()["pin_set"] is False


def test_only_a_face_access_manager_sets_pins(home, db_path, mail):
    member = _household_member(home, db_path, mail, backup_access=True)
    assert _set_pin(home, who=VIEWER).status_code == 403
    assert home.delete("/api/aac/people/p-1/backup-pin", cookies=_cookie(*VIEWER)).status_code == 403
    home.put(f"/api/customer/household/users/{member}/permissions", json={"camera_ids": ["cam-3"], "face_access": True},
             cookies=_cookie(*OWNER))
    assert _set_pin(home, who=VIEWER).status_code == 200


# ------------------------------------------------------------------ who may unlock

def test_backup_unlock_needs_a_session_and_the_backup_grant(home, db_path, mail):
    _set_pin(home)
    assert home.post("/api/aac/people/p-1/backup-unlock", json={"camera_id": "cam-3", "pin": PIN}).status_code in (401, 403)
    member = _household_member(home, db_path, mail)
    denied = _unlock(home, who=VIEWER)
    assert denied.status_code == 403 and "Backup Mobile Access" in denied.json()["detail"]
    home.put(f"/api/customer/household/users/{member}/permissions", json={"camera_ids": ["cam-3"], "backup_access": True},
             cookies=_cookie(*OWNER))
    allowed = _unlock(home, who=VIEWER)
    assert allowed.status_code == 200 and allowed.json()["authorized"] is True
    # The backup grant reaches the People profile it lives on -- read only.
    assert home.get("/api/aac/people/p-1/backup-access", cookies=_cookie(*VIEWER)).json()["can_manage_pin"] is False


def test_another_customer_can_never_use_this_person_or_door(home, db_path):
    _set_pin(home)
    _home(db_path, customer_id="cust-2", email="other-owner@example.test", door_cameras=())
    other = ("other-owner@example.test", "customer_owner", "cust-2")
    assert _unlock(home, who=other).status_code == 404
    assert home.get("/api/aac/people/p-1/backup-access", cookies=_cookie(*other)).status_code == 404
    assert _set_pin(home, "730185", who=other).status_code == 404
    assert _audits(db_path) == []


# ------------------------------------------------------------------ PIN failures and lockout

def test_wrong_pins_are_audited_and_lock_the_person_out(home, db_path):
    _set_pin(home)
    for _ in range(4):
        wrong = _unlock(home, pin="000001")
        assert wrong.status_code == 403 and wrong.json()["detail"] == "That PIN is not correct."
    locked = _unlock(home, pin="000001")
    assert locked.status_code == 423
    assert _unlock(home).status_code == 423  # even the right PIN, until the pause ends
    assert [a["authorization_result"] for a in _audits(db_path)] == ["denied_pin"] * 4 + ["locked_out", "locked_out"]
    assert all(a["relay_result"] == "skipped" and not a["success"] for a in _audits(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE facial_person_backup_pins SET locked_until=?", ((datetime.now() - timedelta(seconds=1)).isoformat(),))
    conn.commit(); conn.close()
    assert _unlock(home).status_code == 200


def test_no_pin_set_is_refused(home, db_path):
    assert _unlock(home).status_code == 409
    assert _audits(db_path)[0]["authorization_result"] == "no_pin"


# ------------------------------------------------------------------ permission rules

@pytest.mark.parametrize("person", [
    {"enabled": 0},
    {"expires": "2000-01-01"},
    {"starts": "2999-01-01"},
    {"grant": False},
    {"days": "mon" if datetime.now().weekday() != 0 else "tue"},
    {"start": "00:00", "end": "00:01"} if datetime.now().strftime("%H:%M") > "00:02" else {"start": "23:58", "end": "23:59"},
])
def test_dates_days_hours_and_revocation_are_enforced(portal, db_path, mail, person):
    import backup_access
    backup_access._actor_limiter.events.clear()
    client, _, _ = portal
    _home(db_path)
    _person(db_path, **person)
    _set_pin(client)
    response = _unlock(client)
    assert response.status_code == 403
    assert _audits(db_path)[-1]["authorization_result"] == "denied_schedule"


def test_a_revoked_grant_stops_backup_access_immediately(home, db_path):
    _set_pin(home)
    assert _unlock(home).status_code == 200
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE facial_rules SET enabled=0 WHERE person_id='p-1'")
    conn.commit(); conn.close()
    assert _unlock(home).status_code == 403


# ------------------------------------------------------------------ honest results

def test_no_relay_configured_is_authorized_but_never_called_unlocked(home, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE cameras SET door_relay_channel=NULL WHERE id='cam-3'")
    conn.commit(); conn.close()
    _set_pin(home)
    result = _unlock(home).json()
    assert result == {"authorized": True, "unlocked": False, "status": "no_hardware",
                      "message": "Access authorized — no door hardware is configured for Camera 3. The door was not unlocked."}
    assert _audits(db_path)[-1]["relay_result"] == "no_hardware"
    notes = _q(db_path, "SELECT title,message FROM notifications WHERE event_type='backup_access'")
    assert notes and "Camera 3" in notes[0]["message"] and "was not unlocked" in notes[0]["message"]


def test_a_simulated_relay_is_reported_as_no_hardware(home, db_path):
    """Edge path: relay_control's provider is simulated in this build."""
    _set_pin(home)
    result = _unlock(home).json()
    assert result["unlocked"] is False and result["status"] == "no_hardware"
    assert _audits(db_path)[-1] == {"authorization_result": "authorized", "relay_result": "simulated", "success": 0,
                                    "trigger_type": "backup_mobile", "matched_person_id": "p-1"}


class _FakeChannel:
    def __init__(self, answer):
        self.answer, self.sent = answer, []


@pytest.fixture()
def cloud(monkeypatch):
    import appliance_control
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    channel = _FakeChannel(None)
    monkeypatch.setattr(appliance_control, "connected", lambda appliance_id: channel.answer is not None)

    def request(appliance_id, message, timeout=10.0):
        channel.sent.append(dict(message))
        return channel.answer
    monkeypatch.setattr(appliance_control, "request", request)
    return channel


def _attach_appliance(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE cameras SET appliance_id='app-1' WHERE id='cam-3'")
    conn.commit(); conn.close()


def test_appliance_offline_fails_closed(home, db_path, cloud):
    _attach_appliance(db_path)
    _set_pin(home)
    response = _unlock(home)
    assert response.status_code == 503 and "was not unlocked" in response.json()["detail"]
    assert _audits(db_path)[-1]["relay_result"] == "not_sent"
    assert _q(db_path, "SELECT status FROM backup_unlock_commands")[0]["status"] == "not_sent"


def test_an_accepted_command_is_single_use_and_carries_its_id(home, db_path, cloud):
    _attach_appliance(db_path)
    cloud.answer = {"status": "ok", "channel": 1, "activated": True, "simulated": False}
    _set_pin(home)
    result = _unlock(home).json()
    assert result["unlocked"] is True and "accepted" in result["message"] and "opened" not in result["message"]
    sent = cloud.sent[-1]
    assert sent["type"] == "door_unlock" and sent["trigger"] == "backup_mobile" and len(sent["command_id"]) == 32
    assert abs(sent["issued_at"] - time.time()) < 10
    ledger = _q(db_path, "SELECT id,status FROM backup_unlock_commands")
    assert ledger == [{"id": sent["command_id"], "status": "accepted"}]
    assert _audits(db_path)[-1]["relay_result"] == "activated" and _audits(db_path)[-1]["success"] == 1
    # A second unlock is a brand-new command, never the old one replayed.
    _unlock(home)
    assert cloud.sent[-1]["command_id"] != sent["command_id"]


def test_the_appliance_refuses_replayed_expired_and_malformed_commands(db_path):
    import backup_access
    from partner_db import connection
    good = {"command_id": "a" * 32, "issued_at": time.time(), "camera_id": "cam-3"}
    with override_target(sqlite_path=str(db_path)), connection() as db:
        assert backup_access.accept_command_on_appliance(db, good) is None
        assert backup_access.accept_command_on_appliance(db, good) == "replayed_command"
        assert backup_access.accept_command_on_appliance(db, {**good, "command_id": "b" * 32, "issued_at": time.time() - 600}) == "expired_command"
        assert backup_access.accept_command_on_appliance(db, {**good, "command_id": "../x"}) == "bad_command"
        assert backup_access.accept_command_on_appliance(db, {**good, "command_id": "c" * 32, "issued_at": "soon"}) == "bad_command"


def test_the_appliance_door_handler_refuses_a_replay_before_touching_the_door(db_path, monkeypatch):
    _home(db_path)
    import door_access
    import talk_audio_relay_client as client
    calls = []
    monkeypatch.setattr(door_access, "trigger_door", lambda *a, **k: calls.append(k) or None)
    message = {"type": "door_unlock", "camera_id": "cam-3", "trigger": "backup_mobile", "command_id": "d" * 32, "issued_at": time.time()}
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as db:
            import backup_access
            backup_access.accept_command_on_appliance(db, message)  # already seen once
        assert client._door_unlock_on_appliance(message) == {"status": "error", "reason": "replayed_command"}
    assert calls == []


def test_a_wrong_pin_never_issues_a_command(home, db_path, cloud):
    _attach_appliance(db_path)
    cloud.answer = {"status": "ok", "activated": True}
    _set_pin(home)
    _unlock(home, pin="000001")
    assert cloud.sent == [] and _q(db_path, "SELECT * FROM backup_unlock_commands") == []


# ------------------------------------------------------------------ audible feedback

def test_feedback_outcomes_distinguish_denied_granted_and_fault():
    import door_feedback as f
    assert f.outcome_for("authorized", "activated") == f.GRANTED
    for relay in ("simulated", "failed", "dry_run", "not_sent", "no_hardware"):
        assert f.outcome_for("authorized", relay) == f.FAULT
    for relay in ("suppressed", "cooldown"):
        assert f.outcome_for("authorized", relay) is None  # just unlocked; nothing to report
    for auth in ("not_authorized", "unknown_person", "denied_pin", "locked_out", "denied_schedule"):
        assert f.outcome_for(auth, "skipped") == f.DENIED
    tones = {o: f.tone_pcm(o) for o in (f.DENIED, f.GRANTED, f.FAULT)}
    assert len(set(tones.values())) == 3 and all(len(t) > 1000 and len(t) % 2 == 0 for t in tones.values())
    with pytest.raises(ValueError):
        f.tone_pcm("open sesame")


def test_feedback_plays_only_where_turned_on_and_audible(monkeypatch):
    import door_feedback as f
    played = []

    class _Provider:
        def speak(self, request):
            played.append(request)
            return type("R", (), {"delivered": True})()

    factory = lambda pcm, rate: (played.append(("pcm", len(pcm), rate)) or _Provider())  # noqa: E731
    door = {"id": "cam-x", "customer_id": "c", "door_feedback_enabled": 1, "talk_down_supported": 1}
    assert f.play({**door, "door_feedback_enabled": 0}, f.GRANTED, provider_factory=factory)["reason"] == "feedback_off"
    assert f.play({**door, "talk_down_supported": 0}, f.GRANTED, provider_factory=factory)["reason"] == "camera_has_no_speaker"
    assert f.play(door, f.GRANTED, provider_factory=factory) == {"played": True, "reason": None}
    assert played[0] == ("pcm", len(f.tone_pcm(f.GRANTED)), f.SAMPLE_RATE)
    f._last_denied.clear()
    clock = iter([100.0, 101.0, 112.0])
    assert f.play(door, f.DENIED, provider_factory=factory, clock=lambda: next(clock))["played"] is True
    assert f.play(door, f.DENIED, provider_factory=factory, clock=lambda: next(clock))["reason"] == "denied_cooldown"
    assert f.play(door, f.DENIED, provider_factory=factory, clock=lambda: next(clock))["played"] is True
    assert f.play(door, None)["reason"] == "no_outcome"


def test_door_feedback_setting_is_owner_controlled_and_off_with_the_door(home, db_path):
    assert home.get("/api/customer/cameras/cam-3/door-config", cookies=_cookie(*OWNER)).json()["door_feedback_enabled"] == 0
    saved = home.post("/api/customer/cameras/cam-3/door-config", cookies=_cookie(*OWNER),
                      json={"door_access_enabled": True, "door_relay_channel": 1, "door_feedback_enabled": True})
    assert saved.status_code == 200 and saved.json()["door_feedback_enabled"] is True
    off = home.post("/api/customer/cameras/cam-3/door-config", cookies=_cookie(*OWNER),
                    json={"door_access_enabled": False, "door_feedback_enabled": True})
    assert off.json()["door_feedback_enabled"] is False
    assert _q(db_path, "SELECT door_feedback_enabled FROM cameras WHERE id='cam-3'")[0]["door_feedback_enabled"] == 0
