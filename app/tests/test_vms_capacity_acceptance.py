"""VMS-side contract for staged versus root-accepted camera capacity."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives import serialization

import appliance_entitlements
import appliance_identity
import vms_capacity

SYSTEM_DIR = Path(__file__).resolve().parents[2] / "appliance-agent" / "system"
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))
import capacity_accept

IDENTITY = {"appliance_id": "appl-capacity-test", "cloud_id": "AIC-capacity-test", "customer_id": "cust-capacity-test"}


@pytest.fixture
def capacity_state(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_b64 = base64.b64encode(public).decode("ascii")
    anchor = {"cloud-key": public_b64}
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setattr(appliance_entitlements, "_local_identity", lambda: dict(IDENTITY))
    monkeypatch.setattr(appliance_entitlements, "_trusted_keys", lambda: anchor)
    monkeypatch.setattr(appliance_entitlements, "_anchor_file_problem", lambda _path: "")
    monkeypatch.setattr(vms_capacity, "STATE_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(vms_capacity, "SIGNED_FILE", tmp_path / "staging.json")
    monkeypatch.setattr(vms_capacity, "ACCEPTED_FILE", tmp_path / "protected" / "accepted.json")
    monkeypatch.setattr(vms_capacity, "FALLBACK_FILE", tmp_path / "protected" / "fallback.json")
    return {"private": private, "public": public, "keys": {"cloud-key": public}, "now": datetime.now(timezone.utc)}


def signed_snapshot(state, quantity, *, issued=None, expires=None):
    now = state["now"]
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
            "features": {},
            "camera_capacity": {"camera_slot_quantity": quantity, "plan_camera_slots": quantity,
                                "vms_license_capacity": quantity},
            "issued_at": (issued or now).isoformat(),
            "expires_at": (expires or now + timedelta(hours=12)).isoformat()}
    raw = state["private"].private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption())
    signature = appliance_identity.sign_body(body, key_id="cloud-key", private_key_b64=base64.b64encode(raw).decode())
    return {**body, "signature": signature}


def accept_in_test(state, snapshot, *, migration=False):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def verify(public, message, signature):
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
            return True
        except Exception:
            return False

    accepted, result = capacity_accept._advance(snapshot, state["keys"], IDENTITY, None,
                                                 now=state["now"], verifier=verify,
                                                 allow_expired_first=migration)
    vms_capacity.ACCEPTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    vms_capacity.ACCEPTED_FILE.write_text(json.dumps(accepted), encoding="utf-8")
    return result


def test_unsigned_legacy_capacity_and_staging_snapshot_never_grant_capacity(capacity_state):
    vms_capacity.STATE_FILE.write_text(json.dumps({"camera_slot_quantity": 64}), encoding="utf-8")
    assert vms_capacity.apply_sync({"camera_slot_quantity": 64}, None) == "absent"
    assert vms_capacity.load_capacity() is None
    snapshot = signed_snapshot(capacity_state, 8)
    assert vms_capacity.apply_sync({"camera_slot_quantity": 8}, snapshot) == "staged"
    assert vms_capacity.load_capacity() is None  # systemd/root acceptance has not promoted it


def test_only_verified_accepted_state_grants_capacity_and_remains_offline_after_expiry(capacity_state):
    now = capacity_state["now"]
    snapshot = signed_snapshot(capacity_state, 6, issued=now - timedelta(days=3),
                               expires=now - timedelta(days=2))
    accept_in_test(capacity_state, snapshot, migration=True)
    assert vms_capacity.load_capacity() == {"camera_slot_quantity": 6, "plan_camera_slots": 6,
                                            "vms_license_capacity": 6, "source": "signed"}


def test_accepted_capacity_remains_valid_after_its_signing_key_is_retired(capacity_state, monkeypatch):
    accept_in_test(capacity_state, signed_snapshot(capacity_state, 6))
    monkeypatch.setattr(appliance_entitlements, "_trusted_keys", lambda: {})
    assert vms_capacity.load_capacity() == {"camera_slot_quantity": 6, "plan_camera_slots": 6,
                                            "vms_license_capacity": 6, "source": "signed"}


def test_new_untrusted_staging_cannot_revoke_or_raise_last_accepted_limit(capacity_state):
    snapshot = signed_snapshot(capacity_state, 4)
    accept_in_test(capacity_state, snapshot)
    forged = dict(snapshot, camera_capacity={"camera_slot_quantity": 64})
    assert vms_capacity.apply_sync({"camera_slot_quantity": 64}, forged) == "staged"
    assert vms_capacity.load_capacity()["camera_slot_quantity"] == 4


def test_corrupt_accepted_state_fails_closed_instead_of_falling_back(capacity_state):
    vms_capacity.FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    vms_capacity.FALLBACK_FILE.write_text(json.dumps({"schema": 1, "source": "migrated_unsigned_capacity",
        "camera_slot_quantity": 8, **IDENTITY}), encoding="utf-8")
    vms_capacity.ACCEPTED_FILE.write_text("not-json", encoding="utf-8")
    assert vms_capacity.load_capacity() is None
