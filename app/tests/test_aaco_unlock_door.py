"""Integration coverage for AACO's unlock_door command path --
main._ClassicAacoBoundary.unlock_door(), reached via aaco.execute()
exactly the way the real /api/aaco/command route reaches it
(register_aaco_routes() -> _ClassicAacoBoundary(request)).

Same established pattern as test_aaco_classic_boundary_integration.py
(real seeded SQLite, the real boundary class, not a fake -- identity is
passed straight into aaco.execute() the same way the real /api/aaco/
command route does after its own identity_provider resolves it, so no
partner_identity monkeypatch is needed here at all) combined with
test_door_access.py's own
_isolated_relay fixture (a fresh, isolated MockRelayProvider per test),
since unlock_door() deliberately reuses door_access.py's own
_authorized_door_camera()/record_door_access_event() and
relay_control.py's own get_provider()/RelayRequest -- the exact same
authorization, execution, and audit path the manual "Unlock Door"
button already uses and this codebase already trusts, never a second,
parallel mechanism."""
from datetime import datetime

import pytest

import aaco
import door_access
import main
import relay_control
from database_backend import override_target
from partner_db import connection, initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aaco_unlock_door.db"


@pytest.fixture(autouse=True)
def _isolated_relay(monkeypatch):
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    yield provider
    relay_control.reset_provider()


def _request():
    from types import SimpleNamespace
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def _owner_identity(customer_id="customer-a", email="owner-a@example.test"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": email}


def _viewer_identity(customer_id="customer-a", email="viewer-a@example.test"):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


def _seed(db_path, *, second_tenant=False, duplicate_door_name=False):
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
                "VALUES('camera-front-door','customer-a','site-a','Front Door','configured',1,1,1,2500,?)",
                (now,),
            )
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('camera-plain','customer-a','site-a','Plain Camera','configured',2,?)",
                (now,),
            )
            if duplicate_door_name:
                db.execute(
                    "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
                    "VALUES('camera-front-door-2','customer-a','site-a','Front Door','configured',3,1,2,2500,?)",
                    (now,),
                )
            if second_tenant:
                db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-b','Partner B','approved','real',?)", (now,))
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-b','partner-b','Customer B','owner-b@example.test','active','real',?)", (now,))
                db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-b','customer-b','Site B',?)", (now,))
                db.execute(
                    "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
                    "VALUES('camera-tenant-b-door','customer-b','site-b','Tenant B Door','configured',1,1,1,2500,?)",
                    (now,),
                )


def _audit_rows(db_path, camera_id=None):
    with override_target(sqlite_path=db_path):
        with connection() as db:
            if camera_id:
                return [dict(r) for r in db.execute("SELECT * FROM door_access_events WHERE camera_id=? ORDER BY created_at", (camera_id,)).fetchall()]
            return [dict(r) for r in db.execute("SELECT * FROM door_access_events ORDER BY created_at").fetchall()]


def test_successful_authorized_unlock_activates_the_real_relay_and_reports_the_pulse(monkeypatch, db_path, _isolated_relay):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:front door")
        result = aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert result["kind"] == "door_unlock"
    assert result["message"] == "Front Door unlocked."
    assert _isolated_relay.calls and _isolated_relay.calls[0].channel == 1
    assert _isolated_relay.calls[0].pulse_ms == 2500


def test_unauthorized_viewer_without_can_unlock_is_denied_and_relay_never_fires(monkeypatch, db_path, _isolated_relay):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:front door")
        with pytest.raises(PermissionError):
            aaco.execute(command, identity=_viewer_identity(), vms=boundary)
    assert _isolated_relay.calls == []
    rows = _audit_rows(db_path, "camera-front-door")
    assert rows and rows[-1]["authorization_result"] == "denied" and rows[-1]["success"] == 0


def test_wrong_tenant_cannot_resolve_or_unlock_another_customers_door(monkeypatch, db_path, _isolated_relay):
    _seed(db_path, second_tenant=True)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:tenant b door")
        with pytest.raises(PermissionError):
            aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert _isolated_relay.calls == []
    assert _audit_rows(db_path, "camera-tenant-b-door") == []


def test_nonexistent_door_fails_closed_with_zero_mutation(monkeypatch, db_path, _isolated_relay):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:back gate")
        with pytest.raises(PermissionError):
            aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert _isolated_relay.calls == []
    assert _audit_rows(db_path) == []


def test_plain_camera_that_is_not_a_configured_door_fails_closed(monkeypatch, db_path, _isolated_relay):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:plain camera")
        with pytest.raises(PermissionError):
            aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert _isolated_relay.calls == []


def test_ambiguous_door_name_returns_a_clarification_never_an_unlock(monkeypatch, db_path, _isolated_relay):
    _seed(db_path, duplicate_door_name=True)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:front door")
        result = aaco.execute(command, identity=_owner_identity(), vms=boundary)
    assert isinstance(result, aaco.Clarification)
    assert _isolated_relay.calls == []
    assert _audit_rows(db_path) == []


def test_relay_action_failure_is_audited_and_fails_closed(monkeypatch, db_path):
    _seed(db_path)

    class RaisingProvider:
        def trigger(self, request):
            raise RuntimeError("relay hardware unreachable")

    monkeypatch.setattr(relay_control, "_provider", RaisingProvider())
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:front door")
        with pytest.raises(PermissionError):
            aaco.execute(command, identity=_owner_identity(), vms=boundary)
    relay_control.reset_provider()
    rows = _audit_rows(db_path, "camera-front-door")
    assert rows and rows[-1]["authorization_result"] == "authorized"
    assert rows[-1]["relay_result"] == "failed" and rows[-1]["success"] == 0
    assert "relay hardware unreachable" in (rows[-1]["error"] or "")


def test_cooldown_suppressed_relay_result_is_audited_and_fails_closed(monkeypatch, db_path):
    _seed(db_path)
    provider = relay_control.MockRelayProvider(cooldown_seconds=30.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:front door")
        aaco.execute(command, identity=_owner_identity(), vms=boundary)
        with pytest.raises(PermissionError):
            aaco.execute(command, identity=_owner_identity(), vms=boundary)
    relay_control.reset_provider()
    rows = _audit_rows(db_path, "camera-front-door")
    assert rows[-1]["relay_result"] == "suppressed" and rows[-1]["success"] == 0 and rows[-1]["error"] == "cooldown"


def test_successful_unlock_writes_a_complete_audit_row_with_user_door_timestamp_and_result(monkeypatch, db_path, _isolated_relay):
    _seed(db_path)
    with override_target(sqlite_path=db_path):
        boundary = main._ClassicAacoBoundary(_request())
        command = aaco.AacoCommand("unlock_door", camera_id="camera-name:front door")
        aaco.execute(command, identity=_owner_identity(), vms=boundary)
    rows = _audit_rows(db_path, "camera-front-door")
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_email"] == "owner-a@example.test"
    assert row["actor_user_id"] == "owner-a"
    assert row["door_name"] == "Front Door"
    assert row["trigger_type"] == "aaco"
    assert row["authorization_result"] == "authorized"
    assert row["relay_result"] == "activated"
    assert row["success"] == 1
    assert row["created_at"]
