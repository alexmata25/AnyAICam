from datetime import datetime
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aaco_web import register_aaco_routes


class ControlledVms:
    def __init__(self):
        self.calls = []

    def authorized_camera(self, identity, camera_id):
        self.calls.append(("authorized_camera", identity["customer_id"], camera_id))
        return {"id": camera_id} if identity["customer_id"] == "tenant-a" and camera_id in {"camera-4", "camera-name:front entrance"} else None

    def live_view(self, identity, camera_id):
        self.calls.append(("live", identity["customer_id"], camera_id))
        return {"kind": "live", "message": "Live ready", "href": "/customer/cameras/opaque/live"}

    def playback(self, identity, camera_id, start, end):
        self.calls.append(("playback", identity["customer_id"], camera_id, start, end))
        return {"kind": "playback", "message": "Existing recording metadata", "href": "/playback?camera=opaque", "context": {"camera_id": camera_id, "playback_at": start.isoformat()}}

    def search_events(self, identity, **kwargs):
        self.calls.append(("events", identity["customer_id"], kwargs))
        context = {"camera_id": "camera-4", "event_at": "2026-09-15T10:00:00"}
        return {"kind": "events", "message": "One authorized result", "events": [{"label": "Camera 4: Person", "timestamp": "2026-09-15T10:00:00", "href": "/investigate", "context": context}], "context": context}

    def previous_event(self, identity, camera_id, before):
        self.calls.append(("previous-event", identity["customer_id"], camera_id, before))
        return {"kind": "events", "message": "Previous authorized event", "events": []}

    def camera_status(self, identity):
        self.calls.append(("status", identity["customer_id"]))
        return {"kind": "status", "message": "One camera", "cameras": [{"label": "Camera 4", "state": "online"}]}


def _client(identity={"role": "customer_owner", "customer_id": "tenant-a"}):
    app, vms = FastAPI(), ControlledVms()
    register_aaco_routes(
        app,
        lambda title, *_args: f"<html><title>{title}</title>{_args[1] if len(_args) > 1 else _args[0]}</html>",
        identity_provider=lambda _request: identity,
        vms_factory=lambda _request: vms,
        now=lambda: datetime(2026, 9, 15, 12),
    )
    return TestClient(app), vms


def test_page_is_shell_only_and_does_not_call_vms():
    client, vms = _client()
    response = client.get("/aaco")
    assert response.status_code == 200
    assert "AACO loads VMS data only after a command" in response.text
    assert "/api/aaco/command" in response.text
    assert "Show the front entrance" in response.text
    assert vms.calls == []
    assert "/api/customer/clips" not in response.text


def test_live_playback_events_status_and_context_reach_only_controlled_boundary():
    client, vms = _client()
    live = client.post("/api/aaco/command", json={"command": "Show Camera 4"})
    assert live.status_code == 200 and live.json()["kind"] == "live"
    playback = client.post("/api/aaco/command", json={"command": "Show Camera 4 yesterday at 3:30 PM"})
    assert playback.status_code == 200 and playback.json()["kind"] == "playback"
    events = client.post("/api/aaco/command", json={"command": "Show person events from the last 2 hours"})
    assert events.status_code == 200 and events.json()["kind"] == "events"
    assert "clips" not in str(vms.calls).lower()
    status = client.post("/api/aaco/command", json={"command": "Which cameras are offline?"})
    assert status.status_code == 200 and status.json()["kind"] == "status"
    context = {"camera_id": "camera-4", "playback_at": "2026-09-15T12:00:00"}
    back = client.post("/api/aaco/command", json={"command": "Go back twenty minutes", "context": context})
    assert back.status_code == 200 and back.json()["kind"] == "playback"
    assert any(call[0] == "live" for call in vms.calls)
    assert sum(call[0] == "playback" for call in vms.calls) == 2


def test_named_camera_previous_event_and_return_live_use_only_contextual_operations():
    client, vms = _client()
    named = client.post("/api/aaco/command", json={"command": "Show the front entrance"})
    assert named.status_code == 200 and named.json()["kind"] == "live"
    context = {"camera_id": "camera-4", "event_at": "2026-09-15T10:00:00"}
    previous = client.post("/api/aaco/command", json={"command": "Show previous event", "context": context})
    assert previous.status_code == 200 and previous.json()["kind"] == "events"
    live = client.post("/api/aaco/command", json={"command": "Return to live", "context": {"camera_id": "camera-4"}})
    assert live.status_code == 200 and live.json()["kind"] == "live"
    assert any(call[0] == "previous-event" for call in vms.calls)


def test_ambiguous_destructive_and_malformed_commands_fail_closed():
    client, vms = _client()
    assert client.post("/api/aaco/command", json={"command": "Show the camera"}).json()["kind"] == "clarification"
    assert client.post("/api/aaco/command", json={"command": "Delete every recording"}).json()["kind"] == "clarification"
    assert client.post("/api/aaco/command", json={"command": "Show Camera 4", "operation": "live_view"}).status_code == 400
    assert client.post("/api/aaco/command", content=b"not-json", headers={"content-type": "application/json"}).status_code == 400
    assert not [call for call in vms.calls if call[0] in {"live", "playback", "events", "status"}]


def test_unauthenticated_and_cross_tenant_requests_are_denied():
    unauthenticated, _ = _client(None)
    assert unauthenticated.get("/aaco").status_code == 403
    assert unauthenticated.post("/api/aaco/command", json={"command": "Show Camera 4"}).status_code == 403
    foreign, vms = _client({"role": "customer_owner", "customer_id": "tenant-b"})
    assert foreign.post("/api/aaco/command", json={"command": "Show Camera 4"}).status_code == 403
    assert not [call for call in vms.calls if call[0] == "live"]
