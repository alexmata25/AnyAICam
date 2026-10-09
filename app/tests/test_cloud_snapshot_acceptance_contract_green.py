"""Cloud-issued capacity snapshots against the appliance acceptance contract
(Green, 2026-10-09; contract: docs/signed-camera-capacity-acceptance.md and
appliance-agent/system/capacity_accept.py from 90d6eb0).

Snapshots come from the real cloud configuration endpoint (billing in the
cloud database) and go through the root helper's own policy functions:
_validate_snapshot, _advance and _validate_state. The helper verifies with
/usr/bin/openssl on the appliance; here an equivalent Ed25519 verifier
(cryptography) is injected, as the helper's own tests do.
"""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from test_appliance_feature_entitlements import (  # noqa: F401 -- fixtures and helpers
    EDGE_IDENTITY, _cloud_public_keys, _resign, _sync, appliance, cloud_client, cloud_db, connection, edge_db, edge_env,
    override_target)

pytestmark = pytest.mark.real_entitlements

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "appliance-agent" / "system"))
import capacity_accept as helper  # noqa: E402


def _verify(key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(signature, message)
        return True
    except InvalidSignature:
        return False


@pytest.fixture()
def cloud(appliance, cloud_db, monkeypatch):
    """The cloud's billing answer (camera plan AND license) and the snapshots
    its configuration endpoint issues to this appliance."""
    import customer_entitlements
    limit = {"slots": 3, "plan": 3, "license": 64}
    monkeypatch.setattr(customer_entitlements, "capacity_breakdown", lambda customer_id: {
        "camera_slot_quantity": limit["slots"], "plan_camera_slots": limit["plan"],
        "vms_license_capacity": limit["license"], "vms_license_required": limit["license"] <= 0})
    issued = []
    appliance["rewrite"] = lambda response: issued.append(response["feature_entitlements"]) or response
    return {"limit": limit, "issued": issued, "keys": helper._validate_keyset({"keys": _cloud_public_keys(cloud_db)})}


def _issue(cloud, edge_db, **limit):
    cloud["limit"].update(limit)
    _sync(edge_db)
    return cloud["issued"][-1]


def _identity(**changes):
    return {**EDGE_IDENTITY, **changes}


# ------------------------------------------------------------------ schema, keys, identity

@pytest.mark.parametrize("slots,plan,license", [(0, 0, 0), (1, 1, 64), (3, 3, 64), (64, 64, 64), (2, 8, 2), (0, 5, 0)])
def test_every_cloud_snapshot_is_accepted_by_the_helper(cloud, edge_db, slots, plan, license):
    snapshot = _issue(cloud, edge_db, slots=slots, plan=plan, license=license)
    body, issued_at, digest = helper._validate_snapshot(snapshot, cloud["keys"], _identity(), verifier=_verify)
    assert body["camera_capacity"] == {"camera_slot_quantity": slots, "plan_camera_slots": plan, "vms_license_capacity": license}
    assert body["type"] == helper.SNAPSHOT_TYPE and body["version"] == helper.SNAPSHOT_VERSION
    assert set(body) == {"type", "version", "appliance_id", "cloud_id", "customer_id", "features",
                         "camera_capacity", "issued_at", "expires_at"}
    assert len(digest) == 64 and datetime.fromisoformat(issued_at).tzinfo is not None


def test_cloud_capacity_above_edge_bound_is_clamped_before_signing(cloud, edge_db):
    """Every signed component stays within the helper's supported 10,000 slots."""
    snapshot = _issue(cloud, edge_db, slots=3, plan=10_001)
    assert snapshot["camera_capacity"] == {
        "camera_slot_quantity": 3,
        "plan_camera_slots": 10_000,
        "vms_license_capacity": 64,
    }
    body, _, _ = helper._validate_snapshot(snapshot, cloud["keys"], _identity(), verifier=_verify)
    assert body["camera_capacity"]["plan_camera_slots"] <= helper.MAX_CAMERA_SLOTS


def test_only_the_trusted_cloud_keys_verify(cloud, edge_db):
    snapshot = _issue(cloud, edge_db)
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    untrusted = helper._validate_keyset({"keys": {"someone-else": other}})
    with pytest.raises(helper.Reject, match="bad_signature"):
        helper._validate_snapshot(snapshot, untrusted, _identity(), verifier=_verify)
    tampered = json.loads(json.dumps(snapshot))
    tampered["camera_capacity"]["camera_slot_quantity"] = 64
    with pytest.raises(helper.Reject, match="bad_signature"):
        helper._validate_snapshot(tampered, cloud["keys"], _identity(), verifier=_verify)


@pytest.mark.parametrize("field,value", [("appliance_id", "appl-other"), ("cloud_id", "AIC-OTHER"), ("customer_id", "cust-other")])
def test_a_snapshot_for_another_appliance_or_tenant_is_rejected(cloud, edge_db, field, value):
    snapshot = _issue(cloud, edge_db)
    with pytest.raises(helper.Reject, match="identity_mismatch"):
        helper._validate_snapshot(snapshot, cloud["keys"], _identity(**{field: value}), verifier=_verify)


# ------------------------------------------------------------------ expiry and offline

def test_a_new_snapshot_expires_on_arrival_but_an_accepted_one_holds_offline(cloud, edge_db):
    snapshot = _issue(cloud, edge_db)
    issued = datetime.fromisoformat(snapshot["issued_at"])
    expires = datetime.fromisoformat(snapshot["expires_at"])
    assert timedelta(hours=1) <= expires - issued <= timedelta(hours=168)  # ANYAICAM_ENTITLEMENT_SNAPSHOT_TTL_HOURS
    later = expires + timedelta(days=30)
    with pytest.raises(helper.Reject, match="expired"):
        helper._advance(snapshot, cloud["keys"], _identity(), None, now=later, verifier=_verify)
    state, outcome = helper._advance(snapshot, cloud["keys"], _identity(), None, now=issued, verifier=_verify)
    assert outcome == "accepted"
    identity, high_water = helper._validate_state(state, cloud["keys"], now=later, verifier=_verify)  # offline a month
    assert identity == {field: EDGE_IDENTITY[field] for field in helper.BOUND_FIELDS}
    # Only the root-run installer/Software Update migration may accept an expired FIRST snapshot.
    assert helper._advance(snapshot, cloud["keys"], _identity(), None, now=later, verifier=_verify,
                           allow_expired_first=True)[1] == "accepted"


def test_a_snapshot_issued_in_the_future_is_rejected(cloud, edge_db, cloud_db):
    snapshot = _issue(cloud, edge_db)
    issued = datetime.fromisoformat(snapshot["issued_at"])
    with pytest.raises(helper.Reject, match="issued_in_future"):
        helper._advance(snapshot, cloud["keys"], _identity(), None, now=issued - timedelta(hours=1), verifier=_verify)


# ------------------------------------------------------------------ ordering and capacity changes

def test_successive_cloud_snapshots_advance_in_order_and_follow_plan_changes(cloud, edge_db):
    s1 = _issue(cloud, edge_db, slots=3)
    s2 = _issue(cloud, edge_db, slots=6, plan=6)
    s3 = _issue(cloud, edge_db, slots=1, plan=1)
    assert s1["issued_at"] < s2["issued_at"] < s3["issued_at"]  # each sync issues a strictly newer snapshot
    now = datetime.fromisoformat(s3["issued_at"])
    state = None
    for snapshot, quantity in ((s1, 3), (s2, 6), (s3, 1)):
        state, outcome = helper._advance(snapshot, cloud["keys"], _identity(), state, now=now, verifier=_verify)
        assert outcome == "accepted" and state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == quantity
    with pytest.raises(helper.Reject, match="replay"):
        helper._advance(s1, cloud["keys"], _identity(), state, now=now, verifier=_verify)
    assert helper._advance(s3, cloud["keys"], _identity(), state, now=now, verifier=_verify)[1] == "unchanged"


def test_a_different_snapshot_with_the_same_issue_time_is_rejected(cloud, edge_db, cloud_db):
    s1 = _issue(cloud, edge_db)
    twin = _resign(s1, cloud_db, camera_capacity={**s1["camera_capacity"], "camera_slot_quantity": 9})
    now = datetime.fromisoformat(s1["issued_at"])
    state, _ = helper._advance(s1, cloud["keys"], _identity(), None, now=now, verifier=_verify)
    with pytest.raises(helper.Reject, match="same_timestamp_conflict"):
        helper._advance(twin, cloud["keys"], _identity(), state, now=now, verifier=_verify)


# ------------------------------------------------------------------ key rotation

def _rotate(cloud_db):
    """A new cloud signing key, newest wins (appliance_identity.ensure_signing_key)."""
    import appliance_identity
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            old = appliance_identity.ensure_signing_key(db)
            db.execute("UPDATE identity_signing_keys SET created_at='2000-01-01' WHERE key_id=?", (old["key_id"],))
            private = Ed25519PrivateKey.generate()
            db.execute("INSERT INTO identity_signing_keys(key_id,public_key_b64,private_key_b64,created_at) VALUES(?,?,?,?)",
                       ("anyaicam-identity-rotated", base64.b64encode(private.public_key().public_bytes_raw()).decode(),
                        base64.b64encode(private.private_bytes_raw()).decode(), datetime.now().isoformat()))
            return old["key_id"], appliance_identity.active_public_keys(db)


def test_key_rotation_with_both_keys_in_the_trust_anchor_keeps_accepting(cloud, edge_db, cloud_db):
    before = _issue(cloud, edge_db)
    old_id, both = _rotate(cloud_db)
    after = _issue(cloud, edge_db, slots=5, plan=5)
    assert before["signature"]["key_id"] == old_id and after["signature"]["key_id"] == "anyaicam-identity-rotated"
    keys = helper._validate_keyset({"keys": both})
    now = datetime.fromisoformat(after["issued_at"])
    state, _ = helper._advance(before, keys, _identity(), None, now=now, verifier=_verify)
    state, outcome = helper._advance(after, keys, _identity(), state, now=now, verifier=_verify)
    assert outcome == "accepted" and state["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 5


def test_a_rotated_key_not_yet_in_the_trust_anchor_keeps_the_previous_authorization(cloud, edge_db, cloud_db):
    before = _issue(cloud, edge_db)
    old_only = dict(cloud["keys"])
    _rotate(cloud_db)
    after = _issue(cloud, edge_db, slots=5, plan=5)
    now = datetime.fromisoformat(after["issued_at"])
    state, _ = helper._advance(before, old_only, _identity(), None, now=now, verifier=_verify)
    with pytest.raises(helper.Reject, match="bad_signature"):
        helper._advance(after, old_only, _identity(), state, now=now, verifier=_verify)
    assert helper._validate_state(state, old_only, now=now, verifier=_verify)  # previous authorization intact


def test_retiring_the_old_key_preserves_appliance_continuity(cloud, edge_db, cloud_db):
    """The accepted state retains its verified key while the next snapshot
    is verified with the currently trusted rotated key."""
    before = _issue(cloud, edge_db)
    _, both = _rotate(cloud_db)
    after = _issue(cloud, edge_db, slots=5, plan=5)
    new_only = helper._validate_keyset({"keys": {"anyaicam-identity-rotated": both["anyaicam-identity-rotated"]}})
    now = datetime.fromisoformat(after["issued_at"])
    state, _ = helper._advance(before, helper._validate_keyset({"keys": both}), _identity(), None, now=now, verifier=_verify)
    advanced, outcome = helper._advance(after, new_only, _identity(), state, now=now, verifier=_verify)
    assert outcome == "accepted"
    assert advanced["snapshot"] == after
    assert base64.b64decode(advanced["accepted_key"]) == new_only[after["signature"]["key_id"]]
    assert helper._validate_state(advanced, new_only, now=now, verifier=_verify)


# ------------------------------------------------------------------ ownership transfer

def test_a_reclaimed_appliance_cannot_accept_its_new_owners_capacity(cloud, edge_db, cloud_db):
    """CONTRACT GAP (reported to Codex/owner): after coordinated_reenroll()
    binds the appliance to another customer, the accepted state's identity
    rejects every snapshot for the new owner, and the VMS ignores the old
    state (identity mismatch): cameras fail closed until a repair/reset."""
    first = _issue(cloud, edge_db)
    now = datetime.fromisoformat(first["issued_at"])
    state, _ = helper._advance(first, cloud["keys"], _identity(), None, now=now, verifier=_verify)
    new_owner = _resign(first, cloud_db, customer_id="cust-new-owner",
                        issued_at=(now + timedelta(minutes=5)).isoformat(),
                        expires_at=(now + timedelta(hours=24)).isoformat())
    with pytest.raises(helper.Reject, match="accepted_identity_changed"):
        helper._advance(new_owner, cloud["keys"], _identity(customer_id="cust-new-owner"), state,
                        now=now + timedelta(minutes=6), verifier=_verify)
