"""Route-level tests for the manual "Unlock Door" button (POST
/api/customer/cameras/{camera_id}/door/unlock) and the shared
door_access_events audit trail, per the user's own explicit Face
Access requirements (docs/aac-face-access-door-control-requirements.md).

Same established pattern as test_admin_initiated_password_reset_route.py:
pull the real route function off main.app.routes and call it directly,
monkeypatching partner_identity on the module it was imported into
(door_access) and relay_control.get_provider() to a fresh, isolated
MockRelayProvider per test."""
from datetime import datetime

import pytest
from fastapi import HTTPException

import door_access
import main
import relay_control
from database_backend import override_target
from partner_db import connection, initialize_database, row, rows


def _route(path, method="POST"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request():
    from types import SimpleNamespace
    return SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"), url=SimpleNamespace(scheme="https", netloc="portal.example.test"))


def _owner_identity(email="owner-a@example.test", customer_id="customer-a"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": email}


def _viewer_identity(email="viewer-a@example.test", customer_id="customer-a"):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_door_access.db"


@pytest.fixture(autouse=True)
def _isolated_relay(monkeypatch):
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    yield provider
    relay_control.reset_provider()


def _seed(db_path, *, door_enabled=True, relay_channel=1, pulse_ms=2500):
    now = datetime.now().isoformat()
    with override_target(sqlite_path=db_path):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','Partner A','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-a','partner-a','Customer A','owner-a@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-a','customer-a','Site A',?)", (now,))
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES('owner-a','partner-a','owner-a@example.test','Owner A','customer_owner','x',1,'customer-a',?,'active')",
                (now,),
            )
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES('viewer-a','partner-a','viewer-a@example.test','Viewer A','customer_viewer','x',1,'customer-a',?,'active')",
                (now,),
            )
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
                "VALUES('camera-front-door','customer-a','site-a','Front Door','configured',1,?,?,?,?)",
                (1 if door_enabled else 0, relay_channel, pulse_ms, now),
            )
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('camera-plain','customer-a','site-a','Plain Camera','configured',2,?)",
                (now,),
            )


def test_unlock_succeeds_for_customer_owner_and_activates_the_configured_relay(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        result = unlock(_fake_request(), "camera-front-door")
        calls = _isolated_relay.calls
    assert result["door_name"] == "Front Door"
    assert result["channel"] == 1
    assert len(calls) == 1
    assert calls[0].channel == 1
    assert calls[0].pulse_ms == 2500
    assert calls[0].dry_run is False


def test_unlock_writes_a_successful_audit_row(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        unlock(_fake_request(), "camera-front-door")
        entry = row("SELECT * FROM door_access_events WHERE camera_id='camera-front-door'")
    assert entry is not None
    assert entry["trigger_type"] == "manual"
    assert entry["door_name"] == "Front Door"
    assert entry["relay_channel"] == 1
    assert entry["actor_email"] == "owner-a@example.test"
    assert entry["authorization_result"] == "authorized"
    assert entry["relay_result"] == "activated"
    assert entry["success"] == 1


def test_viewer_without_can_unlock_grant_is_denied_and_relay_never_fires(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _viewer_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-front-door")
    assert excinfo.value.status_code == 403
    assert _isolated_relay.calls == []


def test_viewer_with_explicit_can_unlock_grant_succeeds(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute(
                "INSERT INTO customer_camera_permissions(user_id,camera_id,can_alerts,can_settings,can_talk,can_unlock) VALUES('viewer-a','camera-front-door',1,0,0,1)"
            )
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _viewer_identity())
        result = unlock(_fake_request(), "camera-front-door")
    assert result["door_name"] == "Front Door"


def test_denied_viewer_attempt_is_still_audited(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _viewer_identity())
        with pytest.raises(HTTPException):
            unlock(_fake_request(), "camera-front-door")
        entries = rows("SELECT * FROM door_access_events WHERE camera_id='camera-front-door'")
    assert len(entries) == 1
    assert entries[0]["authorization_result"] == "denied"
    assert entries[0]["success"] == 0


def test_camera_not_configured_for_door_access_is_rejected(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-plain")
    assert excinfo.value.status_code == 404
    assert _isolated_relay.calls == []


def test_unknown_camera_id_is_a_plain_404_with_zero_mutation(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-does-not-exist")
        entries = rows("SELECT * FROM door_access_events")
    assert excinfo.value.status_code == 404
    assert entries == []


def test_cross_tenant_camera_is_indistinguishable_from_unknown(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    now = datetime.now().isoformat()
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-b','Partner B','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-b','partner-b','Customer B','owner-b@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-b','customer-b','Site B',?)", (now,))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,created_at) VALUES('camera-other','customer-b','site-b','Other Door','configured',1,1,1,?)", (now,))
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-other")
    assert excinfo.value.status_code == 404


def test_door_with_no_relay_channel_configured_fails_closed(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    now = datetime.now().isoformat()
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,created_at) "
                "VALUES('camera-no-relay','customer-a','site-a','No Relay Door','configured',3,1,NULL,?)",
                (now,),
            )
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-no-relay")
    assert excinfo.value.status_code == 409
    assert _isolated_relay.calls == []


def test_unauthenticated_request_is_denied_with_zero_mutation(monkeypatch, db_path, _isolated_relay):
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: None)
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-front-door")
        entries = rows("SELECT * FROM door_access_events")
    assert excinfo.value.status_code == 403
    assert entries == []


def test_cooldown_suppressed_second_press_is_a_409_and_audited_as_not_activated(monkeypatch, db_path):
    """Not the same isolated-fresh-provider fixture -- this test needs a
    REAL cooldown window, so it builds its own provider with a real
    cooldown_seconds instead of the autouse fixture's cooldown_seconds=0."""
    provider = relay_control.MockRelayProvider(cooldown_seconds=30.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    unlock = _route("/api/customer/cameras/{camera_id}/door/unlock")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        unlock(_fake_request(), "camera-front-door")
        with pytest.raises(HTTPException) as excinfo:
            unlock(_fake_request(), "camera-front-door")
        entries = rows("SELECT * FROM door_access_events WHERE camera_id='camera-front-door' ORDER BY created_at")
    assert excinfo.value.status_code == 409
    assert len(entries) == 2
    assert entries[1]["relay_result"] == "suppressed"
    assert entries[1]["success"] == 0
    relay_control.reset_provider()


def test_door_camera_helper_returns_none_for_a_non_door_camera(db_path):
    _seed(db_path, door_enabled=False)
    with override_target(sqlite_path=db_path):
        with connection() as db:
            assert door_access.door_camera(db, customer_id="customer-a", camera_id="camera-front-door") is None


def test_door_camera_helper_returns_the_row_for_a_real_door(db_path):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        with connection() as db:
            camera = door_access.door_camera(db, customer_id="customer-a", camera_id="camera-front-door")
    assert camera is not None
    assert camera["name"] == "Front Door"
    assert camera["door_relay_channel"] == 1


# --------------------------------------------------------------- door-config


def test_get_door_config_returns_current_state(monkeypatch, db_path):
    get_config = _route("/api/customer/cameras/{camera_id}/door-config", method="GET")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        result = get_config(_fake_request(), "camera-front-door")
    assert result["door_access_enabled"] == 1
    assert result["door_relay_channel"] == 1
    assert result["door_relay_pulse_ms"] == 2500


def test_get_door_config_unknown_camera_is_404(monkeypatch, db_path):
    get_config = _route("/api/customer/cameras/{camera_id}/door-config", method="GET")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            get_config(_fake_request(), "camera-does-not-exist")
    assert excinfo.value.status_code == 404


def test_owner_can_enable_door_access_on_a_plain_camera(monkeypatch, db_path):
    update_config = _route("/api/customer/cameras/{camera_id}/door-config", method="POST")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        result = update_config(_fake_request(), "camera-plain", {"door_access_enabled": True, "door_relay_channel": 2, "door_relay_pulse_ms": 4000})
        camera = row("SELECT door_access_enabled,door_relay_channel,door_relay_pulse_ms FROM cameras WHERE id='camera-plain'")
    assert result["door_access_enabled"] is True
    assert camera["door_access_enabled"] == 1
    assert camera["door_relay_channel"] == 2
    assert camera["door_relay_pulse_ms"] == 4000


def test_enabling_door_access_without_a_valid_channel_is_rejected(monkeypatch, db_path):
    update_config = _route("/api/customer/cameras/{camera_id}/door-config", method="POST")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(HTTPException) as excinfo:
            update_config(_fake_request(), "camera-plain", {"door_access_enabled": True, "door_relay_channel": 99})
        camera = row("SELECT door_access_enabled FROM cameras WHERE id='camera-plain'")
    assert excinfo.value.status_code == 400
    assert camera["door_access_enabled"] in (0, None)


def test_disabling_door_access_clears_relay_fields(monkeypatch, db_path):
    update_config = _route("/api/customer/cameras/{camera_id}/door-config", method="POST")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        update_config(_fake_request(), "camera-front-door", {"door_access_enabled": False})
        camera = row("SELECT door_access_enabled,door_relay_channel,door_relay_pulse_ms FROM cameras WHERE id='camera-front-door'")
    assert camera["door_access_enabled"] == 0
    assert camera["door_relay_channel"] is None
    assert camera["door_relay_pulse_ms"] is None


def test_viewer_cannot_configure_door_access_even_with_can_unlock(monkeypatch, db_path):
    """can_unlock only ever grants pressing the button on an already-
    configured door -- never configuring one in the first place."""
    update_config = _route("/api/customer/cameras/{camera_id}/door-config", method="POST")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        with connection() as db:
            db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_unlock) VALUES('viewer-a','camera-plain',1)")
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _viewer_identity())
        with pytest.raises(HTTPException) as excinfo:
            update_config(_fake_request(), "camera-plain", {"door_access_enabled": True, "door_relay_channel": 1})
    assert excinfo.value.status_code == 403


def test_door_config_change_is_audited(monkeypatch, db_path):
    update_config = _route("/api/customer/cameras/{camera_id}/door-config", method="POST")
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(door_access, "partner_identity", lambda request: _owner_identity())
        update_config(_fake_request(), "camera-plain", {"door_access_enabled": True, "door_relay_channel": 3, "door_relay_pulse_ms": 3500})
        entry = row("SELECT * FROM audit_logs WHERE action='camera.door_access_configured' AND entity_id='camera-plain'")
    assert entry is not None
    assert entry["actor_email"] == "owner-a@example.test"
    assert '"door_relay_channel": 3' in entry["details_json"]
