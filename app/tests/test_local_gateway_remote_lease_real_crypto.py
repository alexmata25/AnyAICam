"""Real Ed25519 lease and trust-state checks; never contacts a provider."""
import base64
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_gateway_remote_lease as lease

HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
IDENTITY = {"appliance_id": "app-1", "cloud_id": "cloud-1", "customer_id": "cust-1"}
NONCE = "synthetic-request-nonce"
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


@unittest.skipUnless(HAS_CRYPTO, "cryptography dependency not installed")
class RealLeaseCryptoTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
        import appliance_identity as identity
        import appliance_entitlements as entitlements
        self.identity, self.entitlements = identity, entitlements
        self.keys = {}
        self.state = {}
        for name in ("key-1", "key-2"):
            key = Ed25519PrivateKey.generate()
            private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            self.keys[name] = (base64.b64encode(private).decode(), public)
            self.state[name] = "active"
        def synthetic_trust_file(path):
            if path == entitlements.TRUST_STATE_FILE:
                return None, None
            if path != entitlements.TRUST_REGISTRY_FILE:
                raise AssertionError("unexpected trust file")
            registry = {
                "type": entitlements.TRUST_REGISTRY_TYPE, "version": 1,
                "environment": "test", "generation": 1, "issued_at": NOW.isoformat(),
                "keys": {name: {"public_key": base64.b64encode(key[1]).decode(),
                                "state": self.state[name], "purposes": ["features"],
                                "environment": "test", "changed_generation": 1,
                                "appliance_ids": [IDENTITY["appliance_id"]]}
                         for name, key in self.keys.items()}}
            return entitlements.canonical_trust_registry(registry), None
        files = patch.object(entitlements, "_read_trust_file", side_effect=synthetic_trust_file)
        files.start(); self.addCleanup(files.stop)
        floor = patch.object(entitlements, "_accepted_trust_generation", return_value=0)
        floor.start(); self.addCleanup(floor.stop)
        bound = patch.object(entitlements, "_local_identity", return_value=IDENTITY)
        bound.start(); self.addCleanup(bound.stop)
        local_id = patch.object(entitlements, "_local_appliance_id", return_value=IDENTITY["appliance_id"])
        local_id.start(); self.addCleanup(local_id.stop)
        lease.clear(); self.addCleanup(lease.clear)

    def document(self, key_id):
        body = {"type": lease.LEASE_TYPE, "version": 1, **IDENTITY,
                "request_nonce": NONCE, "issued_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(hours=24)).isoformat()}
        return {**body, "signature": self.identity.sign_body(
            body, key_id=key_id, private_key_b64=self.keys[key_id][0])}

    def test_real_signature_tampering_rotation_and_revocation(self):
        old = self.document("key-1")
        self.assertTrue(lease.accept(old, NONCE, now=NOW, monotonic=1))
        self.state["key-1"] = "retired"
        self.assertTrue(lease.allowed(now=NOW, monotonic=1))
        self.assertFalse(lease.accept(old, NONCE, now=NOW, monotonic=1))
        new = self.document("key-2")
        self.assertTrue(lease.accept(new, NONCE, now=NOW, monotonic=1))
        self.state["key-2"] = "revoked"
        self.assertFalse(lease.allowed(now=NOW, monotonic=1))
        self.state["key-2"] = "active"
        self.assertFalse(lease.accept({**new, "customer_id": "tampered"}, NONCE,
                                      now=NOW, monotonic=1))


    def test_real_signature_rejects_expired_lease(self):
        doc = self.document("key-1")
        self.assertFalse(lease.accept(doc, NONCE, now=NOW + timedelta(hours=24), monotonic=1))

    def test_real_signature_rejects_lease_over_24_hours(self):
        doc = self.document("key-1")
        body = {key: value for key, value in doc.items() if key != "signature"}
        body["expires_at"] = (NOW + timedelta(hours=24, seconds=1)).isoformat()
        signature = self.identity.sign_body(body, key_id="key-1", private_key_b64=self.keys["key-1"][0])
        self.assertFalse(lease.accept({**body, "signature": signature}, NONCE, now=NOW, monotonic=1))


if __name__ == "__main__":
    unittest.main()
