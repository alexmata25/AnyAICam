"""A claimed (self-installed) appliance syncs its configuration (2026-10-02).

Found in the installer E2E on a fresh Ubuntu machine: a claimed appliance
has no partner_id of its own, so the configuration's identity carried no
partner while its customer row pointed at one. The appliance's local
customers insert then failed its FOREIGN KEY on every edge sync iteration
-- no camera, analytics rule or security setting could ever reach it. The
identity now carries the customer's own partner when the appliance has
none, and the real cloud response applies cleanly on the edge."""
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target
from test_appliance_analytics_rules_delivery import _appliance_auth_headers, _seed_appliance

with override_target(sqlite_path="/tmp/test_claimed_appliance_identity.db"):
    import appliance_cloud
    from partner_db import connection, initialize_database


@pytest.fixture()
def cloud(tmp_path):
    path = tmp_path / "cloud.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1")  # claimed: appliances.partner_id is NULL
            assert db.execute("SELECT partner_id FROM appliances WHERE id='appl-1'").fetchone()[0] is None
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as client:
            yield client


def test_the_identity_carries_the_customers_partner_for_a_claimed_appliance(cloud):
    config = cloud.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1")).json()
    identity = config["identity"]
    assert identity["customer"]["partner_id"] == "partner-cust-1"
    assert identity["partner"] == {"id": "partner-cust-1", "name": "Test Partner", "approval_status": "approved"}
    assert identity["appliance"]["partner_id"] is None  # the appliance row itself is unchanged


def test_the_edge_applies_a_claimed_appliances_configuration(cloud, tmp_path, monkeypatch):
    import edge_camera_sync
    response = cloud.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1")).json()
    edge_db = tmp_path / "edge.db"
    with override_target(sqlite_path=str(edge_db)):
        initialize_database()
        monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
        monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
        monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
        monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: {
            "appliance_id": "appl-1", "cloud_id": "AIC-1", "credential": "cred-1", "customer_id": "cust-1", "site_id": "site-1"})
        monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: response)
        monkeypatch.setattr(edge_camera_sync.product_mode, "persist_mode", lambda mode: False)
        result = edge_camera_sync.sync_provisioned_cameras()
        assert result is not None
        con = sqlite3.connect(edge_db)
        assert con.execute("SELECT partner_id FROM customers WHERE id='cust-1'").fetchone() == ("partner-cust-1",)
        assert con.execute("SELECT id FROM partners").fetchall() == [("partner-cust-1",)]
        assert con.execute("SELECT id FROM cameras WHERE id='cam-1'").fetchone() == ("cam-1",)
