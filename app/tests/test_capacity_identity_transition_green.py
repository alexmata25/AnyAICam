"""Signed capacity identity transitions end to end (Green, 2026-10-09,
independent validation of Codex 9be78ab).

Cloud side: the real configuration endpoint issues the snapshot, with the
transition stored the way the claim/activation completion and the global-
admin endpoint store it (feature_entitlements.*_capacity_identity_transition).
Appliance side: the root helper's own policy (capacity_accept._advance with
the protected installer pin as `anchor`, and _validate_state), verified with
an Ed25519 verifier equivalent to the helper's openssl call.
"""
import base64
import json
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_appliance_feature_entitlements import (  # noqa: F401 -- fixtures and helpers
    EDGE_IDENTITY, _cloud_public_keys, _sync, appliance, cloud_client, cloud_db, connection, edge_db, edge_env,
    override_target)
from test_aac_voice_call_cloud_edge_flow import APPLIANCE_ID, _auth_headers
from test_cloud_snapshot_acceptance_contract_green import _rotate, _verify, helper

pytestmark = pytest.mark.real_entitlements

INSTALLER_UUID = "123e4567-e89b-42d3-a456-426614174000"
BOUND = ("appliance_id", "cloud_id", "customer_id")


@pytest.fixture()
def cloud(appliance, cloud_db, monkeypatch):
    import customer_entitlements
    limit = {"slots": 3}
    monkeypatch.setattr(customer_entitlements, "capacity_breakdown", lambda customer_id: {
        "camera_slot_quantity": limit["slots"], "plan_camera_slots": limit["slots"], "vms_license_capacity": 64,
        "vms_license_required": False})
    issued = []
    appliance["rewrite"] = lambda response: issued.append(response["feature_entitlements"]) or response
    return {"limit": limit, "issued": issued}


def _keys(cloud_db):
    return helper._validate_keyset({"keys": _cloud_public_keys(cloud_db)})


def _cloud(cloud_db, sql, args=()):
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None


def _appliance_row(cloud_db):
    return _cloud(cloud_db, "SELECT id,cloud_id,customer_id,capacity_identity_transition_json FROM appliances WHERE id=?",
                  (APPLIANCE_ID,))


def _store_initial_claim(cloud_db, source=INSTALLER_UUID):
    """What claim/activation completion stores (appliance_claims / appliance_cloud)."""
    import feature_entitlements
    row = _appliance_row(cloud_db)
    transition = feature_entitlements.initial_capacity_identity_transition(source, row)
    _cloud(cloud_db, "UPDATE appliances SET capacity_identity_transition_json=? WHERE id=?",
           (json.dumps(transition, sort_keys=True, separators=(",", ":")), APPLIANCE_ID))
    return transition


def _store_authorized(cloud_db, from_identity, reason="ownership_transfer"):
    """What the global-admin endpoint stores."""
    import feature_entitlements
    transition = feature_entitlements.authorized_capacity_identity_transition(from_identity, _appliance_row(cloud_db), reason=reason)
    _cloud(cloud_db, "UPDATE appliances SET capacity_identity_transition_json=? WHERE id=?",
           (json.dumps(transition, sort_keys=True, separators=(",", ":")), APPLIANCE_ID))
    return transition


def _issue(cloud, edge_db, **limit):
    cloud["limit"].update(limit)
    _sync(edge_db)
    return cloud["issued"][-1]


def _advance(snapshot, keys, identity, state, *, pin=INSTALLER_UUID):
    now = datetime.fromisoformat(snapshot["issued_at"])
    return helper._advance(snapshot, keys, identity, state, now=now, verifier=_verify,
                           anchor={"appliance_id": pin})


def _identity(**changes):
    return {**{field: EDGE_IDENTITY[field] for field in BOUND}, **changes}


# ------------------------------------------------------------------ fresh claim (C1)

def test_a_fresh_claim_is_accepted_through_the_signed_initial_transition(cloud, cloud_db, edge_db):
    transition = _store_initial_claim(cloud_db)
    snapshot = _issue(cloud, edge_db)
    assert snapshot["identity_transition"] == transition
    state, outcome = _advance(snapshot, _keys(cloud_db), _identity(), None)
    assert outcome == "accepted" and state["schema"] == 2 and state["transition_id"] == transition["transition_id"]
    assert state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 3
    # Later refreshes keep the same transition; the pin is now the cloud identity.
    refreshed = _issue(cloud, edge_db, slots=5)
    state, outcome = _advance(refreshed, _keys(cloud_db), _identity(), state, pin=EDGE_IDENTITY["appliance_id"])
    assert outcome == "accepted" and state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 5


def test_without_a_signed_initial_transition_a_fresh_install_is_still_refused(cloud, cloud_db, edge_db):
    snapshot = _issue(cloud, edge_db)  # e.g. legacy activation without a provisional UUID
    with pytest.raises(helper.Reject, match="identity_anchor_mismatch"):
        _advance(snapshot, _keys(cloud_db), _identity(), None)


def test_an_initial_transition_from_another_devices_uuid_is_refused(cloud, cloud_db, edge_db):
    _store_initial_claim(cloud_db, source="99999999-e89b-42d3-a456-426614174000")
    snapshot = _issue(cloud, edge_db)
    with pytest.raises(helper.Reject, match="identity_anchor_mismatch"):
        _advance(snapshot, _keys(cloud_db), _identity(), None)


# ------------------------------------------------------------------ re-enrollment / ownership transfer

def _transfer_on_cloud(cloud_db, new_customer="cust-2"):
    _cloud(cloud_db, "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (new_customer, "partner-1", "New owner", f"{new_customer}@example.test",
                                              "active", "real", "2026-10-09"))
    _cloud(cloud_db, "UPDATE appliances SET customer_id=? WHERE id=?", (new_customer, APPLIANCE_ID))


def _accepted_after_claim(cloud, cloud_db, edge_db):
    _store_initial_claim(cloud_db)
    return _advance(_issue(cloud, edge_db), _keys(cloud_db), _identity(), None)[0]


def test_configuration_sync_keeps_working_between_a_transfer_and_its_authorization(cloud, cloud_db, edge_db, cloud_client):
    """BLOCKER CHECK: the stored initial-claim transition no longer matches the
    row once the customer changes; appliance_snapshot() raises ValueError and
    the configuration endpoint has no handler for it."""
    _accepted_after_claim(cloud, cloud_db, edge_db)
    _transfer_on_cloud(cloud_db)
    response = cloud_client.get("/api/appliance/configuration", headers=_auth_headers())
    assert response.status_code == 200, f"configuration sync answered {response.status_code} until an admin authorizes"


def test_an_admin_authorized_transfer_is_accepted_and_an_unauthorized_one_is_not(cloud, cloud_db, edge_db, monkeypatch):
    state = _accepted_after_claim(cloud, cloud_db, edge_db)
    old_identity = _identity()
    _transfer_on_cloud(cloud_db)
    new_identity = _identity(customer_id="cust-2")
    monkeypatch.setattr("appliance_activation.load_persisted_identity",
                        lambda: {**EDGE_IDENTITY, "customer_id": "cust-2"})
    _cloud(cloud_db, "UPDATE appliances SET capacity_identity_transition_json=NULL WHERE id=?", (APPLIANCE_ID,))
    unauthorized = _issue(cloud, edge_db, slots=2)
    with pytest.raises(helper.Reject, match="accepted_identity_changed"):
        _advance(unauthorized, _keys(cloud_db), new_identity, state, pin=old_identity["appliance_id"])
    transition = _store_authorized(cloud_db, old_identity)
    authorized = _issue(cloud, edge_db, slots=2)
    assert authorized["identity_transition"] == transition
    state, outcome = _advance(authorized, _keys(cloud_db), new_identity, state, pin=old_identity["appliance_id"])
    assert outcome == "accepted" and state["identity"] == new_identity and state["transition_id"] == transition["transition_id"]
    assert state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 2


def test_an_authorized_transition_naming_the_wrong_previous_identity_is_refused(cloud, cloud_db, edge_db, monkeypatch):
    state = _accepted_after_claim(cloud, cloud_db, edge_db)
    _transfer_on_cloud(cloud_db)
    monkeypatch.setattr("appliance_activation.load_persisted_identity",
                        lambda: {**EDGE_IDENTITY, "customer_id": "cust-2"})
    _store_authorized(cloud_db, _identity(customer_id="someone-else"))
    snapshot = _issue(cloud, edge_db)
    with pytest.raises(helper.Reject, match="accepted_identity_changed"):
        _advance(snapshot, _keys(cloud_db), _identity(customer_id="cust-2"), state, pin=EDGE_IDENTITY["appliance_id"])


def test_replayed_identity_snapshots_are_refused(cloud, cloud_db, edge_db, monkeypatch):
    claim_snapshot = _issue(cloud, edge_db) if False else None  # noqa: F841 (readability)
    _store_initial_claim(cloud_db)
    first = _issue(cloud, edge_db)
    state, _ = _advance(first, _keys(cloud_db), _identity(), None)
    _transfer_on_cloud(cloud_db)
    monkeypatch.setattr("appliance_activation.load_persisted_identity",
                        lambda: {**EDGE_IDENTITY, "customer_id": "cust-2"})
    _store_authorized(cloud_db, _identity())
    moved = _issue(cloud, edge_db)
    state, _ = _advance(moved, _keys(cloud_db), _identity(customer_id="cust-2"), state, pin=EDGE_IDENTITY["appliance_id"])
    # The old owner's snapshot (older, other identity) and the same transfer again are refused.
    with pytest.raises(helper.Reject):
        _advance(first, _keys(cloud_db), _identity(), state, pin=EDGE_IDENTITY["appliance_id"])
    assert _advance(moved, _keys(cloud_db), _identity(customer_id="cust-2"), state,
                    pin=EDGE_IDENTITY["appliance_id"])[1] == "unchanged"


# ------------------------------------------------------------------ key retirement and offline continuity

def test_an_accepted_capacity_survives_retirement_of_its_signing_key_offline(cloud, cloud_db, edge_db):
    _store_initial_claim(cloud_db)
    before = _issue(cloud, edge_db)
    _, both = _rotate(cloud_db)
    state, _ = _advance(before, helper._validate_keyset({"keys": both}), _identity(), None)
    new_only = helper._validate_keyset({"keys": {"anyaicam-identity-rotated": both["anyaicam-identity-rotated"]}})
    far = datetime.fromisoformat(before["expires_at"]) + timedelta(days=60)
    identity, _ = helper._validate_state(state, new_only, now=far, verifier=_verify)  # offline, key retired
    assert identity == _identity()
    after = _issue(cloud, edge_db, slots=7)
    state, outcome = _advance(after, new_only, _identity(), state, pin=EDGE_IDENTITY["appliance_id"])
    assert outcome == "accepted" and state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 7


def test_a_new_snapshot_still_needs_a_key_in_the_active_trust_anchor(cloud, cloud_db, edge_db):
    _store_initial_claim(cloud_db)
    first = _issue(cloud, edge_db)
    state, _ = _advance(first, _keys(cloud_db), _identity(), None)
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    with pytest.raises(helper.Reject, match="bad_signature"):
        _advance(_issue(cloud, edge_db), helper._validate_keyset({"keys": {"other": other}}), _identity(), state,
                 pin=EDGE_IDENTITY["appliance_id"])


def test_an_edited_accepted_state_key_does_not_validate(cloud, cloud_db, edge_db):
    _store_initial_claim(cloud_db)
    state, _ = _advance(_issue(cloud, edge_db), _keys(cloud_db), _identity(), None)
    forged = json.loads(json.dumps(state))
    forged["accepted_key"] = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    with pytest.raises(helper.Reject):
        helper._validate_state(forged, _keys(cloud_db), verifier=_verify)


# ------------------------------------------------------------------ capacity bound

def test_the_cloud_clamps_capacity_to_the_10000_camera_bound(cloud, cloud_db, edge_db):
    _store_initial_claim(cloud_db)
    snapshot = _issue(cloud, edge_db, slots=50_000)
    assert snapshot["camera_capacity"] == {"camera_slot_quantity": 10_000, "plan_camera_slots": 10_000,
                                           "vms_license_capacity": 64}
    state, _ = _advance(snapshot, _keys(cloud_db), _identity(), None)
    assert state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 10_000


def test_a_1_2_4_appliance_claimed_after_the_cloud_deploy_then_upgraded_is_accepted(cloud, cloud_db, edge_db):
    """BLOCKER CHECK (Red's N1): a 1.2.4 appliance pins its cloud identity on
    upgrade (its activation identity file exists), but its snapshots carry the
    initial_claim transition stored at claim, whose source is the device UUID."""
    _store_initial_claim(cloud_db)
    snapshot = _issue(cloud, edge_db)
    assert snapshot["identity_transition"]["to_identity"] == _identity()
    state, outcome = _advance(snapshot, _keys(cloud_db), _identity(), None, pin=EDGE_IDENTITY["appliance_id"])
    assert outcome == "accepted"
