"""Owner self-service activation-token recovery, and setup completion that
waits for the device (2026-10-02).

- Lost response: the provision call's one-time token never reached the
  owner. The idempotent answer carries no token; the owner recovers with an
  auditable action that revokes the old token and issues a new one through
  the same table POST /api/appliance/activate verifies -- only while the
  appliance is still pending, only for the owner's own appliance.
- confirm_customer_setup() used to mark the appliance activated itself.
  Device activation is now the appliance's alone; the wizard refuses to
  complete until it happened and records its own completion separately.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_camera_slot_enforcement import (  # noqa: F401 -- fixtures and helpers
    _seed_and_activate_appliance,
    _seed_tenant,
    client,
    db_path,
)

import partner_portal

RECOVER = "/api/customer/appliances/appl-1/activation-token/recover"


def _as(client, role="customer_owner", customer_id="cust-1", email="slot-test@example.test"):
    client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token(email, role, None, customer_id, None))
    return client


def _activate(client, token, cloud_id="AIC-SLOT1"):
    client.cookies.clear()
    return client.post("/api/appliance/activate", json={"cloud_id": cloud_id, "activation_token": token})


def _audit_actions(db_path):
    return [r[0] for r in sqlite3.connect(db_path).execute("SELECT action FROM audit_logs ORDER BY rowid")]


# ------------------------------------------------------------------ recovery

def test_a_lost_response_is_recovered_by_the_owner_and_the_new_token_activates(client, db_path):
    _seed_tenant(db_path)
    old_token = _seed_and_activate_appliance(db_path)
    recovered = _as(client).post(RECOVER)
    assert recovered.status_code == 200, recovered.text
    body = recovered.json()
    assert body["cloud_id"] == "AIC-SLOT1" and body["activation_token"] and body["provisioning_qr_payload"] == f"AIC-SLOT1|{body['activation_token']}"
    assert "appliance.activation_token_recovered" in _audit_actions(db_path)
    # the old token is revoked; the new one activates
    assert _activate(client, old_token).status_code in (400, 401, 403)
    assert _activate(client, body["activation_token"]).status_code == 200


def test_recovery_is_refused_once_the_appliance_has_activated(client, db_path):
    _seed_tenant(db_path)
    token = _seed_and_activate_appliance(db_path)
    assert _activate(client, token).status_code == 200
    refused = _as(client).post(RECOVER)
    assert refused.status_code == 409
    tokens = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM appliance_activation_tokens WHERE appliance_id='appl-1' AND used_at IS NULL AND revoked_at IS NULL").fetchone()[0]
    assert tokens == 0  # nothing new was issued


def test_another_customer_and_household_members_cannot_recover(client, db_path):
    _seed_tenant(db_path)
    _seed_tenant(db_path, customer_id="cust-2", email="other@example.test", partner_id="partner-2")
    _seed_and_activate_appliance(db_path)
    assert _as(client, customer_id="cust-2", email="other@example.test").post(RECOVER).status_code == 404
    assert _as(client, role="customer_viewer").post(RECOVER).status_code in (401, 403)
    client.cookies.clear()
    assert client.post(RECOVER).status_code in (401, 403)
    live = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM appliance_activation_tokens WHERE appliance_id='appl-1' AND revoked_at IS NULL").fetchone()[0]
    assert live == 1  # the original token was never revoked by a refused caller


def test_the_provision_answer_says_whether_recovery_is_possible(client, db_path):
    _seed_tenant(db_path)
    token = _seed_and_activate_appliance(db_path)
    answer = _as(client).post("/api/customer/appliances/provision", json={}).json()
    assert answer["status"] == "already_provisioned" and answer["activation_pending"] is True and "activation_token" not in answer
    _activate(client, token)
    answer = _as(client).post("/api/customer/appliances/provision", json={}).json()
    assert answer["activation_pending"] is False and "activation_token" not in answer


# ------------------------------------------------------------------ setup completion

CONFIRM = "/api/customer/setup/confirm"


def _draft(db_path):
    con = sqlite3.connect(db_path)
    con.execute("INSERT OR IGNORE INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES('cust-1',6,'{}','2026-01-01')")
    con.commit()


def test_setup_cannot_be_completed_for_an_appliance_that_never_activated(client, db_path):
    _seed_tenant(db_path)
    _seed_and_activate_appliance(db_path)
    _draft(db_path)
    refused = _as(client).post(CONFIRM, json={"appliance_id": "appl-1"})
    assert refused.status_code == 409 and "has not activated yet" in refused.json()["detail"]
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT activation_status FROM appliances WHERE id='appl-1'").fetchone()[0] == "pending"
    assert con.execute("SELECT completed_at FROM customer_setup_drafts WHERE customer_id='cust-1'").fetchone()[0] is None


def test_setup_completes_after_device_activation_and_records_it_separately(client, db_path):
    _seed_tenant(db_path)
    token = _seed_and_activate_appliance(db_path)
    _draft(db_path)
    assert _activate(client, token).status_code == 200
    con = sqlite3.connect(db_path)
    activated_before = con.execute("SELECT activation_status FROM appliances WHERE id='appl-1'").fetchone()[0]
    done = _as(client).post(CONFIRM, json={"appliance_id": "appl-1"})
    assert done.status_code == 200, done.text
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT activation_status FROM appliances WHERE id='appl-1'").fetchone()[0] == activated_before == "activated"
    assert con.execute("SELECT completed_at,current_step FROM customer_setup_drafts WHERE customer_id='cust-1'").fetchone()[0]


def test_setup_completion_is_owner_only_and_tenant_scoped(client, db_path):
    _seed_tenant(db_path)
    _seed_tenant(db_path, customer_id="cust-2", email="other@example.test", partner_id="partner-2")
    token = _seed_and_activate_appliance(db_path)
    _activate(client, token)
    assert _as(client, role="customer_viewer").post(CONFIRM, json={"appliance_id": "appl-1"}).status_code in (401, 403)
    assert _as(client, customer_id="cust-2", email="other@example.test").post(CONFIRM, json={"appliance_id": "appl-1"}).status_code == 404
