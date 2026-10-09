"""Camera add / capacity / remove / replacement, end to end (2026-10-08,
throwaway integration branch: Green's cloud 56e87fd + Red's e25dbd9).

Cloud: the real customer and appliance routes (TestClient, Green's helpers).
Appliance: the real configuration the cloud serves is fed to the real edge
sync (edge_camera_sync, its own database) and the real agent binding code.

  plan for 2 cameras -> add 2 (customer request + appliance confirmation)
  -> a 3rd is refused (403, code camera_limit_reached)
  -> remove one: no Stripe call, plan and entitlements unchanged
  -> appliance: the removed camera stops, its credentials are deleted, the
     agent forgets its binding; the other camera is untouched
  -> a replacement (the same physical device) gets a NEW camera and a NEW
     camera_number, credentials and a binding
  -> an appliance-side refusal reports failure_code.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

import edge_camera_sync
from appliance_protocol import encrypt_camera_credentials
from database_backend import override_target
from partner_db import initialize_database
from test_camera_add_remove_capacity import _activated, _add, _cameras, _camera_id, _per_camera_plan, _remove
from test_camera_slot_enforcement import (  # noqa: F401  (pytest fixtures)
    _appliance_headers, _seed_tenant, client, db_path,
)

from anyaicam_agent.camera_binding import CameraBindingStore, auto_bind_discovered_cameras, split_removed_cameras

DISCOVERED = [{"device_key": f"dev-{n}", "mac_address": f"14:2f:fd:a2:f6:a{n}", "ip": f"192.168.0.{40 + n}"} for n in (1, 2, 3)]


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    edge_db = tmp_path / "edge.db"
    with override_target(sqlite_path=str(edge_db)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    return edge_db


class Appliance:
    """The appliance side: the edge VMS database and the agent binding store,
    both driven by the configuration the cloud really serves."""

    def __init__(self, client, credential, cloud_db, edge_db, monkeypatch, folder):
        self.client, self.credential, self.cloud_db, self.edge_db, self.monkeypatch = client, credential, cloud_db, edge_db, monkeypatch
        self.bindings = CameraBindingStore(Path(folder) / "bindings.json")

    def configuration(self):
        with override_target(sqlite_path=str(self.cloud_db)):
            response = self.client.get("/api/appliance/configuration", headers=_appliance_headers("appl-1", self.credential))
        assert response.status_code == 200, response.text
        return response.json()

    def provisioned(self, device_key):
        # What the appliance itself holds after verifying the camera.
        with override_target(sqlite_path=str(self.edge_db)):
            con = sqlite3.connect(self.edge_db)
            con.execute("INSERT INTO pending_camera_credentials(device_key,encrypted_blob,created_at) VALUES(?,?,?)",
                        (device_key, encrypt_camera_credentials("admin", f"secret-{device_key}"), "2026-10-08"))
            con.commit(); con.close()

    def sync(self):
        configuration = self.configuration()
        with override_target(sqlite_path=str(self.cloud_db)):
            identity_row = sqlite3.connect(self.cloud_db).execute("SELECT customer_id, site_id FROM appliances WHERE id='appl-1'").fetchone()
        identity = {"appliance_id": "appl-1", "cloud_id": "AIC-SLOT1", "credential": self.credential,
                    "customer_id": identity_row[0], "site_id": identity_row[1]}
        self.monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: identity)
        self.monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: configuration)
        with override_target(sqlite_path=str(self.edge_db)):
            assert edge_camera_sync.sync_provisioned_cameras()["status"] == "ok"
        active, removed = split_removed_cameras(configuration.get("cameras", []))
        self.bindings.forget(removed)
        auto_bind_discovered_cameras(active, DISCOVERED, self.bindings)
        return configuration

    def local(self, sql, *args):
        con = sqlite3.connect(self.edge_db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args)]
        finally:
            con.close()

    def bound(self):
        return {b["cloud_camera_id"]: b for b in self.bindings.bindings()}


def _billing_snapshot(db_path):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        return {table: con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("customer_entitlements", "camera_plan_entitlements_v2")}


def test_add_capacity_remove_and_replacement_end_to_end(client, db_path, edge, monkeypatch, tmp_path):
    import main
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    credential = _activated(client, db_path)
    appliance = Appliance(client, credential, db_path, edge, monkeypatch, tmp_path)

    # ---- add two cameras: the plan is now full
    for key in ("dev-1", "dev-2"):
        assert _add(client, credential, key).status_code == 200
        appliance.provisioned(key)
    appliance.sync()
    cam1, cam2 = _camera_id(db_path, "dev-1"), _camera_id(db_path, "dev-2")
    numbers = {r["id"]: r["camera_number"] for r in appliance.local("SELECT id, camera_number FROM cameras")}
    assert set(numbers) == {cam1, cam2} and None not in numbers.values()
    assert {r["camera_id"] for r in appliance.local("SELECT camera_id FROM camera_credentials")} == {cam1, cam2}
    assert set(appliance.bound()) == {cam1, cam2}

    # ---- a third is refused with the capacity code the Cameras tab links
    third = _add(client, credential, "dev-3")
    assert third.status_code == 403 and third.json()["code"] == "camera_limit_reached"
    capacity = _cameras(client)["camera_capacity"]
    assert (capacity["can_add_camera"], capacity["code"], capacity["action_url"]) == (False, "camera_limit_reached", "/subscription-portal")

    # ---- the Cameras tab offers Remove for both cameras
    page = client.get("/customer-live").text
    for camera_id in (cam1, cam2):
        assert f'class="camera-tool remove-camera" hidden data-camera-id="{camera_id}"' in page

    # ---- remove camera 1: billing untouched
    billing_before = _billing_snapshot(db_path)
    monkeypatch.setattr(main, "stripe_api_post", lambda *a, **k: pytest.fail("removing a camera must not call Stripe"))
    removed = _remove(client, cam1)
    assert removed.status_code == 200 and removed.json()["camera_capacity"]["available"] == 1
    assert _billing_snapshot(db_path) == billing_before
    former_number = numbers[cam1]

    # ---- the appliance: camera 1 stops and forgets everything but its history
    appliance.sync()
    local = {r["id"]: r for r in appliance.local("SELECT id, status, camera_number, device_key FROM cameras")}
    assert (local[cam1]["status"], local[cam1]["camera_number"], local[cam1]["device_key"]) == ("removed", None, None)
    assert local[cam2]["camera_number"] == numbers[cam2]
    assert {r["camera_id"] for r in appliance.local("SELECT camera_id FROM camera_credentials")} == {cam2}
    assert appliance.local("SELECT * FROM pending_camera_credentials WHERE device_key='dev-1'") == []
    assert set(appliance.bound()) == {cam2}
    page = client.get("/customer-live").text
    assert cam1 not in page and cam2 in page

    # ---- a replacement: the same physical camera, added again
    assert _add(client, credential, "dev-1").status_code == 200
    appliance.provisioned("dev-1")
    appliance.sync()
    with override_target(sqlite_path=str(db_path)):
        rows = sqlite3.connect(db_path).execute(
            "SELECT id, status, camera_number, former_camera_number FROM cameras WHERE name='dev-1' ORDER BY created_at").fetchall()
    replacement = next(r for r in rows if r[1] != "removed")
    old = next(r for r in rows if r[1] == "removed")
    assert replacement[0] != cam1                                 # a new camera
    assert replacement[2] not in (None, former_number)            # a NEW camera number, never the removed one's
    assert old[3] == former_number                                # the removed camera keeps its former number
    assert {r["camera_id"] for r in appliance.local("SELECT camera_id FROM camera_credentials")} == {cam2, replacement[0]}
    bound = appliance.bound()
    assert set(bound) == {cam2, replacement[0]} and bound[replacement[0]]["camera_number"] == replacement[2]
    assert _cameras(client)["camera_capacity"]["can_add_camera"] is False   # full again
    assert _billing_snapshot(db_path) == billing_before


def test_an_appliance_side_refusal_reports_its_code(client, db_path, monkeypatch):
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    credential = _activated(client, db_path)
    assert _add(client, credential, "dev-1").status_code == 200
    # A request is queued while there is room ...
    from test_camera_slot_enforcement import _provision_request
    queued = _provision_request(client, appliance_id="appl-1", device_key="dev-2", name="dev-2")
    assert queued.status_code == 200
    job_id = queued.json()["job_id"]
    # ... then the plan shrinks before the appliance confirms it.
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.execute("UPDATE camera_plan_entitlements_v2 SET camera_quantity=1 WHERE customer_id='cust-1'")
        con.commit(); con.close()
    client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}", json={"success": True, "message": "Verified."},
                headers=_appliance_headers("appl-1", credential))
    from test_camera_slot_enforcement import _owner_cookie, _session_cookie_name
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    job = client.get(f"/api/customer/camera-provisioning/{job_id}").json()
    assert job["status"] == "failed" and job["failure_code"] == "camera_limit_reached"


def test_repeated_camera_add_remove_cycles_preserve_mappings_and_never_reuse_numbers(client, db_path):
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 1)
    credential = _activated(client, db_path)
    allocated = []
    for index in range(1, 4):
        device_key = f"cycle-camera-{index}"
        assert _add(client, credential, device_key).status_code == 200
        camera_id = _camera_id(db_path, device_key)
        with override_target(sqlite_path=str(db_path)):
            number = sqlite3.connect(db_path).execute(
                "SELECT camera_number FROM cameras WHERE id=?", (camera_id,)
            ).fetchone()[0]
        allocated.append(number)
        removed = _remove(client, camera_id)
        assert removed.status_code == 200, removed.text
        with override_target(sqlite_path=str(db_path)):
            row = sqlite3.connect(db_path).execute(
                "SELECT status,camera_number,former_camera_number,device_key FROM cameras WHERE id=?", (camera_id,)
            ).fetchone()
        assert row == ("removed", None, number, None)
    assert allocated == [1, 2, 3]
