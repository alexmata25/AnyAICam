"""Regression/integration coverage for the self-service Cloud ID
provisioning bridge (2026-09-12).

Confirmed-live gap this closes: a customer who purchases hardware + a
camera-slot plan through the storefront and is approved via
/customer-registration-requests (never through the partner-run
"Customer onboarding" wizard) reached Customer Setup Step 2 with no
site and no appliance -- POST /api/customer/appliances/link's own
precondition (an appliances row already scoped to this customer_id,
created by partner_workspace.onboard_customer()) was simply never met.

POST /api/customer/appliances/provision (partner_workspace.py) closes
this by reusing the exact same authoritative
get_provisioning_backend().provision() seam the admin-run onboarding
endpoint already uses -- never a second provisioning backend, never a
hardcoded camera-slot quantity (customer_entitlements.total_camera_
slots() is the one source of truth), never bypassing Cloud ID/
activation-token security (hashing, expiry, single-use device
redemption via the unmodified POST /api/appliance/activate all stay
exactly as they were).

Follows this test suite's own established pattern (see
test_customer_setup_cloud_id_field.py): grab the raw endpoint function
via _route(), fake the authenticated identity via monkeypatching
partner_workspace.partner_identity, seed a throwaway SQLite DB
directly, and call the endpoint like a plain function -- no HTTP layer,
no CSRF concerns, matching every other appliance-linking test in this
file's sibling module.
"""
import sqlite3

import pytest

import main
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


def _fake_request(headers=None):
    from types import SimpleNamespace
    return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


def _owner_identity(customer_id="cust-1", email="owner@example.test"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": email}


def _seed_customer(conn, customer_id="cust-1", partner_id="partner-1", email="owner@example.test", name="Real Customer"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, name, email, "active", "2026-01-01"),
    )
    conn.commit()


def _seed_paid_hardware(conn, customer_id="cust-1", order_id="hw-1", status="paid"):
    conn.execute(
        "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,status,fulfillment_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, customer_id, "anyaicam-starter", "AnyAiCam Starter", "price_test_hw", 1, 50, status, "unfulfilled", "2026-01-01", "2026-01-01"),
    )
    conn.commit()


def _seed_camera_entitlement(conn, customer_id="cust-1", entitlement_id="ent-1", quantity=8, status="active", product="camera_slots_local"):
    conn.execute(
        "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (entitlement_id, customer_id, product, quantity, status, "2026-01-01", "2026-01-01"),
    )
    conn.commit()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_self_service_appliance_provisioning.db"


@pytest.fixture()
def mock_backend(tmp_path, monkeypatch):
    reset_provisioning_backend_for_tests()
    backend = MockProvisioningBackend(path=tmp_path / "mock_aws_provisioning.json")
    monkeypatch.setattr(provisioning_service, "_backend_instance", backend)
    yield backend
    reset_provisioning_backend_for_tests()


def _as_customer(monkeypatch, customer_id="cust-1", email="owner@example.test"):
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity(customer_id=customer_id, email=email))


# --------------------------------------------------- the real customer journey, end to end


def test_full_self_service_journey_paid_hardware_and_8_slot_entitlement_to_linked_cloud_id(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    link_appliance = _route("/api/customer/appliances/link", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_paid_hardware(conn)
        _seed_camera_entitlement(conn, quantity=8)
        _as_customer(monkeypatch)

        result = provision(_fake_request(), {"site_name": "HQ", "site_address": "1 Main St"})
        assert result["status"] == "provisioned"
        assert result["cloud_id"].startswith("AIC-")
        assert result["camera_capacity"] == 8
        assert result["activation_token"]
        assert result["provisioning_qr_payload"] == f'{result["cloud_id"]}|{result["activation_token"]}'

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        sites = conn.execute("SELECT * FROM sites WHERE customer_id='cust-1'").fetchall()
        assert len(sites) == 1
        assert sites[0]["name"] == "HQ"
        assert sites[0]["address"] == "1 Main St"
        appliances = conn.execute("SELECT * FROM appliances WHERE customer_id='cust-1'").fetchall()
        assert len(appliances) == 1
        assert appliances[0]["cloud_id"] == result["cloud_id"]
        assert appliances[0]["site_id"] == sites[0]["id"]
        tokens = conn.execute("SELECT * FROM appliance_activation_tokens WHERE appliance_id=?", (appliances[0]["id"],)).fetchall()
        assert len(tokens) == 1
        assert tokens[0]["used_at"] is None  # not yet redeemed by the device
        cameras = conn.execute("SELECT * FROM cameras WHERE appliance_id=?", (appliances[0]["id"],)).fetchall()
        assert len(cameras) == 8
        assert all(c["status"] == "pending_installation" for c in cameras)

        # Existing, unmodified Step 2 link flow must accept exactly what
        # was just provisioned -- proves this bridge produces a real
        # Cloud ID + token the canonical flow already knows how to consume.
        _as_customer(monkeypatch)
        link_result = link_appliance(_fake_request(), {"cloud_id": result["cloud_id"], "activation_token": result["activation_token"]})
        assert link_result["message"] == "Appliance linked to customer account."


# --------------------------------------------------- authorization


def test_unauthorized_customer_is_rejected(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: None)
        with pytest.raises(Exception) as excinfo:
            provision(_fake_request(), {})
    assert getattr(excinfo.value, "status_code", None) == 403


def test_non_customer_owner_role_is_rejected(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {"role": "customer_viewer", "customer_id": "cust-1", "email": "viewer@example.test"})
        with pytest.raises(Exception) as excinfo:
            provision(_fake_request(), {})
    assert getattr(excinfo.value, "status_code", None) == 403


def test_customer_cannot_provision_against_another_customers_records(db_path, mock_backend, monkeypatch):
    """A payload-supplied customer_id (or anything else) must never
    override the authenticated session's own customer_id -- the
    provisioned appliance/site must always belong to whoever is
    actually logged in, never to a customer_id an attacker's payload
    happens to mention."""
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn, customer_id="cust-1", email="owner@example.test")
        _seed_customer(conn, customer_id="cust-2", email="victim@example.test")
        _seed_paid_hardware(conn, customer_id="cust-1")
        _seed_camera_entitlement(conn, customer_id="cust-1", quantity=8)
        # cust-2 has its own real, separate entitlement+hardware too, so a
        # cross-tenant leak would be visible either direction.
        _seed_paid_hardware(conn, customer_id="cust-2", order_id="hw-2")
        _seed_camera_entitlement(conn, customer_id="cust-2", entitlement_id="ent-2", quantity=3)
        _as_customer(monkeypatch, customer_id="cust-1", email="owner@example.test")

        result = provision(_fake_request(), {"site_name": "HQ", "customer_id": "cust-2"})
        assert result["status"] == "provisioned"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        appliance = conn.execute("SELECT * FROM appliances WHERE cloud_id=?", (result["cloud_id"],)).fetchone()
        assert appliance["customer_id"] == "cust-1"  # never cust-2, despite the payload
        assert conn.execute("SELECT COUNT(*) AS n FROM appliances WHERE customer_id='cust-2'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM cameras WHERE customer_id='cust-1'").fetchone()["n"] == 8


# --------------------------------------------------- eligibility gates


def test_no_paid_hardware_order_fails_safely(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_camera_entitlement(conn, quantity=8)  # entitlement present, hardware is not
        _as_customer(monkeypatch)
        with pytest.raises(Exception) as excinfo:
            provision(_fake_request(), {})
    assert getattr(excinfo.value, "status_code", None) == 403
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM appliances").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 0


def test_unpaid_hardware_order_does_not_count_as_eligible(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_paid_hardware(conn, status="pending")
        _seed_camera_entitlement(conn, quantity=8)
        _as_customer(monkeypatch)
        with pytest.raises(Exception) as excinfo:
            provision(_fake_request(), {})
    assert getattr(excinfo.value, "status_code", None) == 403


def test_no_entitlement_fails_safely(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_paid_hardware(conn)  # hardware present, entitlement is not
        _as_customer(monkeypatch)
        with pytest.raises(Exception) as excinfo:
            provision(_fake_request(), {})
    assert getattr(excinfo.value, "status_code", None) == 403
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM appliances").fetchone()[0] == 0


def test_inactive_entitlement_does_not_count_as_eligible(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_paid_hardware(conn)
        _seed_camera_entitlement(conn, quantity=8, status="cancelled")
        _as_customer(monkeypatch)
        with pytest.raises(Exception) as excinfo:
            provision(_fake_request(), {})
    assert getattr(excinfo.value, "status_code", None) == 403


# --------------------------------------------------- idempotency / no duplicates


def test_retrying_the_same_provisioning_call_is_idempotent(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_paid_hardware(conn)
        _seed_camera_entitlement(conn, quantity=8)
        _as_customer(monkeypatch)

        first = provision(_fake_request(), {"site_name": "HQ"})
        assert first["status"] == "provisioned"
        second = provision(_fake_request(), {"site_name": "A different name entirely"})
        assert second["status"] == "already_provisioned"
        assert second["cloud_id"] == first["cloud_id"]
        assert second["site_id"] == first["site_id"]
        assert second["appliance_id"] == first["appliance_id"]

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM sites WHERE customer_id='cust-1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM appliances WHERE customer_id='cust-1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cameras WHERE customer_id='cust-1'").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM appliance_activation_tokens").fetchone()[0] == 1
        # The site name from the first call survives -- the retry must
        # never re-create or rename it.
        site_name = conn.execute("SELECT name FROM sites WHERE customer_id='cust-1'").fetchone()[0]
        assert site_name == "HQ"


def test_retry_after_partial_local_failure_recovers_the_same_cloud_id_and_token(db_path, mock_backend, monkeypatch):
    """Simulates the one genuine retry scenario that reaches
    get_provisioning_backend().provision() a second time: the backend
    call succeeded and durably recorded the idempotency key, but the
    local SQL transaction never got a chance to insert anything (e.g.
    the process died in between). A retry must recover the exact same
    identity, not mint a second Cloud ID for this one customer."""
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn)
        _seed_paid_hardware(conn)
        _seed_camera_entitlement(conn, quantity=8)
        _as_customer(monkeypatch)

        # Pre-seed the provisioning backend's own idempotency record
        # directly, exactly as if a first call to provision() had
        # already succeeded there, without ever reaching the local
        # sites/appliances INSERTs.
        pre = mock_backend.provision({"customer_id": "cust-1", "site_id": "site-orphan", "camera_count": 8}, idempotency_key="self-service-provision:cust-1")

        result = provision(_fake_request(), {"site_name": "HQ"})
        assert result["status"] == "provisioned"
        assert result["cloud_id"] == pre["cloud_id"]
        assert result["site_id"] == "site-orphan"

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM appliances").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM sites WHERE id='site-orphan'").fetchone() is not None
        assert conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 8


# --------------------------------------------------- entitlement quantity controls capacity


def test_entitlement_quantity_controls_camera_capacity_not_a_hardcoded_number(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn, customer_id="cust-3", email="three@example.test")
        _seed_paid_hardware(conn, customer_id="cust-3", order_id="hw-3")
        _seed_camera_entitlement(conn, customer_id="cust-3", entitlement_id="ent-3", quantity=3)
        _as_customer(monkeypatch, customer_id="cust-3", email="three@example.test")

        result = provision(_fake_request(), {})
        assert result["camera_capacity"] == 3

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM cameras WHERE customer_id='cust-3'").fetchone()[0] == 3


def test_multiple_active_entitlements_sum_to_the_real_total(db_path, mock_backend, monkeypatch):
    provision = _route("/api/customer/appliances/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_customer(conn, customer_id="cust-4", email="four@example.test")
        _seed_paid_hardware(conn, customer_id="cust-4", order_id="hw-4")
        _seed_camera_entitlement(conn, customer_id="cust-4", entitlement_id="ent-4a", quantity=8, product="camera_slots_local")
        _seed_camera_entitlement(conn, customer_id="cust-4", entitlement_id="ent-4b", quantity=8, product="camera_slots_hybrid")
        _as_customer(monkeypatch, customer_id="cust-4", email="four@example.test")

        result = provision(_fake_request(), {})
        assert result["camera_capacity"] == 16
