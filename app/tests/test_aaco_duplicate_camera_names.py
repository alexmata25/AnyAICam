"""Two cameras with the same display name are ambiguous to AACO
(2026-10-02, Codex review).

_find_camera() used to return the FIRST exact name match, so "show the
front door" with two cameras called Front Door silently opened whichever
came first -- for Live, Playback and event search. Now an identical-name
match asks which one, naming each by camera number, and a camera number
still resolves directly. Door unlock already refused to guess; its
question now names the cameras the same way.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import aaco
import main
from database_backend import override_target
from partner_db import initialize_database
from test_aaco_classic_boundary_integration import _owner_identity, _request, _seed_base_tenant, _seed_camera, db_path  # noqa: F401


@pytest.fixture()
def twins(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-1", "Front Door", 1)
        _seed_camera(conn, "cam-4", "front  door", 4)  # same name once case/spacing are normalized
        _seed_camera(conn, "cam-2", "Driveway", 2)
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        yield conn
        conn.close()


def _run(operation, camera_token, **extra):
    now = datetime.now(timezone.utc)
    if operation in ("playback", "event_search"):
        extra.setdefault("start", now - timedelta(hours=1))
        extra.setdefault("end", now)
    return aaco.execute(aaco.AacoCommand(operation, camera_id=camera_token, **extra),
                        identity=_owner_identity(), vms=main._ClassicAacoBoundary(_request()))


@pytest.mark.parametrize("operation", ["live_view", "playback", "event_search"])
def test_an_identical_name_asks_which_camera_instead_of_picking_one(twins, operation):
    result = _run(operation, "camera-name:front door")
    assert isinstance(result, aaco.Clarification), result
    assert "Front Door (Camera 1)" in result.message and "(Camera 4)" in result.message


def test_a_camera_number_still_resolves_directly(twins):
    result = _run("live_view", "camera-4")
    assert result["kind"] == "live" and "cam-4" in result["href"]


def test_a_unique_name_is_unaffected(twins):
    result = _run("live_view", "camera-name:driveway")
    assert result["kind"] == "live" and "cam-2" in result["href"]


def test_find_camera_never_returns_one_of_two_identical_names():
    cameras = [{"id": "a", "name": "Gate", "camera_number": 1}, {"id": "b", "name": "gate", "camera_number": 2}]
    assert main._ClassicAacoBoundary._find_camera(cameras, "camera-name:gate") is None
    assert main._ClassicAacoBoundary._resolve_camera(cameras, "camera-name:gate") is None


def test_two_doors_with_one_name_ask_which_and_never_pulse(db_path, monkeypatch):
    import relay_control
    from test_aaco_unlock_door import _owner_identity as door_owner, _seed
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    _seed(db_path, duplicate_door_name=True)
    with override_target(sqlite_path=db_path):
        result = aaco.execute(aaco.AacoCommand("unlock_door", camera_id="camera-name:front door"),
                              identity=door_owner(), vms=main._ClassicAacoBoundary(_request()))
    relay_control.reset_provider()
    assert isinstance(result, aaco.Clarification)
    assert "(Camera 1)" in result.message and "(Camera 3)" in result.message
    assert provider.calls == []
