"""Claim device possession (2026-10-01 security fix, launch blocker).

Before: claim/begin returned an existing pending claim's claim_session_id to
anyone presenting the device's UUID; claim/status returned the
owner-confirmed proof for that session id alone; claim/complete then minted
-- or, on retry, recovered -- the permanent appliance credential. The
session id was also logged. Now a per-claim device secret, held only by the
appliance that opened the claim, is required for resume, status and
complete, and session ids are never logged."""
import logging

from test_appliance_claims import (  # noqa: F401 -- fixtures
    VALID_DEVICE_ID,
    _confirm,
    _customer_cookie,
    _seeded_customer,
    claim_flow_key,
    client,
    db_path,
    identity_file,
)

DEVICE = "device-secret-of-the-real-appliance-0123456789abcdef"
ATTACKER = "attacker-guess-of-the-device-secret-0123456789abcdef"


def _begin(client, secret=DEVICE, device_id=VALID_DEVICE_ID):
    return client.post("/api/appliance/claim/begin", json={"device_id": device_id, "device_secret": secret})


def _status(client, session_id, secret=DEVICE):
    return client.post("/api/appliance/claim/status", json={"claim_session_id": session_id, "device_secret": secret})


def _complete(client, session_id, proof, secret=DEVICE):
    return client.post("/api/appliance/claim/complete",
                       json={"claim_session_id": session_id, "claim_proof": proof, "device_secret": secret})


def _opened_and_confirmed(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client).json()
    assert _confirm(client, opened["claim_code"], "site-1", cookie=_customer_cookie()).status_code == 200
    return opened


def test_a_device_uuid_alone_never_returns_the_session(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    assert opened.status_code == 200 and opened.json()["claim_code"]
    stolen = _begin(client, secret=ATTACKER)
    assert stolen.status_code == 409
    assert "claim_session_id" not in stolen.text and opened.json()["claim_session_id"] not in stolen.text
    resumed = _begin(client)  # the real appliance, same secret: resumes
    assert resumed.status_code == 200 and resumed.json() == {**resumed.json(), "resumed": True,
                                                            "claim_session_id": opened.json()["claim_session_id"]}


def test_every_claim_call_requires_a_device_secret(client, db_path):
    _seeded_customer(db_path)
    assert client.post("/api/appliance/claim/begin", json={"device_id": VALID_DEVICE_ID}).status_code == 400
    assert _begin(client, secret="short").status_code == 400
    opened = _begin(client).json()
    assert client.post("/api/appliance/claim/status", json={"claim_session_id": opened["claim_session_id"]}).status_code == 400
    assert client.post("/api/appliance/claim/complete",
                       json={"claim_session_id": opened["claim_session_id"], "claim_proof": "x"}).status_code == 400


def test_a_leaked_session_id_cannot_poll_the_proof_or_complete(client, db_path):
    opened = _opened_and_confirmed(client, db_path)
    session_id = opened["claim_session_id"]
    attacker_poll = _status(client, session_id, secret=ATTACKER)
    assert attacker_poll.status_code == 200 and attacker_poll.json() == {"status": "expired"}
    real_poll = _status(client, session_id).json()
    assert real_poll["status"] == "claimed" and real_poll["claim_proof"]
    # Even holding the real proof (say, from a log), the wrong device secret fails.
    assert _complete(client, session_id, real_poll["claim_proof"], secret=ATTACKER).status_code == 403
    done = _complete(client, session_id, real_poll["claim_proof"])
    assert done.status_code == 200 and done.json()["credential"]


def test_the_real_appliance_can_still_recover_after_a_lost_response(client, db_path):
    opened = _opened_and_confirmed(client, db_path)
    proof = _status(client, opened["claim_session_id"]).json()["claim_proof"]
    first = _complete(client, opened["claim_session_id"], proof).json()
    retry = _complete(client, opened["claim_session_id"], proof)
    assert retry.status_code == 200 and retry.json()["credential"] == first["credential"]
    stolen = _complete(client, opened["claim_session_id"], proof, secret=ATTACKER)
    assert stolen.status_code == 403 and first["credential"] not in stolen.text


def test_claim_session_ids_and_proofs_are_never_logged(client, db_path, caplog):
    caplog.set_level(logging.DEBUG)
    opened = _opened_and_confirmed(client, db_path)
    proof = _status(client, opened["claim_session_id"]).json()["claim_proof"]
    credential = _complete(client, opened["claim_session_id"], proof).json()["credential"]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (opened["claim_session_id"], proof, credential, DEVICE, opened["claim_code"]):
        assert secret not in logged
