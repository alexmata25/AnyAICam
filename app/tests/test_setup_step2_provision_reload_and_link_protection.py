"""Regression coverage for two fixes made together (2026-09-13), both found
during the same real customer-journey audit (Cloud ID AIC-C814766E, real
Ryzen activation):

1. Customer Setup Step 3/4 showed a self-service-provisioned appliance as
   "Cloud ID —, Software Not installed, Status offline, Last check-in
   Never" even though the appliance was genuinely activated and
   heartbeating. Root cause: `customer_first_setup()`'s `appliances` list
   (and the `<select id="customer-appliance">` built from it) is rendered
   ONCE, server-side, at page load, and injected into the page's own
   `<script>` as a static JS array -- nothing on Step 2 ever refreshed it.
   The self-service "Provision appliance" button's success handler only
   filled the Cloud ID/activation-token *input fields*; unlike "Link
   appliance" (which already does `location.reload()` on success), it
   never reloaded the page, so "Save and continue" carried the stale,
   pre-provisioning (empty) `appliances` array all the way through Step 3
   and Step 4's appliance `<select>`. Fixed by reloading after a
   successful provision too -- gated behind a `confirm()` for a genuinely
   fresh provision (never before) so the one-time-shown activation token/
   QR value isn't silently discarded before the customer can copy it;
   immediate for the idempotent `already_provisioned` case, which never
   carries a fresh token to lose.

2. `POST /api/customer/appliances/link` unconditionally overwrote
   `online_status`/`software_version`/`last_check_in` from
   `get_provisioning_backend().verify_link()`'s response -- a snapshot
   captured once at `provision()` time and never updated again by real
   heartbeats (those go straight into the SQL `appliances` row via
   `appliance_cloud.heartbeat()`). Confirmed live: a customer who ran
   "Link appliance" (or an automation replaying it) *after* the physical
   appliance had already activated and was heartbeating would regress
   that appliance back to looking `offline`/"Not installed" in the DB.
   Fixed by skipping only the stale-field overwrite when
   `appliance['activation_status']=='activated'` -- token verification
   (the actual security check) and the rest of the endpoint are
   unchanged.
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


CUSTOMER_EMAIL = "step2-reload@example.test"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_setup_step2_provision_reload_and_link_protection.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


@pytest.fixture()
def mock_backend(tmp_path, monkeypatch):
    reset_provisioning_backend_for_tests()
    backend = MockProvisioningBackend(path=tmp_path / "mock_aws_provisioning.json")
    monkeypatch.setattr(provisioning_service, "_backend_instance", backend)
    yield backend
    reset_provisioning_backend_for_tests()


def _seed_tenant(conn, customer_id="cust-1", partner_id="partner-1", email=CUSTOMER_EMAIL):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Real Customer", email, "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Primary site", "2026-01-01"))
    conn.commit()


def _owner_cookie(customer_id="cust-1", email=CUSTOMER_EMAIL):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _owner_identity(customer_id="cust-1", email=CUSTOMER_EMAIL):
    return {"role": "customer_owner", "customer_id": customer_id, "email": email}


def _fake_request(headers=None):
    from types import SimpleNamespace
    return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


# --------------------------------------------------- fix 1: reload after self-service provisioning


def test_provision_success_handler_reloads_after_a_fresh_provision(http_client, db_path):
    """The confirmed-live bug: without this, Step 3/4 keep whatever
    (possibly empty) appliances array was baked in at page load, even
    after a successful POST /api/customer/appliances/provision. Asserts
    the exact fix: a fresh provision shows the confirmation message, then
    reloads (gated behind a confirm() so the one-time activation token/QR
    value isn't silently thrown away first)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "provisionApplianceButton.textContent='Provisioned';if(r.status==='already_provisioned')" in body
    # Fresh-provision branch: message set, THEN a confirm()-gated reload --
    # never an unconditional/instant reload that would discard the
    # one-time-shown token before the customer can copy it.
    fresh_branch = body[body.index("messageEl.textContent=`Provisioned Cloud ID"):]
    assert "if(confirm(" in fresh_branch
    confirm_call = fresh_branch[fresh_branch.index("if(confirm("):]
    assert confirm_call.index("))location.reload()") < confirm_call.index("document.getElementById('link-customer-appliance')")


def test_provision_success_handler_reloads_immediately_when_already_provisioned(http_client, db_path):
    """The idempotent short-circuit response never carries a fresh
    activation_token (nothing new was minted), so there is no one-time
    secret to protect -- this branch must reload right away, matching
    "Link appliance"'s own existing unconditional reload-on-success."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "if(r.status==='already_provisioned'){messageEl.textContent='An appliance was already provisioned for this account.';location.reload();return}" in body


# --------------------------------------------------- fix 2: link must not regress an already-activated appliance


def test_link_does_not_overwrite_live_heartbeat_fields_for_an_already_activated_appliance(db_path, mock_backend, monkeypatch):
    """The confirmed-live regression this closes: calling /link after the
    physical device already activated (real heartbeat data flowing into
    the row) must never fall back to the provisioning backend's
    permanently-stale online_status='offline'/software_version='Not
    installed'/last_check_in=None snapshot from provision() time."""
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 8}, idempotency_key="order-activated")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,software_version,last_check_in,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("appl-1", "cust-1", "site-1", provisioned["cloud_id"], "activated", "degraded", "0.1.0", "2026-09-12T22:00:38.183373", "2026-01-01"),
        )
        conn.commit()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
        assert result["message"] == "Appliance linked to customer account."

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT activation_status,online_status,software_version,last_check_in FROM appliances WHERE id='appl-1'").fetchone()
        # Real heartbeat data survives untouched -- not clobbered by the
        # mock backend's stale offline/"Not installed"/None snapshot.
        assert row["online_status"] == "degraded"
        assert row["software_version"] == "0.1.0"
        assert row["last_check_in"] == "2026-09-12T22:00:38.183373"
        # activation_status is the higher-confidence, device-confirmed
        # value -- never demoted back to 'linked'.
        assert row["activation_status"] == "activated"


def test_link_still_updates_status_fields_for_an_appliance_not_yet_activated(db_path, mock_backend, monkeypatch):
    """Control case, proving fix 2 is scoped correctly: an appliance the
    physical device has NOT yet activated (the original, pre-self-service
    "admin provisioned it, customer is confirming the Cloud ID/token"
    scenario) must still get its status fields populated from verify_link()
    exactly as before -- this is the only source of any status information
    for it at that point."""
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 4}, idempotency_key="order-pending")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
            ("appl-2", "cust-1", "site-1", provisioned["cloud_id"], "pending", "2026-01-01"),
        )
        conn.commit()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
        assert result["message"] == "Appliance linked to customer account."

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT activation_status,online_status FROM appliances WHERE id='appl-2'").fetchone()
        assert row["activation_status"] == "linked"
        assert row["online_status"] == "offline"  # the mock backend's own provision()-time default
