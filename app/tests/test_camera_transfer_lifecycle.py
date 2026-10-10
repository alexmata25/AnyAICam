"""Isolated lifecycle coverage: transfers preserve source history and never move credentials."""

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main
import partner_workspace
from database_backend import override_target
from partner_db import initialize_database
from db_migrations import apply_migrations


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "camera-transfer.db"
    with override_target(sqlite_path=path):
        initialize_database()
    return path


def _route(path, method="POST"):
    for route in main.app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or {method}):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _request():
    return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda key, default=None: default))


def _identity(customer="ryzen", email=None, role="customer_owner"):
    return {"role": role, "customer_id": customer, "email": email or f"{customer}@example.test", "session_id": "session-test"}


def _seed_account(db, customer, *, email=None, quantity=1):
    email = email or f"{customer}@example.test"
    db.execute("INSERT INTO partners(id,name,created_at) VALUES(?,?,?)", (f"p-{customer}", customer, "2026-01-01"))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (customer, f"p-{customer}", customer, email, "active", "2026-01-01"))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"s-{customer}", customer, "Main", "2026-01-01"))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (f"a-{customer}", customer, f"s-{customer}", f"CLOUD-{customer}", "2026-01-01"))
    db.execute("INSERT INTO plans(id,customer_id,camera_quantity,status,created_at) VALUES(?,?,?,'active','2026-01-01')", (f"plan-{customer}", customer, quantity))


def _seed_camera(db, *, camera_id="cam-bedroom", customer="ryzen", key="uuid-a08360", status="configured", number=4):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number,device_key) VALUES(?,?,?,?,?,?,?, ?,?)",
        (camera_id, customer, f"s-{customer}", f"a-{customer}", "Bedroom camera #4", status, "2026-01-01", number if status != "removed" else None, key),
    )
    if status != "removed":
        db.execute("INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)", (camera_id, b"source-secret", "2026-01-01", "2026-01-01"))


def _seed_history(db, camera_id="cam-bedroom"):
    db.execute("INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,created_at) VALUES('rec-historic','ryzen','s-ryzen','a-ryzen',?,'ryzen/bedroom/old.mkv','2026-01-01','2026-01-02','2026-01-02')", (camera_id,))
    db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) VALUES('evt-historic','ryzen','s-ryzen','a-ryzen',?,'evt-local','motion','2026-01-01','2026-01-01')", (camera_id,))
    db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES('ryzen@example.test','customer_owner','camera.added','camera',?,'{}','2026-01-01')", (camera_id,))


def _seed_registry(db, key="uuid-a08360", customer="ryzen", camera_id="cam-bedroom"):
    db.execute("INSERT INTO camera_identity_registry(device_key,customer_id,camera_id,created_at) VALUES(?,?,?,'2026-01-01')", (key, customer, camera_id))


def test_two_party_transfer_archives_source_preserves_history_and_uses_destination_slot(db_path, monkeypatch):
    invitation = _route("/api/customer/camera-transfer-invitations")
    initiate = _route("/api/customer/cameras/{camera_id}/transfers")
    accept = _route("/api/customer/camera-transfers/{transfer_id}/accept")
    principal = {"identity": _identity("ryzen")}
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: principal["identity"])
    with override_target(sqlite_path=db_path):
        db = sqlite3.connect(db_path)
        _seed_account(db, "ryzen", email="anyaicamtest@gmail.com", quantity=4)
        _seed_account(db, "samsung", email="anyaicamtest2@gmail.com", quantity=1)
        _seed_camera(db)
        _seed_registry(db)
        _seed_history(db)
        db.commit()

        invite = invitation(_request(), {"target_email": "anyaicamtest2@gmail.com"})
        started = initiate(_request(), "cam-bedroom", {"confirmation": "TRANSFER", "invitation_code": invite["invitation_code"]})
        assert started["status"] == "awaiting_recipient"
        principal["identity"] = _identity("samsung", "anyaicamtest2@gmail.com")
        accepted = accept(_request(), started["transfer_id"], {"confirmation": "ACCEPT TRANSFER", "appliance_id": "a-samsung", "name": "Mobile Demo"})
        assert accepted["status"] == "completed"
        retried = accept(_request(), started["transfer_id"], {"confirmation": "ACCEPT TRANSFER", "appliance_id": "a-samsung"})
        assert retried["already_completed"] is True

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        old = db.execute("SELECT customer_id,status,former_camera_number,camera_number FROM cameras WHERE id='cam-bedroom'").fetchone()
        new = db.execute("SELECT customer_id,name,status,device_key FROM cameras WHERE id=?", (accepted["camera_id"],)).fetchone()
        registry = db.execute("SELECT customer_id,camera_id FROM camera_identity_registry WHERE device_key='uuid-a08360'").fetchone()
        assert tuple(old) == ("ryzen", "removed", 4, None)
        assert tuple(new) == ("samsung", "Mobile Demo", "pending_installation", "uuid-a08360")
        assert tuple(registry) == ("samsung", accepted["camera_id"])
        assert tuple(db.execute("SELECT customer_id,camera_id FROM recordings WHERE id='rec-historic'").fetchone()) == ("ryzen", "cam-bedroom")
        assert tuple(db.execute("SELECT customer_id,camera_id FROM detection_events WHERE id='evt-historic'").fetchone()) == ("ryzen", "cam-bedroom")
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE entity_id='cam-bedroom'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id='cam-bedroom'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id=?", (accepted["camera_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT camera_quantity FROM plans WHERE customer_id='ryzen'").fetchone()[0] == 4
        assert db.execute("SELECT camera_quantity FROM plans WHERE customer_id='samsung'").fetchone()[0] == 1


def test_wrong_recipient_and_duplicate_claim_are_rejected(db_path, monkeypatch):
    invitation = _route("/api/customer/camera-transfer-invitations")
    initiate = _route("/api/customer/cameras/{camera_id}/transfers")
    accept = _route("/api/customer/camera-transfers/{transfer_id}/accept")
    provision = _route("/api/customer/cameras/provision")
    principal = {"identity": _identity("ryzen")}
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: principal["identity"])
    with override_target(sqlite_path=db_path):
        db = sqlite3.connect(db_path)
        _seed_account(db, "ryzen", quantity=2)
        _seed_account(db, "samsung", quantity=1)
        _seed_account(db, "other", quantity=1)
        _seed_camera(db); _seed_registry(db); db.commit()
        token = invitation(_request(), {"target_email": "samsung@example.test"})["invitation_code"]
        transfer = initiate(_request(), "cam-bedroom", {"confirmation": "TRANSFER", "invitation_code": token})
        principal["identity"] = _identity("other")
        with pytest.raises(HTTPException) as denied:
            accept(_request(), transfer["transfer_id"], {"confirmation": "ACCEPT TRANSFER", "appliance_id": "a-other"})
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as duplicate:
            provision(_request(), {"appliance_id": "a-other", "device_key": "UUID-A08360"})
        assert duplicate.value.status_code == 409
        db = sqlite3.connect(db_path)
        assert db.execute("SELECT customer_id FROM camera_identity_registry WHERE device_key='uuid-a08360'").fetchone()[0] == "ryzen"


def test_transfer_requires_destination_license_and_does_not_partially_archive_source(db_path, monkeypatch):
    invitation = _route("/api/customer/camera-transfer-invitations")
    initiate = _route("/api/customer/cameras/{camera_id}/transfers")
    accept = _route("/api/customer/camera-transfers/{transfer_id}/accept")
    principal = {"identity": _identity("ryzen")}
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: principal["identity"])
    with override_target(sqlite_path=db_path):
        db = sqlite3.connect(db_path)
        _seed_account(db, "ryzen", quantity=2); _seed_account(db, "samsung", quantity=0)
        _seed_camera(db); _seed_registry(db); db.commit()
        token = invitation(_request(), {"target_email": "samsung@example.test"})["invitation_code"]
        transfer = initiate(_request(), "cam-bedroom", {"confirmation": "TRANSFER", "invitation_code": token})
        principal["identity"] = _identity("samsung")
        with pytest.raises(HTTPException) as denied:
            accept(_request(), transfer["transfer_id"], {"confirmation": "ACCEPT TRANSFER", "appliance_id": "a-samsung"})
        assert denied.value.status_code == 409
        db = sqlite3.connect(db_path)
        assert db.execute("SELECT status FROM cameras WHERE id='cam-bedroom'").fetchone()[0] == "configured"
        assert db.execute("SELECT customer_id FROM camera_identity_registry WHERE device_key='uuid-a08360'").fetchone()[0] == "ryzen"


def test_replace_archives_old_history_and_requires_a_new_identity_without_copying_credentials(db_path, monkeypatch):
    replace = _route("/api/customer/cameras/{camera_id}/replace")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity("ryzen"))
    with override_target(sqlite_path=db_path):
        db = sqlite3.connect(db_path)
        _seed_account(db, "ryzen", quantity=1); _seed_camera(db); _seed_registry(db); _seed_history(db); db.commit()
        result = replace(_request(), "cam-bedroom", {"confirmation": "REPLACE", "device_key": "uuid-new-device", "appliance_id": "a-ryzen", "name": "Replacement"})
        db = sqlite3.connect(db_path)
        assert result["status"] == "queued" and result["credentials_transferred"] is False
        assert db.execute("SELECT status FROM cameras WHERE id='cam-bedroom'").fetchone()[0] == "removed"
        new = db.execute("SELECT customer_id,status,device_key FROM cameras WHERE id=?", (result["camera_id"],)).fetchone()
        assert new == ("ryzen", "pending_installation", "uuid-new-device")
        assert db.execute("SELECT customer_id,camera_id FROM recordings WHERE id='rec-historic'").fetchone() == ("ryzen", "cam-bedroom")
        assert db.execute("SELECT COUNT(*) FROM camera_credentials WHERE camera_id=?", (result["camera_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM cameras WHERE customer_id='ryzen' AND device_key IS NOT NULL AND status!='removed'").fetchone()[0] == 1
        assert db.execute("SELECT camera_quantity FROM plans WHERE customer_id='ryzen'").fetchone()[0] == 1


def test_replacement_rejects_identity_owned_by_another_customer_without_changes(db_path, monkeypatch):
    replace = _route("/api/customer/cameras/{camera_id}/replace")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _identity("ryzen"))
    with override_target(sqlite_path=db_path):
        db = sqlite3.connect(db_path)
        _seed_account(db, "ryzen", quantity=2); _seed_account(db, "samsung", quantity=1)
        _seed_camera(db); _seed_registry(db)
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,device_key) VALUES('cam-samsung','samsung','s-samsung','a-samsung','Existing','configured','2026-01-01','other-device')")
        db.execute("INSERT INTO camera_identity_registry(device_key,customer_id,camera_id,created_at) VALUES('other-device','samsung','cam-samsung','2026-01-01')")
        db.commit()
        with pytest.raises(HTTPException) as denied:
            replace(_request(), "cam-bedroom", {"confirmation": "REPLACE", "device_key": "other-device", "appliance_id": "a-ryzen"})
        assert denied.value.status_code == 409
        db = sqlite3.connect(db_path)
        assert db.execute("SELECT status FROM cameras WHERE id='cam-bedroom'").fetchone()[0] == "configured"


def test_admin_reconciliation_records_authority_without_fabricating_owner_approval(db_path, monkeypatch):
    reconcile = _route("/api/admin/camera-ownership/reconcile")
    principal = {"identity": _identity("admin", "global-admin@example.test", "administrator")}
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: principal["identity"])
    with override_target(sqlite_path=db_path):
        db = sqlite3.connect(db_path)
        _seed_account(db, "ryzen", quantity=4); _seed_account(db, "samsung", quantity=1, email="anyaicamtest2@gmail.com")
        _seed_camera(db, status="removed"); _seed_registry(db)
        db.execute("UPDATE cameras SET former_camera_number=4 WHERE id='cam-bedroom'")
        _seed_history(db)
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number,device_key) VALUES('cam-mobile-demo','samsung','s-samsung','a-samsung','Mobile Demo','configured','2026-10-10',1,'uuid-a08360')")
        db.execute("INSERT INTO camera_identity_conflicts(device_key,reason,status,created_at) VALUES('uuid-a08360','legacy duplicated ownership','needs_review','2026-10-10')")
        admin_id="admin-user"
        db.execute("INSERT INTO partner_users(id,email,name,role,password_hash,approved,created_at) VALUES(?,?,'Admin','administrator','x',1,'2026-01-01')",(admin_id,"global-admin@example.test"))
        db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,granted_at) VALUES('grant-admin',?,'administrator','global','2026-01-01')",(admin_id,))
        db.commit()
        result=reconcile(_request(),{"source_camera_id":"cam-bedroom","target_customer_id":"samsung","target_camera_id":"cam-mobile-demo","basis":"verified_historical_transfer","evidence_reference":"Green-acceptance-20261010","reason":"Verified appliance acceptance confirms the historical cross-account device transfer.","confirmation":"RECONCILE cam-bedroom"})
        db=sqlite3.connect(db_path)
        assert result["status"]=="reconciled" and result["owner_authorizations_fabricated"] is False
        assert db.execute("SELECT customer_id,camera_id FROM camera_identity_registry WHERE device_key='uuid-a08360'").fetchone()==("samsung","cam-mobile-demo")
        assert tuple(db.execute("SELECT customer_id,camera_id FROM recordings WHERE id='rec-historic'").fetchone())==("ryzen","cam-bedroom")
        event=db.execute("SELECT action,details_json FROM camera_ownership_transfer_events WHERE transfer_id=?",(result["transfer_id"],)).fetchone()
        assert event[0]=="admin_reconciled" and '"owner_authorizations_fabricated": false' in event[1]
        assert db.execute("SELECT accepted_by_email FROM camera_ownership_transfers WHERE id=?",(result["transfer_id"],)).fetchone()[0] is None


def test_admin_reconciliation_requires_global_grant(db_path, monkeypatch):
    reconcile=_route("/api/admin/camera-ownership/reconcile")
    monkeypatch.setattr(partner_workspace,"partner_identity",lambda request:_identity("admin","not-global@example.test","administrator"))
    with override_target(sqlite_path=db_path):
        db=sqlite3.connect(db_path); _seed_account(db,"ryzen"); _seed_account(db,"samsung")
        _seed_camera(db,status="removed")
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,device_key) VALUES('target','samsung','s-samsung','a-samsung','target','configured','2026','uuid-a08360')")
        db.commit()
        with pytest.raises(HTTPException) as denied:
            reconcile(_request(),{"source_camera_id":"cam-bedroom","target_customer_id":"samsung","target_camera_id":"target","basis":"source_account_unavailable","evidence_reference":"case-123456","reason":"Source owner account was verified unavailable by support.","confirmation":"RECONCILE cam-bedroom"})
        assert denied.value.status_code==403


def test_migration_never_infers_cross_customer_owner_from_oldest_camera_row(db_path):
    with override_target(sqlite_path=db_path):
        db=sqlite3.connect(db_path)
        _seed_account(db,"ryzen"); _seed_account(db,"samsung")
        _seed_camera(db,camera_id="old-row",customer="ryzen",key="same-device",status="removed",number=4)
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,device_key) VALUES('new-row','samsung','s-samsung','a-samsung','Mobile Demo','configured','2099-01-01','same-device')")
        db.execute("DELETE FROM camera_identity_registry WHERE device_key='same-device'")
        db.execute("DELETE FROM camera_identity_conflicts WHERE device_key='same-device'")
        db.commit(); db.close()
        apply_migrations()
        db=sqlite3.connect(db_path)
        assert db.execute("SELECT COUNT(*) FROM camera_identity_registry WHERE device_key='same-device'").fetchone()[0] == 0
        assert db.execute("SELECT status,reason FROM camera_identity_conflicts WHERE device_key='same-device'").fetchone() == (
            "needs_review", "multiple_customer_rows_require_authorized_reconciliation",
        )


def test_transfer_invitation_cannot_be_replayed_or_accepted_after_expiry(db_path, monkeypatch):
    invitation = _route("/api/customer/camera-transfer-invitations")
    initiate = _route("/api/customer/cameras/{camera_id}/transfers")
    accept = _route("/api/customer/camera-transfers/{transfer_id}/accept")
    principal={"identity":_identity("ryzen")}
    monkeypatch.setattr(partner_workspace,"partner_identity",lambda request:principal["identity"])
    with override_target(sqlite_path=db_path):
        db=sqlite3.connect(db_path); _seed_account(db,"ryzen",quantity=2); _seed_account(db,"samsung")
        _seed_camera(db); _seed_registry(db); db.commit()
        code=invitation(_request(),{"target_email":"samsung@example.test"})["invitation_code"]
        started=initiate(_request(),"cam-bedroom",{"confirmation":"TRANSFER","invitation_code":code})
        replay=initiate(_request(),"cam-bedroom",{"confirmation":"TRANSFER","invitation_code":code})
        assert replay["already_requested"] is True and replay["transfer_id"] == started["transfer_id"]
        db=sqlite3.connect(db_path)
        assert db.execute("SELECT COUNT(*) FROM camera_ownership_transfers WHERE device_key='uuid-a08360'").fetchone()[0] == 1
        db.execute("UPDATE camera_ownership_transfers SET expires_at='2000-01-01T00:00:00' WHERE id=?",(started["transfer_id"],)); db.commit()
        principal["identity"]=_identity("samsung")
        with pytest.raises(HTTPException) as stale:
            accept(_request(),started["transfer_id"],{"confirmation":"ACCEPT TRANSFER","appliance_id":"a-samsung"})
        assert stale.value.status_code in (400,409)
        db=sqlite3.connect(db_path)
        assert db.execute("SELECT status FROM cameras WHERE id='cam-bedroom'").fetchone()[0] == "configured"
        assert db.execute("SELECT customer_id FROM camera_identity_registry WHERE device_key='uuid-a08360'").fetchone()[0] == "ryzen"
