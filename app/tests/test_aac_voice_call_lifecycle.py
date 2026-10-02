"""Visitor Call lifecycle (2026-10-02, Codex review), clock-controlled.

Unanswered calls never expired and an answered call stayed 'answered'
forever after a closed tab or lost connection; a second detection while a
call was ringing created a second call. Now: unanswered calls become
'missed' after RING_TIMEOUT_SECONDS; an answered call with no call-page
heartbeat for STALE_ANSWERED_SECONDS becomes 'ended' (a refresh or quick
reconnect resumes it); one live call per camera (a new trigger joins it, or
supersedes it once it has expired); duplicate Answer/End are harmless.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

import aac_voice_call
import aac_voice_call_events as store
from database_backend import override_target
from test_aac_voice_call_household_authorization import DOOR, OWNER, tenant  # noqa: F401 -- fixtures
from test_aac_voice_call_door_unlock import _isolated_relay, client, db_path  # noqa: F401 -- fixtures


class Clock:
    def __init__(self):
        self.now = datetime.now()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(store, "_now", c)
    return c


@pytest.fixture()
def call(db_path, clock):
    """A fresh ringing call on DOOR (created on the controlled clock)."""
    from test_aac_voice_call_door_unlock import _seed_tenant_with_door
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id=DOOR, partner_id="partner-a",
                           owner_email="owner@example.test", fleet_size=2)
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-a", camera_id=DOOR, enabled=True)
        return aac_voice_call.trigger_visitor_event(customer_id="cust-a", camera_id=DOOR, transcript_text="hello")["event_id"]


def _event(client, event_id):
    return client.get(f"/api/customer/aac/voice-call/events/{event_id}", cookies=OWNER).json()


def _post(client, event_id, action):
    return client.post(f"/api/customer/aac/voice-call/events/{event_id}/{action}", cookies=OWNER, json={} if action == "answer" else None)


# ------------------------------------------------------------------ unanswered timeout

def test_an_unanswered_call_rings_until_the_timeout_then_is_missed(client, call, clock):
    clock.advance(store.RING_TIMEOUT_SECONDS - 1)
    assert _event(client, call)["state"] == "notified"
    clock.advance(2)
    event = _event(client, call)
    assert event["state"] == "missed" and event["end_reason"] == "unanswered_timeout"
    late = _post(client, call, "answer")  # too late: harmless, never reopens
    assert late.status_code == 200 and late.json()["state"] == "missed"
    assert _event(client, call)["state"] == "missed" and not _event(client, call)["answered"]


# ------------------------------------------------------------------ refresh / disconnect / reconnect

def test_heartbeats_keep_an_answered_call_alive_and_silence_ends_it(client, call, clock):
    assert _post(client, call, "answer").json()["state"] == "answered"
    for _ in range(4):  # a call longer than the stale window, kept alive by the page
        clock.advance(store.HEARTBEAT_SECONDS)
        assert _post(client, call, "heartbeat").json()["state"] == "answered"
    clock.advance(store.STALE_ANSWERED_SECONDS + 1)  # tab closed / connection lost
    event = _event(client, call)
    assert event["state"] == "ended" and event["end_reason"] == "connection_lost" and event["call_ended_at"]
    assert _post(client, call, "heartbeat").json()["state"] == "ended"  # a late page learns it ended


def test_a_refresh_or_short_reconnect_resumes_the_same_call(client, call, clock):
    _post(client, call, "answer")
    clock.advance(store.STALE_ANSWERED_SECONDS - 5)  # page reloading, phone briefly offline
    page = client.get(f"/aac/voice-call/{call}", cookies=OWNER).text
    assert "const callInitialState='answered'" in page  # the page resumes heartbeats
    assert _post(client, call, "heartbeat").json()["state"] == "answered"
    clock.advance(store.STALE_ANSWERED_SECONDS - 5)
    assert _event(client, call)["state"] == "answered"


# ------------------------------------------------------------------ overlapping calls

def test_a_second_trigger_while_ringing_joins_the_same_call(client, db_path, call, clock):
    clock.advance(10)
    with override_target(sqlite_path=str(db_path)):
        again = aac_voice_call.trigger_visitor_event(customer_id="cust-a", camera_id=DOOR, transcript_text="hello again")
        detected = aac_voice_call.handle_person_detected(customer_id="cust-a", camera_id=DOOR, cooldown_seconds=0.0)
        edge = aac_voice_call.ingest_edge_visitor_event(customer_id="cust-a", camera_id=DOOR, detection_event_id="det-2",
                                                        event_timestamp=clock.now.isoformat(), now=clock.now)
    assert again["event_id"] == call and again["joined_active_call"] and again["notifications_created"] == 0
    assert detected == {"triggered": False, "skipped_reason": "call_in_progress", "event_id": call}
    assert edge == {"status": "joined_active_call", "event_id": call}
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM aac_voice_call_events WHERE camera_id=?", (DOOR,)).fetchone()[0] == 1
    conn.close()


def test_an_answered_call_also_blocks_a_second_ring(client, db_path, call, clock):
    _post(client, call, "answer")
    for _ in range(6):  # 90 s -- well past the ring timeout -- with the page heartbeating
        clock.advance(store.HEARTBEAT_SECONDS)
        assert _post(client, call, "heartbeat").json()["state"] == "answered"
    with override_target(sqlite_path=str(db_path)):
        again = aac_voice_call.trigger_visitor_event(customer_id="cust-a", camera_id=DOOR, transcript_text="still here")
    assert again["event_id"] == call


def test_a_new_visitor_after_expiry_gets_a_new_call_that_supersedes_the_old(client, db_path, call, clock):
    clock.advance(store.RING_TIMEOUT_SECONDS + 1)
    with override_target(sqlite_path=str(db_path)):
        fresh = aac_voice_call.trigger_visitor_event(customer_id="cust-a", camera_id=DOOR, transcript_text="hi")
    assert fresh["event_id"] != call and not fresh.get("joined_active_call")
    old = _event(client, call)
    assert old["state"] == "missed" and old["superseded_by"] == fresh["event_id"]
    assert _event(client, fresh["event_id"])["state"] == "notified"


def test_an_edge_utterance_from_a_joined_trigger_reaches_the_live_call(client, db_path, call, clock, monkeypatch):
    seen = []
    monkeypatch.setattr(aac_voice_call, "record_visitor_utterance", lambda **kwargs: seen.append(kwargs) or {"ok": True})
    with override_target(sqlite_path=str(db_path)):
        aac_voice_call.ingest_edge_visitor_event(customer_id="cust-a", camera_id=DOOR, detection_event_id="det-joined",
                                                 event_timestamp=clock.now.isoformat(), now=clock.now)
        outcome = aac_voice_call.ingest_edge_visitor_utterance(customer_id="cust-a", camera_id=DOOR,
                                                               trigger_detection_event_id="det-joined", transcript_text="parcel")
    assert outcome["status"] == "accepted" and outcome["event_id"] == call and seen[0]["event_id"] == call


# ------------------------------------------------------------------ duplicate Answer / End

def test_duplicate_answer_and_end_are_harmless(client, call, clock):
    first = _post(client, call, "answer").json()
    answered_at = _event(client, call)["answered_at"]
    clock.advance(3)
    second = _post(client, call, "answer").json()
    assert first["state"] == second["state"] == "answered" and "already answered" in second["message"]
    assert _event(client, call)["answered_at"] == answered_at  # timing never re-stamped
    assert _post(client, call, "end").json()["message"] == "Call ended."
    ended_at = _event(client, call)["call_ended_at"]
    clock.advance(3)
    again = _post(client, call, "end")
    assert again.status_code == 200 and "already ended" in again.json()["message"]
    assert _event(client, call)["call_ended_at"] == ended_at


def test_ending_a_missed_call_keeps_it_missed(client, call, clock):
    clock.advance(store.RING_TIMEOUT_SECONDS + 1)
    response = _post(client, call, "end")
    assert response.status_code == 200 and _event(client, call)["state"] == "missed"


def test_the_call_page_says_why_a_call_closed(client, call, clock):
    clock.advance(store.RING_TIMEOUT_SECONDS + 1)
    assert "Missed call: nobody answered in time." in client.get(f"/aac/voice-call/{call}", cookies=OWNER).text
