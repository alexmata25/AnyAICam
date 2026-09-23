"""AAC Voice Call, Phase 1 -- foundation, intent classification, and the
first vertical slice: configured entrance camera -> simulated trigger ->
visitor intent classification -> AAC Voice Call event created -> customer
notification appears -> customer can open the associated call screen.

Same lightweight standalone-app fixture idiom as test_facial_recognition_
ui.py: a fresh FastAPI() app with only this feature's own routes
registered (not the full main.app), a real SQLite database via
override_target()/initialize_database() (so notification_engine.py's own
real fan-out logic, camera_access.py, and every real table this feature
touches all run for real, not mocked).

Camera-count-agnostic throughout: fleets of 2, 6, and 13 cameras (never
the 5-camera Ryzen pilot number), with only a subset of each fleet
configured as AAC Voice Call entrance cameras -- proving participation is
driven by explicit per-camera configuration, never a fixed count or range.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_aac_voice_call_foundation_import.db"):
    import aac_voice_call
    import aac_voice_call_events as store
    import partner_portal
    from partner_db import connection, initialize_database, row

from aac_voice_call_intent import (
    DELIVERY,
    GREETING,
    MAINTENANCE,
    PRESENCE_CHECK,
    UNKNOWN,
    VISITOR,
    DeterministicVisitorIntentClassifier,
)

NOW = "2026-09-23T00:00:00"


def _shell(title, active, content, scripts=""):
    return f"<html><title>{title}</title>{content}{scripts}</html>"


# ------------------------------------------------------------- fixtures


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aac_voice_call_foundation.db"


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


def _seed_customer_with_cameras(conn, customer_id, camera_count, *, partner_id="partner-1", owner_email="owner@example.test"):
    """Seeds a full tenant (partner, customer, site, appliance,
    `camera_count` cameras, and an approved customer_owner partner_user
    row -- required for notification_engine.fanout_appliance_event()'s
    own recipient query, which reads partner_users directly)."""
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", NOW))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", NOW),
    )
    site_id = f"site-{customer_id}"
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
    camera_ids = []
    for n in range(1, camera_count + 1):
        camera_id = f"cam-{customer_id}-{n}"
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at) VALUES(?,?,?,?,?,?,?)",
            (camera_id, customer_id, site_id, appliance_id, f"Camera {n}", "configured", NOW),
        )
        camera_ids.append(camera_id)
    return {"site_id": site_id, "appliance_id": appliance_id, "camera_ids": camera_ids}


def _seed(db_path, customer_id, camera_count, **kwargs):
    conn = sqlite3.connect(db_path)
    info = _seed_customer_with_cameras(conn, customer_id, camera_count, **kwargs)
    conn.commit()
    conn.close()
    return info


# --------------------------------------------------- intent classifier


@pytest.mark.parametrize(
    "transcript,expected_intent",
    [
        ("I have a delivery for you", DELIVERY),
        ("got a package for you", DELIVERY),
        ("I'm the maintenance technician", MAINTENANCE),
        ("here to fix the furnace", MAINTENANCE),
        ("is anybody home", PRESENCE_CHECK),
        ("Is anyone there?", PRESENCE_CHECK),
        ("hello!", GREETING),
        ("hi there", GREETING),
        ("I'm a visitor here to see John", VISITOR),
        ("here for the meeting", VISITOR),
        ("asdkfjasdf random noise", UNKNOWN),
        ("", UNKNOWN),
    ],
)
def test_intent_classifier_recognizes_phrase_variants_not_just_exact_matches(transcript, expected_intent):
    classifier = DeterministicVisitorIntentClassifier()
    result = classifier.classify(transcript)
    assert result.intent == expected_intent
    if expected_intent == UNKNOWN:
        assert result.confidence == 0.0
    else:
        assert result.confidence > 0.0


def test_more_specific_intents_win_over_the_generic_visitor_catch_all():
    classifier = DeterministicVisitorIntentClassifier()
    result = classifier.classify("maintenance visitor here for the AC unit")
    assert result.intent == MAINTENANCE


# --------------------------------------------------------- entrance cameras


@pytest.mark.parametrize("camera_count", [2, 6, 13])
def test_only_explicitly_configured_cameras_participate(db_path, camera_count):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    info = _seed(db_path, "cust-1", camera_count)
    with override_target(sqlite_path=str(db_path)):
        for camera_id in info["camera_ids"]:
            assert store.is_entrance_camera("cust-1", camera_id) is False
        store.set_entrance_camera(customer_id="cust-1", camera_id=info["camera_ids"][0], enabled=True)
        assert store.is_entrance_camera("cust-1", info["camera_ids"][0]) is True
        for camera_id in info["camera_ids"][1:]:
            assert store.is_entrance_camera("cust-1", camera_id) is False


def test_disabling_an_entrance_camera_stops_participation(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    info = _seed(db_path, "cust-1", 3)
    with override_target(sqlite_path=str(db_path)):
        camera_id = info["camera_ids"][0]
        store.set_entrance_camera(customer_id="cust-1", camera_id=camera_id, enabled=True)
        assert store.is_entrance_camera("cust-1", camera_id) is True
        store.set_entrance_camera(customer_id="cust-1", camera_id=camera_id, enabled=False)
        assert store.is_entrance_camera("cust-1", camera_id) is False


# ----------------------------------------------- the first vertical slice


@pytest.mark.parametrize("camera_count", [2, 6, 13])
def test_full_vertical_slice_trigger_to_notification_to_call_screen(client, db_path, camera_count):
    info = _seed(db_path, "cust-1", camera_count)
    entrance_camera_id = info["camera_ids"][0]
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-1", camera_id=entrance_camera_id, enabled=True)

    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}

    # 1. Trigger: simulated person/audio event with a transcript.
    trigger_response = client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": entrance_camera_id, "transcript_text": "I have a delivery for you"},
        cookies=cookies,
    )
    assert trigger_response.status_code == 200
    body = trigger_response.json()
    assert body["intent"] == DELIVERY
    assert body["notifications_created"] >= 1
    event_id = body["event_id"]

    # 2. AAC Voice Call event was really created, with the full field set.
    with override_target(sqlite_path=str(db_path)):
        event = store.get_voice_call_event(event_id=event_id, customer_id="cust-1")
    assert event is not None
    assert event["camera_id"] == entrance_camera_id
    assert event["customer_id"] == "cust-1"
    assert event["intent"] == DELIVERY
    assert event["transcript_text"] == "I have a delivery for you"
    assert event["state"] == "notified"
    assert event["answered"] == 0

    # 3. A real customer notification appears (notifications table, not
    # a fake/mocked send) -- the exact existing mechanism every other
    # customer-facing alert already uses.
    with override_target(sqlite_path=str(db_path)):
        notification = row(
            "SELECT * FROM notifications WHERE event_id=? AND event_type='aac_voice_call'", (event_id,)
        )
    assert notification is not None
    assert notification["camera_id"] == entrance_camera_id
    assert "delivery" in notification["message"].lower()

    # 4. Customer can open the associated call screen.
    screen_response = client.get(f"/aac/voice-call/{event_id}", cookies=cookies)
    assert screen_response.status_code == 200
    html = screen_response.text
    assert "delivery" in html.lower()
    assert f"/customer/cameras/{entrance_camera_id}/live" in html
    assert "Answer" in html
    assert "End call" in html

    # 5. Answer, then end the call.
    answer_response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/answer", cookies=cookies)
    assert answer_response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        answered_event = store.get_voice_call_event(event_id=event_id, customer_id="cust-1")
    assert answered_event["answered"] == 1
    assert answered_event["answered_at"] is not None
    assert answered_event["call_started_at"] is not None
    assert answered_event["state"] == "answered"

    end_response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/end", cookies=cookies)
    assert end_response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        ended_event = store.get_voice_call_event(event_id=event_id, customer_id="cust-1")
    assert ended_event["state"] == "ended"
    assert ended_event["call_ended_at"] is not None


def test_trigger_on_a_non_entrance_camera_is_rejected(client, db_path):
    """The core "only explicitly configured cameras participate" rule,
    enforced at the actual trigger route, not just the data layer."""
    info = _seed(db_path, "cust-1", 4)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": info["camera_ids"][0], "transcript_text": "hello"},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert "entrance camera" in response.json()["detail"].lower()


def test_unrecognized_transcript_still_creates_an_event_and_notification(client, db_path):
    """UNKNOWN intent is a real, expected outcome -- a person triggered
    the entrance camera and said something; the homeowner should still
    be notified even without a confidently classified intent."""
    info = _seed(db_path, "cust-1", 3)
    entrance_camera_id = info["camera_ids"][0]
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-1", camera_id=entrance_camera_id, enabled=True)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": entrance_camera_id, "transcript_text": "mumble mumble static"},
        cookies=cookies,
    )
    assert response.status_code == 200
    assert response.json()["intent"] == UNKNOWN
    assert response.json()["notifications_created"] >= 1


# ------------------------------------------------------ tenant isolation


def test_customer_b_cannot_trigger_customer_as_entrance_camera(client, db_path):
    info_a = _seed(db_path, "cust-a", 3)
    _seed(db_path, "cust-b", 3, partner_id="partner-1")
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-a", camera_id=info_a["camera_ids"][0], enabled=True)
    cookies_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b")}
    response = client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": info_a["camera_ids"][0], "transcript_text": "hello"},
        cookies=cookies_b,
    )
    assert response.status_code == 404


def test_customer_b_cannot_read_customer_as_voice_call_event(client, db_path):
    info_a = _seed(db_path, "cust-a", 3)
    _seed(db_path, "cust-b", 3)
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-a", camera_id=info_a["camera_ids"][0], enabled=True)
    cookies_a = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    cookies_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b")}
    trigger = client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": info_a["camera_ids"][0], "transcript_text": "hello"},
        cookies=cookies_a,
    )
    event_id = trigger.json()["event_id"]

    assert client.get(f"/api/customer/aac/voice-call/events/{event_id}", cookies=cookies_b).status_code == 404
    assert client.get(f"/aac/voice-call/{event_id}", cookies=cookies_b).status_code == 404
    assert client.post(f"/api/customer/aac/voice-call/events/{event_id}/answer", cookies=cookies_b).status_code == 404
    assert client.post(f"/api/customer/aac/voice-call/events/{event_id}/end", cookies=cookies_b).status_code == 404


def test_customer_b_never_receives_customer_as_notification(client, db_path):
    info_a = _seed(db_path, "cust-a", 3, owner_email="owner-a@example.test")
    _seed(db_path, "cust-b", 3, owner_email="owner-b@example.test")
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-a", camera_id=info_a["camera_ids"][0], enabled=True)
    cookies_a = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", email="owner-a@example.test")}
    client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": info_a["camera_ids"][0], "transcript_text": "hello"},
        cookies=cookies_a,
    )
    with override_target(sqlite_path=str(db_path)):
        cust_b_notifications = [
            item for item in (row("SELECT customer_id FROM notifications WHERE customer_id='cust-b'") or [])
        ]
    assert cust_b_notifications == []


def test_viewer_can_use_but_not_configure_entrance_cameras(client, db_path):
    info = _seed(db_path, "cust-1", 3)
    owner_cookie = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    viewer_cookie = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-1")}
    camera_id = info["camera_ids"][0]

    # Viewer cannot configure.
    response = client.post(f"/api/customer/aac/voice-call/entrance-cameras/{camera_id}", cookies=viewer_cookie)
    assert response.status_code == 403

    # Owner configures it.
    response = client.post(f"/api/customer/aac/voice-call/entrance-cameras/{camera_id}", cookies=owner_cookie)
    assert response.status_code == 200

    # Viewer can still trigger/use the now-configured camera.
    response = client.post(
        "/api/customer/aac/voice-call/simulate-trigger",
        json={"camera_id": camera_id, "transcript_text": "hello"},
        cookies=viewer_cookie,
    )
    assert response.status_code == 200


# ------------------------------------------------- door unlock interface


def test_request_door_unlock_is_a_prepared_interface_not_an_implementation():
    """Phase 5 explicitly not implemented -- calling it must fail loudly
    (NotImplementedError), never silently pretend to unlock a door."""
    with pytest.raises(NotImplementedError):
        aac_voice_call.request_door_unlock(customer_id="cust-1", camera_id="cam-1", event_id="evt-1", requested_by="owner@example.test")
