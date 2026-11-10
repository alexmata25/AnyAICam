"""Deterministic lease tests; synthetic signatures and no external services."""
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_gateway_remote_lease as lease

IDENTITY = {"appliance_id": "app-1", "cloud_id": "cloud-1", "customer_id": "cust-1"}
NONCE = "synthetic-request-nonce"
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def sign(body):
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(b"test-only", encoded, hashlib.sha256).hexdigest()


def make_doc(*, issued=NOW, expires=None, nonce=NONCE, **updates):
    body = {"type": lease.LEASE_TYPE, "version": 1, **IDENTITY,
            "request_nonce": nonce, "issued_at": issued.isoformat(),
            "expires_at": (expires or issued + timedelta(hours=24)).isoformat(), **updates}
    return {**body, "signature": sign(body)}


def fake_verify(body, signature, *, public_keys):
    if not hmac.compare_digest(sign(body), str(signature)):
        raise ValueError("invalid synthetic signature")


def verify_trusted(doc, nonce, *, now=None, for_new_grant=True):
    return lease.verify_lease(doc, {"test-key": "trusted"}, identity=IDENTITY, nonce=nonce, now=now)


class RemoteLeaseTests(unittest.TestCase):
    def setUp(self):
        lease.clear()
        self.addCleanup(lease.clear)
        fake_identity = SimpleNamespace(verify_signed_body=fake_verify)
        fake_entitlements = SimpleNamespace(_bound_identity=lambda x: x,
                                            _parse_time=lambda x: datetime.fromisoformat(x) if isinstance(x, str) else None)
        env = patch.dict(sys.modules, {"appliance_identity": fake_identity, "appliance_entitlements": fake_entitlements})
        env.start(); self.addCleanup(env.stop)
        trust = patch.object(lease, "_verify_trusted", side_effect=verify_trusted)
        trust.start(); self.addCleanup(trust.stop)

    def test_expiry_is_exact_and_restart_never_restores_cache(self):
        self.assertTrue(lease.accept(make_doc(), NONCE, now=NOW, monotonic=100))
        self.assertTrue(lease.allowed(now=NOW + timedelta(hours=23), monotonic=82900))
        self.assertFalse(lease.allowed(now=NOW + timedelta(hours=24), monotonic=86500))
        self.assertTrue(lease.accept(make_doc(), NONCE, now=NOW, monotonic=100))
        lease.clear()
        self.assertFalse(lease.allowed(now=NOW, monotonic=100))

    def test_clock_rollback_and_forward_never_extend(self):
        self.assertTrue(lease.accept(make_doc(), NONCE, now=NOW, monotonic=100))
        self.assertFalse(lease.allowed(now=NOW - timedelta(seconds=5), monotonic=110))
        self.assertTrue(lease.accept(make_doc(), NONCE, now=NOW, monotonic=100))
        self.assertFalse(lease.allowed(now=NOW + timedelta(hours=25), monotonic=101))

    def test_suspend_elapsed_time_cannot_extend_the_lease(self):
        self.assertTrue(lease.accept(make_doc(), NONCE, now=NOW, monotonic=100))
        # Simulates a sleeping system whose wall clock was set backwards
        # while the suspend-inclusive Windows monotonic clock advanced.
        self.assertFalse(lease.allowed(now=NOW, monotonic=100 + 25 * 3600))

    def test_nonce_identity_window_and_signature_fail_closed(self):
        for document, nonce in [
            (make_doc(), "different-request-nonce"),
            (make_doc(customer_id="other"), NONCE),
            (make_doc(expires=NOW + timedelta(hours=24, seconds=1)), NONCE),
            ({**make_doc(), "signature": "bad"}, NONCE),
        ]:
            with self.subTest(document=document):
                self.assertFalse(lease.accept(document, nonce, now=NOW, monotonic=100))

    def test_edge_accepts_only_a_valid_lease_and_cancellation_denies(self):
        import product_mode as pm
        current = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(pm, "STATE_FILE", Path(folder) / "mode.json"), \
                patch.object(pm, "_GATEWAY_REMOTE_DENIED", False), \
                patch.object(pm, "_LOCAL_GATEWAY_VERIFIED_THIS_PROCESS", False), \
                patch.dict("os.environ", {"ANYAICAM_GATEWAY_EDITION": "local_gateway",
                                          pm.PRODUCT_MODE_ENV: ""}):
            pm.apply_configuration_mode({"local_gateway_remote_access": True,
                                         "local_gateway_remote_lease": make_doc(issued=current),
                                         "_local_gateway_request_nonce": NONCE})
            self.assertTrue(pm.remote_access_authorized())
            pm.apply_configuration_mode({"local_gateway_remote_access": False})
            self.assertFalse(pm.remote_access_authorized())
            pm.apply_configuration_mode({"local_gateway_remote_access": True,
                                         "local_gateway_remote_lease": make_doc(issued=current),
                                         "_local_gateway_request_nonce": "wrong-request-nonce"})
            self.assertFalse(pm.remote_access_authorized())

    def test_renewal_requires_new_nonce_and_cancel_clears(self):
        self.assertTrue(lease.accept(make_doc(), NONCE, now=NOW, monotonic=100))
        later = NOW + timedelta(hours=1)
        fresh = "fresh-synthetic-request-nonce"
        self.assertFalse(lease.accept(make_doc(issued=later, nonce=NONCE), fresh,
                                      now=later, monotonic=3700))
        self.assertTrue(lease.accept(make_doc(issued=later, nonce=fresh), fresh,
                                     now=later, monotonic=3700))
        lease.clear()
        self.assertFalse(lease.allowed(now=later, monotonic=3700))


if __name__ == "__main__":
    unittest.main()
