import base64
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
APP = ROOT / "app"
AGENT_SYSTEM = ROOT / "appliance-agent" / "system"
sys.path[:0] = [str(APP), str(AGENT_SYSTEM)]

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import appliance_identity
import capacity_accept

VERIFIER_PATH = HERE.parents[1] / "verify-direct-test-licensing.py"
SPEC = importlib.util.spec_from_file_location("direct_test_license_verifier", VERIFIER_PATH)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _documents():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_b64 = base64.b64encode(public).decode("ascii")
    now = datetime.now(timezone.utc)
    identity = {
        "appliance_id": "TEST-appliance-1", "cloud_id": "TEST-cloud-1",
        "customer_id": "TEST-customer-1", "site_id": "TEST-site-1",
        "partner_id": "TEST-partner-1", "credential": "test-synthetic-credential-01",
    }
    registry = {
        "type": "anyaicam.entitlement_trust_registry", "version": 1,
        "environment": "synthetic", "generation": 1, "issued_at": now.isoformat(),
        "keys": {"synthetic-key-1": {
            "public_key": public_b64, "state": "active", "purposes": ["capacity", "features"],
            "environment": "synthetic", "changed_generation": 1,
        }},
    }
    body = {
        "type": "anyaicam.feature_entitlements", "version": 1,
        "appliance_id": identity["appliance_id"], "cloud_id": identity["cloud_id"],
        "customer_id": identity["customer_id"], "features": {"talk_down": False, "voice_call": False},
        "camera_capacity": {"camera_slot_quantity": 2, "plan_camera_slots": 2, "vms_license_capacity": 2},
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=2)).isoformat(),
    }
    signed = {**body, "signature": appliance_identity.sign_body(
        body, key_id="synthetic-key-1",
        private_key_b64=base64.b64encode(private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode("ascii"),
    )}
    digest = hashlib.sha256(capacity_accept._canonical(body)).hexdigest()
    accepted = capacity_accept._state_record(
        signed, {key: identity[key] for key in ("appliance_id", "cloud_id", "customer_id")},
        now.isoformat(), digest, now.isoformat(), public,
        key_id="synthetic-key-1", trust_generation=1,
    )
    return identity, signed, accepted, registry


def _run(tmp_path, docs):
    identity, feature, capacity, registry = docs
    paths = []
    for name, obj in (("identity", identity), ("feature", feature), ("capacity", capacity), ("registry", registry)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        paths.append(str(path))
    return verifier.main([*paths, str(APP), str(AGENT_SYSTEM)])


def test_signed_synthetic_feature_and_capacity_fixtures_verify(tmp_path):
    assert _run(tmp_path, _documents()) == 0


def test_tampered_synthetic_feature_fixture_is_rejected(tmp_path):
    docs = list(_documents())
    docs[1] = {**docs[1], "features": {"talk_down": True, "voice_call": True}}
    with pytest.raises(ValueError, match="synthetic feature entitlement refused: bad_signature"):
        _run(tmp_path, tuple(docs))
