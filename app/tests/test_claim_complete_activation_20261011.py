"""The real claim/complete route marks its appliance for claim-flow
activation, and the appliance's first authenticated heartbeat records that
activation once and asks build_orders to start the deferred storage plan
(2026-10-11; billing behavior itself: test_claim_storage_activation_20261011)."""
import pytest

from database_backend import override_target
from test_appliance_claims import (DEVICE_SECRET, _appliance_auth_headers, _claim_through_to_proof,  # noqa: F401
                                   client, claim_flow_key, db_path, identity_file)

with override_target(sqlite_path="/tmp/test_appliance_claims_import.db"):
    import appliance_cloud
    import build_orders
    from partner_db import connection


@pytest.fixture()
def starts(monkeypatch):
    calls = []
    monkeypatch.setattr(build_orders, "on_appliance_activated", lambda customer_id: calls.append(customer_id) or {"status": "ok"})
    return calls


def _appliance(db_path, appliance_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return dict(db.execute("SELECT * FROM appliances WHERE id=?", (appliance_id,)).fetchone())


def _heartbeat(client, body):
    appliance_cloud.request_limiter.events.clear()
    return client.post("/api/appliance/heartbeat", headers=_appliance_auth_headers(body["appliance_id"], body["credential"]),
                       json={"uptime_seconds": 120, "cpu": 5, "memory": 10})


def test_claim_complete_marks_the_row_and_the_first_heartbeat_activates_it_once(client, db_path, starts):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    body = client.post("/api/appliance/claim/complete",
                       json={"device_secret": DEVICE_SECRET, "claim_session_id": claim_session_id, "claim_proof": claim_proof}).json()
    appliance = _appliance(db_path, body["appliance_id"])
    assert appliance["activated_via"] == "claim" and appliance["activated_at"] is None
    assert appliance["activation_status"] == "pending" and starts == []

    assert _heartbeat(client, body).status_code == 200
    appliance = _appliance(db_path, body["appliance_id"])
    assert appliance["activated_at"] and appliance["activation_status"] == "pending"
    assert starts == ["cust-1"]
    assert _heartbeat(client, body).status_code == 200
    assert starts == ["cust-1"]  # recorded once
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            audits = db.execute("SELECT actor_role,details_json FROM audit_logs WHERE action='appliance.activated' AND entity_id=?",
                                (body["appliance_id"],)).fetchall()
    assert len(audits) == 1 and audits[0]["actor_role"] == "appliance"


def test_a_retried_claim_complete_never_activates_twice(client, db_path, starts):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    payload = {"device_secret": DEVICE_SECRET, "claim_session_id": claim_session_id, "claim_proof": claim_proof}
    body = client.post("/api/appliance/claim/complete", json=payload).json()
    assert _heartbeat(client, body).status_code == 200
    assert client.post("/api/appliance/claim/complete", json=payload).json() == body
    assert _heartbeat(client, body).status_code == 200
    assert starts == ["cust-1"]
