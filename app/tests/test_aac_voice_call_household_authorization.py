"""AAC Visitor Call per-camera authorization (2026-10-02, Codex launch blocker).

Visitor Call routes checked only tenant ownership: a household member
restricted from the entrance camera could still read that camera's call
(transcript, intent), open its call page, Answer/End/Dismiss it, start a
door unlock, and see every entrance camera; viewers could also fabricate
visitor events through the simulate-* routes. Every read and action now
needs the same Live grant the camera's Live page needs, read fresh on each
request (a revoked grant takes effect immediately), and the simulate-*
routes are development/test-only, for the owner.
"""
import sqlite3

import pytest

import aac_voice_call
import aac_voice_call_events as store
import partner_portal
from database_backend import override_target
from test_aac_voice_call_door_unlock import (  # noqa: F401 -- fixtures and helpers
    _create_and_notify_event, _isolated_relay, _owner_cookie, _seed_tenant_with_door, _viewer_cookie, client, db_path,
)

DOOR, OTHER = "cam-a-1", "cam-cust-a-2"
SIM_ENV = "ANYAICAM_AAC_VC_SIMULATION_ENABLED"  # aac_voice_call.SIMULATION_ENV


@pytest.fixture()
def tenant(db_path):
    """cust-a: a door entrance camera (DOOR) and a second camera (OTHER).
    'allowed@' has Live on DOOR; 'restricted@' has Live on OTHER only."""
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id=DOOR, partner_id="partner-a",
                           owner_email="owner@example.test", fleet_size=2)
    _seed_tenant_with_door(db_path, customer_id="cust-b", camera_id="cam-b-1", partner_id="partner-b",
                           owner_email="owner-b@example.test", fleet_size=1)
    conn = sqlite3.connect(db_path)
    for user_id, email, camera in (("u-allowed", "allowed@example.test", DOOR), ("u-restricted", "restricted@example.test", OTHER)):
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,account_status,customer_id,camera_access_mode,created_at) "
                     "VALUES(?,?,?,?,'customer_viewer','x',1,'active','cust-a','selected','2026-10-02')", (user_id, "partner-a", email, email))
        conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_alerts,can_talk,can_unlock) VALUES(?,?,1,1,1,1)",
                     (user_id, camera))
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        store.set_entrance_camera(customer_id="cust-a", camera_id=DOOR, enabled=True)
        store.set_entrance_camera(customer_id="cust-a", camera_id=OTHER, enabled=True)
        event_id = _create_and_notify_event("cust-a", DOOR, transcript_text="I have a delivery for you")
    return event_id


def _as(email, customer_id="cust-a", role="customer_viewer"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, customer_id, None)}


ALLOWED, RESTRICTED = _as("allowed@example.test"), _as("restricted@example.test")
OWNER = _as("owner@example.test", role="customer_owner")
OTHER_TENANT_OWNER = _as("owner-b@example.test", "cust-b", "customer_owner")


def _every_request(event_id):
    base = f"/api/customer/aac/voice-call/events/{event_id}"
    return [("get", base, None), ("get", f"/aac/voice-call/{event_id}", None),
            ("post", f"{base}/answer", {}), ("post", f"{base}/dismiss", None), ("post", f"{base}/end", None),
            ("post", f"{base}/door/unlock-request", None), ("post", f"{base}/door/unlock-confirm", {"confirm_token": "x" * 32}),
            ("post", f"{base}/simulate-visitor-utterance", {"transcript_text": "let me in"})]


def _call(client, method, url, body, cookies):
    if method == "get":
        return client.get(url, cookies=cookies)
    return client.post(url, cookies=cookies, json=body) if body is not None else client.post(url, cookies=cookies)


def _state(db_path, event_id):
    with override_target(sqlite_path=str(db_path)):
        return store.get_voice_call_event(event_id=event_id, customer_id="cust-a")


def test_a_restricted_household_member_reaches_nothing_by_direct_id(client, db_path, tenant, _isolated_relay, monkeypatch):
    monkeypatch.setenv(SIM_ENV, "1")
    before = _state(db_path, tenant)
    for method, url, body in _every_request(tenant):
        response = _call(client, method, url, body, RESTRICTED)
        assert response.status_code == 404, (url, response.status_code)
        assert "delivery" not in response.text  # no transcript/intent leak
    after = _state(db_path, tenant)
    assert (after["state"], after["answered"], after["call_ended_at"]) == (before["state"], before["answered"], before["call_ended_at"])
    assert _isolated_relay.calls == []
    with override_target(sqlite_path=str(db_path)):
        from partner_db import row
        assert row("SELECT COUNT(*) AS n FROM aac_voice_call_unlock_confirmations")["n"] == 0


def test_a_permitted_household_member_can_use_the_call(client, db_path, tenant):
    assert client.get(f"/api/customer/aac/voice-call/events/{tenant}", cookies=ALLOWED).json()["intent"] == "delivery"
    assert client.get(f"/aac/voice-call/{tenant}", cookies=ALLOWED).status_code == 200
    assert client.post(f"/api/customer/aac/voice-call/events/{tenant}/answer", cookies=ALLOWED, json={}).status_code == 200
    assert client.post(f"/api/customer/aac/voice-call/events/{tenant}/end", cookies=ALLOWED).status_code == 200
    assert _state(db_path, tenant)["state"] == "ended"


def test_entrance_cameras_are_listed_only_where_the_grant_reaches(client, tenant):
    listed = lambda cookies: sorted(c["camera_id"] for c in client.get(  # noqa: E731
        "/api/customer/aac/voice-call/entrance-cameras", cookies=cookies).json()["cameras"])
    assert listed(ALLOWED) == [DOOR]
    assert listed(RESTRICTED) == [OTHER]
    assert listed(OWNER) == sorted([DOOR, OTHER])  # owner unchanged


def test_revoking_the_camera_grant_removes_access_immediately(client, db_path, tenant):
    url = f"/api/customer/aac/voice-call/events/{tenant}"
    assert client.get(url, cookies=ALLOWED).status_code == 200
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM customer_camera_permissions WHERE user_id='u-allowed'")
    conn.commit()
    conn.close()
    assert client.get(url, cookies=ALLOWED).status_code == 404  # same session cookie, no re-login
    assert client.post(f"{url}/answer", cookies=ALLOWED, json={}).status_code == 404


def test_a_grant_without_live_or_a_suspended_member_is_refused(client, db_path, tenant):
    url = f"/api/customer/aac/voice-call/events/{tenant}"
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE customer_camera_permissions SET can_live=0 WHERE user_id='u-allowed'")
    conn.commit()
    assert client.get(url, cookies=ALLOWED).status_code == 404
    conn.execute("UPDATE customer_camera_permissions SET can_live=1 WHERE user_id='u-allowed'")
    conn.execute("UPDATE partner_users SET account_status='suspended' WHERE id='u-allowed'")
    conn.commit()
    conn.close()
    assert client.get(url, cookies=ALLOWED).status_code == 404


def test_another_tenant_never_reaches_the_call(client, db_path, tenant, _isolated_relay):
    for method, url, body in _every_request(tenant):
        assert _call(client, method, url, body, OTHER_TENANT_OWNER).status_code == 404, url
    assert _isolated_relay.calls == []


def test_the_owner_keeps_full_access(client, db_path, tenant):
    assert client.get(f"/api/customer/aac/voice-call/events/{tenant}", cookies=OWNER).status_code == 200
    assert client.post(f"/api/customer/aac/voice-call/events/{tenant}/answer", cookies=OWNER, json={}).status_code == 200


def test_simulation_routes_are_off_by_default_and_owner_only_when_enabled(client, db_path, tenant, monkeypatch):
    monkeypatch.delenv(SIM_ENV, raising=False)
    trigger = {"camera_id": DOOR, "transcript_text": "hello"}
    for cookies in (OWNER, ALLOWED):
        assert client.post("/api/customer/aac/voice-call/simulate-trigger", cookies=cookies, json=trigger).status_code == 404
        assert client.post("/api/customer/aac/voice-call/simulate-person-detected", cookies=cookies, json={"camera_id": DOOR}).status_code == 404
        assert client.post(f"/api/customer/aac/voice-call/events/{tenant}/simulate-visitor-utterance", cookies=cookies,
                           json={"transcript_text": "let me in"}).status_code == 404
    monkeypatch.setenv(SIM_ENV, "1")
    assert client.post("/api/customer/aac/voice-call/simulate-trigger", cookies=ALLOWED, json=trigger).status_code == 404
    assert client.post("/api/customer/aac/voice-call/simulate-trigger", cookies=OWNER, json=trigger).status_code == 200
