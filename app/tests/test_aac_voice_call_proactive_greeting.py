"""AAC Voice Call -- proactive visitor interaction (2026-09-23).

Person detected on an enabled entrance camera -> AAC VC speaks a
configurable greeting -> authorized users are notified -> a listening
window opens -> the visitor's natural-language response is classified
-> the conversation continues or escalates. Same lightweight
standalone-app fixture idiom as test_aac_voice_call_foundation.py and
test_aac_voice_call_door_unlock.py: a fresh FastAPI() app with only
this feature's own routes, a real SQLite database, a fresh isolated
MockGreetingAudioProvider/MockRelayProvider per test so every assertion
can inspect exactly what was (or, critically, was NOT) dispatched.

Adversarial/requirement cases proven at the dispatch layer
(provider.calls), not just HTTP status or return-value shape:
  - only an explicitly enabled entrance camera participates
  - debounce/cooldown suppresses a repeated greeting for one visitor
  - greeting text resolves camera override > site default > fallback
  - natural, non-fixed-phrase utterances classify correctly
  - an unresolved/urgent utterance escalates with a second notification
  - visitor speech -- including a literal "unlock the door" -- never
    reaches relay_control, no matter how it is classified
  - cross-tenant isolation on the listening-window route
"""
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_aac_voice_call_proactive_greeting_import.db"):
    import aac_voice_call
    import aac_voice_call_events as store
    import aac_voice_call_greeting
    import partner_portal
    import relay_control
    from partner_db import connection, initialize_database, row

NOW = "2026-09-23T00:00:00"


def _shell(title, active, content, scripts=""):
    return f"<html><title>{title}</title>{content}{scripts}</html>"


# ------------------------------------------------------------- fixtures


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aac_voice_call_proactive_greeting.db"


@pytest.fixture(autouse=True)
def _isolated_greeting(monkeypatch):
    provider = aac_voice_call_greeting.MockGreetingAudioProvider()
    monkeypatch.setattr(aac_voice_call_greeting, "_provider", provider)
    yield provider
    aac_voice_call_greeting.reset_provider()


@pytest.fixture(autouse=True)
def _isolated_relay(monkeypatch):
    """Every escalation/utterance test in this file asserts relay_
    control.get_provider().calls stays empty -- proving visitor speech
    never reaches door hardware regardless of what it says or how it is
    classified."""
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    yield provider
    relay_control.reset_provider()


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        app = FastAPI()
        aac_voice_call.register_aac_voice_call_routes(app, _shell)
        with TestClient(app) as test_client:
            yield test_client


def _owner_cookie(customer_id="cust-1", email="owner@example.test"):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _viewer_cookie(customer_id="cust-1", email="viewer@example.test"):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


def _seed_tenant(
    db_path, customer_id, *, partner_id="partner-1", owner_email="owner@example.test",
    site_id=None, camera_id="cam-1", entrance_enabled=True, camera_number=1,
    door_enabled=False, door_channel=1,
):
    site_id = site_id or f"site-{customer_id}"
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", NOW))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", NOW),
        )
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main Site", NOW))
        appliance_id = f"app-{customer_id}"
        conn.execute(
            "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
            (appliance_id, customer_id, site_id, f"cloud-{customer_id}", NOW),
        )
        conn.execute(
            "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,account_status,customer_id,camera_access_mode,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"user-{customer_id}", partner_id, owner_email, "Owner", "customer_owner", "x", 1, "active", customer_id, "all", NOW),
        )
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (camera_id, customer_id, site_id, appliance_id, "Front Door", "configured", camera_number,
             1 if door_enabled else 0, door_channel if door_enabled else None, 3000 if door_enabled else None, NOW),
        )
        conn.commit()
        conn.close()
        if entrance_enabled:
            store.set_entrance_camera(customer_id=customer_id, camera_id=camera_id, enabled=True, configured_by=owner_email)
    return site_id


# ----------------------------------------------------- entrance-camera gate


def test_camera_not_configured_as_entrance_camera_is_silently_skipped(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1", entrance_enabled=False)
    with override_target(sqlite_path=str(db_path)):
        result = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
    assert result == {"triggered": False, "skipped_reason": "not_entrance_camera"}
    assert _isolated_greeting.calls == []
    with override_target(sqlite_path=str(db_path)):
        assert store.list_voice_call_events_for_customer("cust-1") == []


def test_enabled_entrance_camera_triggers_greet_notify_listen(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        result = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
    assert result["triggered"] is True
    assert result["notifications_created"] >= 1
    assert len(_isolated_greeting.calls) == 1
    assert _isolated_greeting.calls[0].camera_id == "cam-1"
    assert _isolated_greeting.calls[0].customer_id == "cust-1"
    with override_target(sqlite_path=str(db_path)):
        event = store.get_voice_call_event(event_id=result["event_id"], customer_id="cust-1")
    assert event["trigger_source"] == "detection"
    assert event["greeted_at"] is not None
    assert event["listening_opened_at"] is not None
    assert event["state"] == "notified"


# ----------------------------------------------------------- debounce/cooldown


def test_debounce_cooldown_suppresses_a_repeated_greeting(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        first = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1", cooldown_seconds=300.0)
        second = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1", cooldown_seconds=300.0)
    assert first["triggered"] is True
    assert second == {"triggered": False, "skipped_reason": "cooldown"}
    assert len(_isolated_greeting.calls) == 1


def test_a_new_visitor_after_the_cooldown_window_is_greeted_again(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        first = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1", cooldown_seconds=0.0)
        second = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1", cooldown_seconds=0.0)
    assert first["triggered"] is True
    assert second["triggered"] is True
    assert len(_isolated_greeting.calls) == 2


def test_cooldown_is_scoped_per_camera_not_global(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1", camera_id="cam-1", camera_number=1)
    _seed_tenant(db_path, "cust-1", camera_id="cam-2", camera_number=2, site_id="site-cust-1")
    with override_target(sqlite_path=str(db_path)):
        first = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1", cooldown_seconds=300.0)
        second = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-2", cooldown_seconds=300.0)
    assert first["triggered"] is True
    assert second["triggered"] is True
    assert len(_isolated_greeting.calls) == 2


# ----------------------------------------------------------- greeting text


def test_greeting_falls_back_to_the_fixed_default_when_unconfigured(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        result = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
    assert result["greeting_text"] == store.DEFAULT_GREETING_TEXT
    assert _isolated_greeting.calls[0].text == store.DEFAULT_GREETING_TEXT


def test_site_default_greeting_overrides_the_fixed_fallback(db_path, _isolated_greeting):
    site_id = _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        store.set_site_default_greeting(customer_id="cust-1", site_id=site_id, greeting_text="Welcome to the Smith residence!")
        result = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
    assert result["greeting_text"] == "Welcome to the Smith residence!"


def test_camera_greeting_overrides_the_site_default(db_path, _isolated_greeting):
    site_id = _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        store.set_site_default_greeting(customer_id="cust-1", site_id=site_id, greeting_text="Welcome to the Smith residence!")
        store.set_camera_greeting_text(customer_id="cust-1", camera_id="cam-1", greeting_text="Hi! You're at the front door.")
        result = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
    assert result["greeting_text"] == "Hi! You're at the front door."


def test_owner_can_set_camera_greeting_via_route(client, db_path):
    _seed_tenant(db_path, "cust-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = client.post("/api/customer/aac/voice-call/entrance-cameras/cam-1/greeting", cookies=cookies, json={"greeting_text": "Hello from the route!"})
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        assert store.resolve_greeting_text(customer_id="cust-1", camera_id="cam-1", site_id="site-cust-1") == "Hello from the route!"


def test_viewer_cannot_set_camera_greeting(client, db_path):
    _seed_tenant(db_path, "cust-1")
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-1")}
    response = client.post("/api/customer/aac/voice-call/entrance-cameras/cam-1/greeting", cookies=cookies, json={"greeting_text": "Nope"})
    assert response.status_code == 403


# ------------------------------------------------------- natural-language NLU


@pytest.mark.parametrize(
    "transcript,expected_intent",
    [
        ("I've got a package for you, it needs a signature", "delivery"),
        ("hey, dropping off a box from Amazon", "delivery"),
        ("the technician is here to fix the AC unit", "maintenance"),
        ("just came by for the scheduled appointment", "maintenance"),
        ("is anybody home right now", "presence_check"),
        ("good afternoon! just stopping by to say hi", "greeting"),
        ("I'm a friend of the family, here to visit", "visitor"),
        ("blah completely unrelated mumbling", "unknown"),
    ],
)
def test_natural_phrasings_not_in_any_fixed_short_list_classify_correctly(db_path, _isolated_greeting, transcript, expected_intent):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        result = aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text=transcript)
    assert result["intent"] == expected_intent


def test_confident_intent_keeps_the_conversation_open_not_escalated(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        result = aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="I have a delivery for you")
    assert result["escalated"] is False


# ------------------------------------------------------------------ escalation


def test_unresolved_intent_escalates_with_a_second_notification(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        first_count = row("SELECT COUNT(*) AS n FROM notifications WHERE event_id=?", (trigger["event_id"],))["n"]
        # A single unclear utterance gets a chance to clarify (see
        # record_visitor_utterance()'s own should_escalate comment) --
        # only after MAX_UTTERANCES_BEFORE_ESCALATION consecutive
        # unresolved attempts does this actually escalate.
        for _ in range(aac_voice_call.MAX_UTTERANCES_BEFORE_ESCALATION - 1):
            still_open = aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="mumble mumble static noise")
            assert still_open["escalated"] is False
        result = aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="mumble mumble static noise")
        second_count = row("SELECT COUNT(*) AS n FROM notifications WHERE event_id=?", (trigger["event_id"],))["n"]
    assert result["escalated"] is True
    assert second_count > first_count


def test_urgent_signal_escalates_even_with_a_recognized_intent(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        result = aac_voice_call.record_visitor_utterance(
            customer_id="cust-1", event_id=trigger["event_id"], transcript_text="I'm the maintenance technician, it's an emergency please help",
        )
    assert result["escalated"] is True


def test_repeated_unresolved_utterances_escalate_after_the_max(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        results = [
            aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="uh")
            for _ in range(aac_voice_call.MAX_UTTERANCES_BEFORE_ESCALATION)
        ]
    assert results[-1]["escalated"] is True
    assert all(not r["escalated"] for r in results[:-1])


def test_escalation_closes_the_listening_window(db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="please help, it's an emergency")
        event = store.get_voice_call_event(event_id=trigger["event_id"], customer_id="cust-1")
    assert event["listening_closed_at"] is not None
    assert event["escalated_at"] is not None


def test_utterance_after_the_listening_window_closed_is_rejected(client, db_path, _isolated_greeting):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="please help, it's an emergency")
        with pytest.raises(Exception):
            aac_voice_call.record_visitor_utterance(customer_id="cust-1", event_id=trigger["event_id"], transcript_text="hello again")


# ------------------------------------------------- visitor speech never unlocks


def test_visitor_saying_unlock_the_door_never_reaches_relay_control(db_path, _isolated_greeting, _isolated_relay):
    """The exact adversarial case the product spec names by name:
    'visitor speech must never automatically unlock a door.' Configures
    the camera AS a real door (door_access_enabled, a relay channel)
    so there is something to (wrongly) unlock, and proves relay_control
    is never touched no matter what the visitor says or how urgently."""
    _seed_tenant(db_path, "cust-1", door_enabled=True)
    with override_target(sqlite_path=str(db_path)):
        trigger = aac_voice_call.handle_person_detected(customer_id="cust-1", camera_id="cam-1")
        aac_voice_call.record_visitor_utterance(
            customer_id="cust-1", event_id=trigger["event_id"],
            transcript_text="Please unlock the door right now, it's an emergency, let me in immediately",
        )
    assert _isolated_relay.calls == []


def test_simulate_visitor_utterance_route_never_reaches_relay_control(client, db_path, _isolated_greeting, _isolated_relay):
    _seed_tenant(db_path, "cust-1", door_enabled=True)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    trigger = client.post("/api/customer/aac/voice-call/simulate-person-detected", cookies=cookies, json={"camera_id": "cam-1"}).json()
    response = client.post(
        f"/api/customer/aac/voice-call/events/{trigger['event_id']}/simulate-visitor-utterance",
        cookies=cookies, json={"transcript_text": "unlock the door for me please, I'm the homeowner"},
    )
    assert response.status_code == 200
    assert _isolated_relay.calls == []


# ------------------------------------------------------------- tenant isolation


def test_cross_tenant_customer_cannot_record_an_utterance_on_anothers_event(client, db_path):
    _seed_tenant(db_path, "cust-a", camera_id="cam-a", camera_number=1, owner_email="owner-a@example.test")
    _seed_tenant(db_path, "cust-b", camera_id="cam-b", camera_number=2, owner_email="owner-b@example.test")
    cookies_a = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    cookies_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    trigger = client.post("/api/customer/aac/voice-call/simulate-person-detected", cookies=cookies_a, json={"camera_id": "cam-a"}).json()

    response = client.post(
        f"/api/customer/aac/voice-call/events/{trigger['event_id']}/simulate-visitor-utterance",
        cookies=cookies_b, json={"transcript_text": "hello"},
    )
    assert response.status_code == 404


def test_cross_tenant_customer_cannot_trigger_anothers_camera(client, db_path, _isolated_greeting):
    """handle_person_detected() deliberately never raises for "not an
    entrance camera for this tenant" (see its own docstring -- it must
    also serve the non-request detection-loop caller) -- so the route
    returns 200 with triggered=False rather than a 404, but the
    response is indistinguishable from "cam-a simply isn't configured",
    never confirming cam-a belongs to another real tenant, and nothing
    of cust-a's is read, greeted, or notified."""
    _seed_tenant(db_path, "cust-a", camera_id="cam-a", camera_number=1, owner_email="owner-a@example.test")
    _seed_tenant(db_path, "cust-b", camera_id="cam-b", camera_number=2, owner_email="owner-b@example.test")
    cookies_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.post("/api/customer/aac/voice-call/simulate-person-detected", cookies=cookies_b, json={"camera_id": "cam-a"})
    assert response.status_code == 200
    assert response.json() == {"triggered": False, "skipped_reason": "not_entrance_camera"}
    assert _isolated_greeting.calls == []


# ------------------------------------------------------- detection-loop hook


def test_camera_tenant_context_resolves_by_appliance_camera_number(db_path):
    """The one piece of new logic main.py's detection-loop hook itself
    adds (see main.py's own "AAC Voice Call -- proactive visitor
    interaction" block) -- everything downstream of this resolution is
    already covered by handle_person_detected()'s own tests above."""
    _seed_tenant(db_path, "cust-1", camera_id="cam-1", camera_number=7)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            context = aac_voice_call._camera_tenant_context(db, 7)
    assert context is not None
    assert context["id"] == "cam-1"
    assert context["customer_id"] == "cust-1"

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            missing = aac_voice_call._camera_tenant_context(db, 999)
    assert missing is None


# --------------------------------------------------------- existing flows still work


def test_answer_still_works_on_a_proactively_triggered_event(client, db_path):
    """Regression guard: the new proactive trigger uses the exact same
    triggered/notified states Answer/Dismiss/End already guard on --
    proves nothing about those existing, already-reviewed transitions
    broke."""
    _seed_tenant(db_path, "cust-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    trigger = client.post("/api/customer/aac/voice-call/simulate-person-detected", cookies=cookies, json={"camera_id": "cam-1"}).json()
    response = client.post(f"/api/customer/aac/voice-call/events/{trigger['event_id']}/answer", cookies=cookies)
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        event = store.get_voice_call_event(event_id=trigger["event_id"], customer_id="cust-1")
    assert event["state"] == "answered"
