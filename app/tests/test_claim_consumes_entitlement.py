"""Provisioning Phase 2, section 8: proves POST /api/customer/appliances/
link (the claim endpoint -- see provisioning_api.py's docstring for why
it, not a new /api/provisioning/claim, is the real claim implementation)
consumes customer_entitlements, and documents the deliberate design
decision behind how it does so: claiming an installation is never gated
by entitlement count (0 purchased slots still claims successfully), but
the response always reflects the caller's real, current
customer_entitlements-derived camera-slot total -- never a locally
fabricated or hard-coded number. Real slot enforcement belongs at
camera-provisioning time, matching this codebase's existing
per-camera-analytics-entitlement pattern (customer_analytics_panel.py's
assign_entitlement()), not at installation-claim time.
"""
import sqlite3

import pytest

import main
import partner_portal
import partner_workspace
import provisioning_service
from database_backend import override_target
from partner_db import initialize_database
from provisioning_service import MockProvisioningBackend, reset_provisioning_backend_for_tests


def _route(path, method="GET"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_claim_consumes_entitlement.db"


def _seed_tenant(conn, customer_id="cust-1", email="customer@example.test", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Real Customer", email, "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Main Site", "2026-01-01"))


def _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id=None):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", cloud_id or f"AIC-{appliance_id}", "pending", "2026-01-01"),
    )


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _fake_request():
    from types import SimpleNamespace
    return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


@pytest.fixture()
def mock_backend(tmp_path, monkeypatch):
    reset_provisioning_backend_for_tests()
    backend = MockProvisioningBackend(path=tmp_path / "mock_aws_provisioning.json")
    monkeypatch.setattr(provisioning_service, "_backend_instance", backend)
    yield backend
    reset_provisioning_backend_for_tests()


def test_claim_without_any_entitlement_still_succeeds_and_reports_zero_slots(db_path, mock_backend, monkeypatch):
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-none")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn, cloud_id=provisioned["cloud_id"])
        conn.commit()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
    assert result["message"] == "Appliance linked to customer account."
    assert result["camera_slots_purchased"] == 0


def test_claim_with_a_real_entitlement_reports_the_real_slot_count(db_path, mock_backend, monkeypatch):
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-with-slots")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn, cloud_id=provisioned["cloud_id"])
        conn.commit()
        import customer_entitlements as ce
        ce.upsert_entitlement(customer_id="cust-1", product="professional", camera_slot_quantity=10, status="active")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
    assert result["camera_slots_purchased"] == 10


def test_claim_never_fabricates_a_slot_count_it_only_ever_reads_the_authoritative_total(db_path, mock_backend, monkeypatch):
    """Regression guard against a future change hard-coding a number here
    instead of calling customer_entitlements.total_camera_slots()."""
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-cancelled")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn, cloud_id=provisioned["cloud_id"])
        conn.commit()
        import customer_entitlements as ce
        # A cancelled entitlement must not count toward the reported total.
        ce.upsert_entitlement(customer_id="cust-1", product="enterprise", camera_slot_quantity=25, status="cancelled")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
    assert result["camera_slots_purchased"] == 0
