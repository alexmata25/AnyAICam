"""AACO door unlock asks first and acts once (2026-10-02, Codex review).

The unlock command used to pulse the relay as soon as its gates passed;
a replayed or double-submitted request could pulse it again, and the AACO
panel said the assistant "never changes or deletes anything". Now the
command only resolves and authorizes the door and returns a one-use
confirmation (hashed, 60 s, bound to the person and the door); the relay
fires only when that token is consumed, after settings, camera scope and
the door permission are checked again.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aaco_settings
import aaco_web
import main
from database_backend import override_target
from partner_db import connection
from test_aaco_unlock_door import _audit_rows, _isolated_relay, _owner_identity, _seed, _viewer_identity, db_path  # noqa: F401


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, 12, 0, 0)

    def __call__(self):
        return self.now


@pytest.fixture()
def site(db_path, monkeypatch):
    _seed(db_path)
    state = {"identity": _owner_identity(), "settings": dict(aaco_settings.DEFAULTS, allow_door_actions=True)}
    monkeypatch.setattr(aaco_settings, "load", lambda _customer_id: dict(state["settings"]))
    clock = Clock()
    app = FastAPI()
    aaco_web.register_aaco_routes(app, lambda title, *args: f"<html>{args[-1] if args else ''}</html>",
                                  identity_provider=lambda _request: state["identity"],
                                  vms_factory=lambda request: main._ClassicAacoBoundary(request), now=clock)
    with override_target(sqlite_path=db_path), TestClient(app) as client:
        yield client, state, clock


def _ask(client, text="Open Front Door"):
    return client.post("/api/aaco/command", json={"command": text})


def _confirm(client, token):
    return client.post("/api/aaco/door-unlock/confirm", json={"confirm_token": token})


def test_the_command_asks_and_does_nothing_physical(site, db_path, _isolated_relay):
    client, _, _ = site
    asked = _ask(client).json()
    assert asked["kind"] == "confirm_door_unlock" and asked["door_name"] == "Front Door"
    assert "physically unlocks" in asked["message"] and asked["confirm_token"]
    assert _isolated_relay.calls == []
    assert not [r for r in _audit_rows(db_path, "camera-front-door") if r["success"]]
    with connection() as db:
        stored = [dict(r) for r in db.execute("SELECT * FROM aaco_door_unlock_confirmations").fetchall()]
    assert len(stored) == 1 and asked["confirm_token"] not in str(stored)  # only the hash is kept


def test_confirming_unlocks_once_and_a_replay_does_nothing(site, db_path, _isolated_relay):
    client, _, _ = site
    token = _ask(client).json()["confirm_token"]
    first = _confirm(client, token)
    assert first.status_code == 200 and first.json()["message"] == "Front Door unlocked."
    assert len(_isolated_relay.calls) == 1
    for _ in range(3):  # double-tap, retry, replayed request
        again = _confirm(client, token)
        assert again.status_code == 409 and "already used" in again.json()["detail"]
    assert len(_isolated_relay.calls) == 1
    assert len([r for r in _audit_rows(db_path, "camera-front-door") if r["success"]]) == 1


def test_two_commands_need_two_confirmations(site, _isolated_relay):
    client, _, _ = site
    tokens = [_ask(client).json()["confirm_token"] for _ in range(2)]
    assert tokens[0] != tokens[1] and _isolated_relay.calls == []
    assert _confirm(client, tokens[0]).status_code == 200
    assert len(_isolated_relay.calls) == 1


def test_an_expired_confirmation_does_nothing(site, _isolated_relay):
    client, _, clock = site
    token = _ask(client).json()["confirm_token"]
    clock.now += timedelta(seconds=61)
    assert _confirm(client, token).status_code == 409
    assert _isolated_relay.calls == []


def test_someone_elses_confirmation_does_nothing(site, _isolated_relay):
    client, state, _ = site
    token = _ask(client).json()["confirm_token"]
    state["identity"] = dict(_owner_identity(), email="other-owner@example.test")
    assert _confirm(client, token).status_code == 409
    state["identity"] = _owner_identity(customer_id="customer-b", email="owner-a@example.test")
    assert _confirm(client, token).status_code == 409
    assert _isolated_relay.calls == []


def test_settings_turned_off_after_asking_stop_the_unlock(site, _isolated_relay):
    client, state, _ = site
    token = _ask(client).json()["confirm_token"]
    state["settings"]["allow_door_actions"] = False
    response = _confirm(client, token)
    assert response.status_code == 200 and response.json()["kind"] == "clarification"
    assert _isolated_relay.calls == []


def test_a_permission_lost_after_asking_is_checked_again(site, db_path, _isolated_relay):
    client, state, _ = site
    state["identity"] = _viewer_identity()
    with connection() as db:
        db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_unlock) VALUES('viewer-a','camera-front-door',1)")
    token = _ask(client).json()["confirm_token"]
    with connection() as db:
        db.execute("UPDATE customer_camera_permissions SET can_unlock=0 WHERE user_id='viewer-a'")
    assert _confirm(client, token).status_code == 403
    assert _isolated_relay.calls == []


def test_an_excluded_camera_is_refused_before_any_confirmation(site, _isolated_relay):
    client, state, _ = site
    state["settings"].update(camera_scope="selected", camera_ids=["camera-plain"])
    asked = _ask(client, "Open Front Door").json()
    assert asked["kind"] == "clarification" and asked["message"] == aaco_settings.CAMERA_NOT_ALLOWED_MESSAGE
    # Any other way of naming it is refused too (no token issued either way);
    # every token form is covered at the boundary in test_aaco_unlock_scope.py.
    assert _ask(client, "Unlock camera 1").status_code in (200, 403)
    with connection() as db:
        assert db.execute("SELECT COUNT(*) FROM aaco_door_unlock_confirmations").fetchone()[0] == 0
    assert _isolated_relay.calls == []


def test_a_forged_or_malformed_token_does_nothing(site, _isolated_relay):
    client, _, _ = site
    for token in ("", "x" * 43, "a" * 500, None, 7):
        assert _confirm(client, token).status_code == 409
    assert client.post("/api/aaco/door-unlock/confirm", json={"confirm_token": "x", "door": "camera-1"}).status_code == 400
    assert _isolated_relay.calls == []


def test_the_panel_tells_the_truth_about_physical_actions(site):
    client, _, _ = site
    page = client.get("/aaco").text
    assert "never changes or deletes anything" not in page
    assert "Door unlock is the one physical action" in page and "confirm first" in page
    assert "aacoConfirmDoorUnlock" in page and "/api/aaco/door-unlock/confirm" in page
