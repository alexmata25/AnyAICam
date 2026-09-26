"""Regression coverage for a confirmed-live bug: Step 2 ("Add appliance")
of /customer/setup was showing the signed-in customer's own account EMAIL
in the Cloud ID field. Root cause, found by elimination: neither the
server template nor any script on the page ever wrote the customer's
email (or anything else) into that input -- it starts with no `value=`
attribute, no `name`, and no `autocomplete`, and it sits immediately
before a `type="password"` field (the activation token) with no <form>
wrapper anywhere on the page. That exact shape -- a bare text input right
before a password input -- is what Chrome/Firefox/1Password/LastPass use
to decide "this is a login form" and autofill the origin's saved
username (the customer's own login email, saved from customer-login.html)
into it, entirely client-side; nothing server-side was ever wrong.

Fixed with the standard cross-browser mitigation: off-screen decoy
username/password inputs ahead of the real fields (so autofill has
something else to latch onto), plus autocomplete="off"/"new-password"
and password-manager-specific ignore attributes as defense in depth on
top of the decoys, not a replacement for them.

Customer email is the customer's own identity. Cloud ID is the
installation's identity, minted only by provisioning_service.py's
ProvisioningBackend -- never by the browser, and never by main.py or
partner_workspace.py directly (see that module's docstring). These tests
prove the two are never conflated in the rendered page or in the link
flow.
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


CUSTOMER_EMAIL = "alexmata25@yahoo.com"  # the exact value confirmed live in the bug report


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_setup_cloud_id_field.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id="cust-1", partner_id="partner-1", email=CUSTOMER_EMAIL):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Real Customer", email, "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Main Site", "2026-01-01"))
    conn.commit()


def _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id=None, activation_status="pending"):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", cloud_id or f"AIC-{appliance_id}", activation_status, "2026-01-01"),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1", email=CUSTOMER_EMAIL):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _owner_identity(customer_id="cust-1", email=CUSTOMER_EMAIL):
    return {"role": "customer_owner", "customer_id": customer_id, "email": email}


def _fake_request(headers=None):
    from types import SimpleNamespace
    return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


def _cloud_id_input_tag(body: str) -> str:
    start = body.index('id="customer-cloud-id"')
    tag_start = body.rindex('<input', 0, start)
    tag_end = body.index('>', start)
    return body[tag_start:tag_end + 1]


# --------------------------------------------------- the reported symptom


def test_cloud_id_field_never_contains_the_customer_email_anywhere_on_the_page(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, email=CUSTOMER_EMAIL)
    _seed_appliance(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert CUSTOMER_EMAIL not in response.text


def test_cloud_id_input_has_no_value_attribute_when_nothing_is_claimed_yet(http_client, db_path):
    """Expected behavior: with no appliance claimed via this field, Cloud
    ID must be blank -- never silently populated from session/customer
    data of any kind."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    tag = _cloud_id_input_tag(response.text)
    assert "value=" not in tag


def test_cloud_id_input_carries_autofill_suppression_and_a_decoy_precedes_it(http_client, db_path):
    """The actual root cause: a bare text input immediately before a
    type="password" input, no <form>, no autocomplete -- indistinguishable
    from a login form to browser/password-manager autofill. Confirms the
    fix's specific mitigations are present."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    cloud_id_tag = _cloud_id_input_tag(body)
    assert 'autocomplete="off"' in cloud_id_tag
    assert 'data-lpignore="true"' in cloud_id_tag
    # Decoy username/password pair present ahead of the real fields, so
    # autofill has something else to target instead.
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body
    assert body.index('autocomplete="username"') < body.index(cloud_id_tag)
    token_start = body.index('id="customer-activation-token"')
    token_tag = body[body.rindex('<input', 0, token_start):body.index('>', token_start) + 1]
    assert 'autocomplete="new-password"' in token_tag


# --------------------------------------------- valid appliance identity still works normally


def test_a_real_appliance_cloud_id_still_appears_correctly_in_the_status_step(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn, appliance_id="appl-1", cloud_id="AIC-REAL1")
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert '"cloud_id": "AIC-REAL1"' in body or '"cloud_id":"AIC-REAL1"' in body


def test_no_appliance_at_all_means_no_cloud_id_is_fabricated(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    # "AIC-XXXXXXXX" is only ever the static placeholder text here (no
    # appliance was seeded, so no real cloud_id exists to leak); what
    # must never happen is a real-looking value attribute or the
    # customer's email standing in for one.
    tag = _cloud_id_input_tag(response.text)
    assert "value=" not in tag
    assert CUSTOMER_EMAIL not in response.text


# --------------------------------------------- activation token independent of customer identity, tenant isolation


@pytest.fixture()
def mock_backend(tmp_path, monkeypatch):
    reset_provisioning_backend_for_tests()
    backend = MockProvisioningBackend(path=tmp_path / "mock_aws_provisioning.json")
    monkeypatch.setattr(provisioning_service, "_backend_instance", backend)
    yield backend
    reset_provisioning_backend_for_tests()


def test_link_succeeds_from_cloud_id_and_token_alone_regardless_of_session_email(db_path, mock_backend, monkeypatch):
    """Proves the activation token/cloud_id pair -- not the customer's
    email -- is what the link flow actually verifies."""
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 2}, idempotency_key="order-1")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, email="totally-different-address@example.test")
        _seed_appliance(conn, appliance_id="appl-1", cloud_id=provisioned["cloud_id"])
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity(email="totally-different-address@example.test"))
        result = link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
    assert result["message"] == "Appliance linked to customer account."


def test_link_is_scoped_to_the_caller_customer_even_with_a_valid_token(db_path, mock_backend, monkeypatch):
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-2")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, customer_id="cust-1")
        _seed_tenant(conn, customer_id="cust-2", email="other-customer@example.test")
        # The appliance really belongs to cust-1 locally too.
        _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id=provisioned["cloud_id"])
        # cust-2 has the *correct* token (it came straight from the mock
        # backend) but must still be refused: this cloud_id is not on
        # their account.
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity(customer_id="cust-2", email="other-customer@example.test"))
        with pytest.raises(Exception) as excinfo:
            link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": provisioned["activation_token"]})
    assert getattr(excinfo.value, "status_code", None) == 404


def test_link_rejects_a_wrong_activation_token(db_path, mock_backend, monkeypatch):
    link_appliance = _route("/api/customer/appliances/link", "POST")
    provisioned = mock_backend.provision({"customer_id": "cust-1", "camera_count": 1}, idempotency_key="order-3")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn, appliance_id="appl-1", cloud_id=provisioned["cloud_id"])
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(Exception) as excinfo:
            link_appliance(_fake_request(), {"cloud_id": provisioned["cloud_id"], "activation_token": "wrong-token"})
    assert getattr(excinfo.value, "status_code", None) == 403
