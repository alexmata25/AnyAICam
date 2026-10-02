"""No relay hardware yet (2026-10-01): the production relay provider is a
simulator, so every customer-facing unlock path must say nothing was
unlocked and the door history must record 'simulated' -- never
"Front Door unlocked." or a successful unlock."""
from datetime import datetime

import pytest

import aaco
import door_access
import main
import relay_control
from database_backend import override_target
from partner_db import connection

from test_aaco_unlock_door import _owner_identity, _request, _seed, db_path  # noqa: F401


@pytest.fixture(autouse=True)
def _production_provider(monkeypatch):
    relay_control.reset_provider()
    yield relay_control.get_provider()
    relay_control.reset_provider()


def _last_audit(db_path):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            return dict(db.execute("SELECT * FROM door_access_events ORDER BY created_at DESC LIMIT 1").fetchone())


def test_the_production_provider_marks_every_pulse_as_simulated(_production_provider):
    assert _production_provider.capability()["hardware_connected"] is False
    result = _production_provider.trigger(relay_control.RelayRequest(channel=1, pulse_ms=500, reason="t", dry_run=False, requested_by="t"))
    assert result.simulated is True
    again = _production_provider.trigger(relay_control.RelayRequest(channel=1, pulse_ms=500, reason="t", dry_run=False, requested_by="t"))
    assert again.simulated is True  # a cooldown answer is not "just unlocked" either


def test_a_mock_standing_in_for_hardware_is_not_simulated():
    result = relay_control.MockRelayProvider(cooldown_seconds=0.0).trigger(
        relay_control.RelayRequest(channel=1, pulse_ms=500, reason="t", dry_run=False, requested_by="t"))
    assert result.activated and not result.simulated


def test_aaco_unlock_says_nothing_was_unlocked_and_audits_simulated(db_path):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        result = aaco.execute(aaco.AacoCommand("unlock_door", camera_id="camera-name:front door"),
                              identity=_owner_identity(), vms=boundary)
    text = getattr(result, "message", None) or getattr(result, "question", None) or str(result)
    assert "nothing was unlocked" in text and "Front Door unlocked." not in text
    row = _last_audit(db_path)
    assert row["relay_result"] == "simulated" and row["success"] == 0


def test_the_audit_word_for_each_result():
    R = relay_control.RelayResult
    assert door_access.audit_relay_result(R(channel=1, activated=True, dry_run=False, simulated=True)) == "simulated"
    assert door_access.audit_relay_result(R(channel=1, activated=True, dry_run=False)) == "activated"
    assert door_access.audit_relay_result(R(channel=1, activated=False, dry_run=False, suppressed_reason="cooldown")) == "suppressed"


def test_cloud_dispatch_carries_the_appliance_simulated_flag(monkeypatch):
    import appliance_control
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(appliance_control, "connected", lambda appliance_id: True)
    monkeypatch.setattr(appliance_control, "request", lambda appliance_id, message, **kw: {
        "status": "ok", "channel": 1, "activated": True, "dry_run": False, "suppressed_reason": None, "simulated": True})
    result = door_access.dispatch_manual_unlock({"id": "cam", "appliance_id": "app-1"}, actor="o", pulse_ms=None)
    assert result.simulated is True


def test_appliance_answer_includes_simulated(monkeypatch):
    import talk_audio_relay_client
    monkeypatch.setattr(door_access, "trigger_door", lambda camera, **kw: relay_control.RelayResult(
        channel=1, activated=True, dry_run=False, simulated=True))

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            class _Row:
                def fetchone(self_inner):
                    return {"id": "cam", "door_access_enabled": 1}
            return _Row()

    import partner_db
    monkeypatch.setattr(partner_db, "connection", lambda: _Db())
    answer = talk_audio_relay_client._door_unlock_on_appliance({"camera_id": "cam"})
    assert answer["status"] == "ok" and answer["simulated"] is True


def test_the_live_unlock_button_says_nothing_was_unlocked(monkeypatch, db_path):
    from fastapi import HTTPException

    from test_door_access import _fake_request, _route
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-front-door")
        # a second press is not "just unlocked, wait a moment" either
        with pytest.raises(HTTPException) as again:
            unlock(_fake_request(), "camera-front-door")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "No door relay is connected to Front Door yet, so nothing was unlocked."
    assert again.value.detail == excinfo.value.detail
    row = _last_audit(db_path)
    assert row["relay_result"] == "simulated" and row["success"] == 0

