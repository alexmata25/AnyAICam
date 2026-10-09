"""Focused tests for the root-only signed camera-capacity acceptance policy."""
import base64
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system"))

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

from capacity_accept import (Reject, _advance, _validate_state, _write_atomic_at, _openssl_verify,
                             _canonical, _check_identity_anchor, _bootstrap_identity, IDENTITY_ANCHOR_FILE,
                             accept_pending)


IDENTITY = {"appliance_id": "appl-test", "cloud_id": "AIC-test", "customer_id": "cust-test"}
NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


def make_snapshot(private, *, issued=None, expires=None, quantity=8, **changes):
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
            "features": {"talk_down": False, "voice_call": False},
            "camera_capacity": {"camera_slot_quantity": quantity, "plan_camera_slots": quantity,
                                "vms_license_capacity": 64},
            "issued_at": (issued or NOW).isoformat(),
            "expires_at": (expires or NOW + dt.timedelta(hours=24)).isoformat()}
    body.update(changes)
    signature = base64.urlsafe_b64encode(private.sign(_canonical(body))).decode("ascii").rstrip("=")
    return {**body, "signature": {"alg": "Ed25519", "key_id": "cloud-key", "value": signature}}


def verifier_for(public_key, public_bytes):
    def verify(key, message, signature):
        if key != public_bytes:
            return False
        try:
            public_key.verify(signature, message)
            return True
        except InvalidSignature:
            return False
    return verify


class CapacityAcceptancePolicyTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)
        self.keys = {"cloud-key": self.public}
        self.verify = verifier_for(self.private.public_key(), self.public)

    def test_valid_snapshot_is_accepted_with_bound_identity_and_high_water(self):
        snapshot = make_snapshot(self.private)
        state, outcome = _advance(snapshot, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(state["identity"], IDENTITY)
        self.assertEqual(state["snapshot"], snapshot)
        self.assertEqual(state["high_water"]["issued_at"], NOW.isoformat())
        self.assertEqual(len(state["high_water"]["digest"]), 64)
        _validate_state(state, self.keys, now=NOW, verifier=self.verify)

    def test_older_higher_capacity_snapshot_is_rejected_after_lower_update(self):
        recent = make_snapshot(self.private, issued=NOW, quantity=4)
        accepted, _ = _advance(recent, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        replay = make_snapshot(self.private, issued=NOW - dt.timedelta(days=1), quantity=64,
                               expires=NOW + dt.timedelta(hours=1))
        with self.assertRaisesRegex(Reject, "replay"):
            _advance(replay, self.keys, IDENTITY, accepted, now=NOW, verifier=self.verify)

    def test_equal_issue_time_with_different_signed_body_is_rejected(self):
        first = make_snapshot(self.private, quantity=4)
        accepted, _ = _advance(first, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        conflict = make_snapshot(self.private, quantity=5)
        with self.assertRaisesRegex(Reject, "same_timestamp_conflict"):
            _advance(conflict, self.keys, IDENTITY, accepted, now=NOW, verifier=self.verify)

    def test_identical_retry_is_idempotent(self):
        snapshot = make_snapshot(self.private)
        accepted, _ = _advance(snapshot, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        same, outcome = _advance(snapshot, self.keys, IDENTITY, accepted, now=NOW, verifier=self.verify)
        self.assertEqual((same, outcome), (accepted, "unchanged"))

    def test_accepted_key_survives_cloud_key_retirement(self):
        snapshot = make_snapshot(self.private)
        accepted, _ = _advance(snapshot, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        self.assertEqual(accepted["schema"], 2)
        _validate_state(accepted, {}, now=NOW + dt.timedelta(days=30), verifier=self.verify)

    def test_new_key_snapshot_advances_after_the_previous_signing_key_is_retired(self):
        old = make_snapshot(self.private)
        accepted, _ = _advance(old, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        next_private = Ed25519PrivateKey.generate()
        next_public = next_private.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)
        next_keys = {"cloud-key": next_public}

        def verify_any_trusted_key(key, message, signature):
            try:
                Ed25519PublicKey.from_public_bytes(key).verify(signature, message)
                return True
            except InvalidSignature:
                return False

        next_snapshot = make_snapshot(next_private, issued=NOW + dt.timedelta(minutes=1), quantity=6)
        advanced, outcome = _advance(next_snapshot, next_keys, IDENTITY, accepted,
                                     now=NOW + dt.timedelta(minutes=1), verifier=verify_any_trusted_key)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(advanced["snapshot"], next_snapshot)
        self.assertEqual(base64.b64decode(advanced["accepted_key"]), next_public)

    def test_initial_claim_accepts_only_a_signed_provisional_to_cloud_identity_transition(self):
        target = dict(IDENTITY, appliance_id="cloud-assigned-appliance")
        transition = {"schema": 1, "transition_id": "claim-01", "kind": "initial_claim",
                      "from_appliance_id": "installer-generated-id", "to_identity": target}
        snapshot = make_snapshot(self.private, **target, identity_transition=transition)
        state, outcome = _advance(snapshot, self.keys, target, None, now=NOW,
                                 verifier=self.verify, anchor={"appliance_id": "installer-generated-id"})
        self.assertEqual(outcome, "accepted")
        self.assertEqual(state["identity"], target)
        self.assertEqual(state["transition_id"], "claim-01")
        with self.assertRaisesRegex(Reject, "identity_anchor_mismatch"):
            _advance(snapshot, self.keys, target, None, now=NOW, verifier=self.verify,
                     anchor={"appliance_id": "attacker-controlled-id"})
        # A cloud ID that happens to equal the current pin does not waive
        # validation of the signed transition's source.
        with self.assertRaisesRegex(Reject, "identity_anchor_mismatch"):
            _advance(snapshot, self.keys, target, None, now=NOW, verifier=self.verify,
                     anchor={"appliance_id": target["appliance_id"]})
        wrong_target = dict(target, customer_id="cust-other")
        wrong = make_snapshot(self.private, issued=NOW + dt.timedelta(minutes=1), **wrong_target,
                              identity_transition=transition)
        with self.assertRaisesRegex(Reject, "identity_transition_target_mismatch"):
            _advance(wrong, self.keys, wrong_target, None, now=NOW,
                     verifier=self.verify, anchor={"appliance_id": "installer-generated-id"})

    def test_reenrollment_requires_a_new_explicit_signed_transition_from_accepted_identity(self):
        accepted, _ = _advance(make_snapshot(self.private), self.keys, IDENTITY, None,
                               now=NOW, verifier=self.verify)
        target = dict(IDENTITY, appliance_id="appl-new", cloud_id="AIC-new", customer_id="cust-new")
        unauthorized = make_snapshot(self.private, issued=NOW + dt.timedelta(minutes=1), **target)
        with self.assertRaisesRegex(Reject, "accepted_identity_changed"):
            _advance(unauthorized, self.keys, target, accepted, now=NOW, verifier=self.verify)
        transition = {"schema": 1, "transition_id": "admin-transition-02", "kind": "authorized",
                      "reason": "ownership_transfer", "from_identity": IDENTITY,
                      "to_identity": target, "authorized_at": NOW.isoformat()}
        authorized = make_snapshot(self.private, issued=NOW + dt.timedelta(minutes=1), **target,
                                   identity_transition=transition)
        state, outcome = _advance(authorized, self.keys, target, accepted, now=NOW, verifier=self.verify,
                                  anchor={"appliance_id": IDENTITY["appliance_id"]})
        self.assertEqual(outcome, "accepted")
        self.assertEqual(state["identity"], target)
        self.assertEqual(state["transition_id"], "admin-transition-02")

    def test_tampering_forgery_and_identity_mismatch_fail_closed(self):
        good = make_snapshot(self.private)
        tampered = dict(good, camera_capacity={"camera_slot_quantity": 64})
        with self.assertRaisesRegex(Reject, "bad_signature"):
            _advance(tampered, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        with self.assertRaisesRegex(Reject, "bad_signature"):
            _advance(good, {"other": self.public}, IDENTITY, None, now=NOW, verifier=self.verify)
        for field in ("appliance_id", "cloud_id", "customer_id"):
            other = dict(IDENTITY, **{field: "wrong"})
            with self.assertRaisesRegex(Reject, "identity_mismatch"):
                _advance(good, self.keys, other, None, now=NOW, verifier=self.verify)

    def test_missing_or_malformed_capacity_never_promotes(self):
        for capacity in (None, {}, {"camera_slot_quantity": True}, {"camera_slot_quantity": -1},
                         {"camera_slot_quantity": 10_001}, {"camera_slot_quantity": "8"}):
            snapshot = make_snapshot(self.private)
            body = {k: v for k, v in snapshot.items() if k != "signature"}
            if capacity is None:
                body.pop("camera_capacity")
            else:
                body["camera_capacity"] = capacity
            bad = make_snapshot(self.private, **{})
            signature = base64.urlsafe_b64encode(self.private.sign(_canonical(body))).decode("ascii").rstrip("=")
            bad = {**body, "signature": {"alg": "Ed25519", "key_id": "cloud-key", "value": signature}}
            with self.subTest(capacity=capacity), self.assertRaises(Reject):
                _advance(bad, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)

    def test_expiration_must_follow_issue_time(self):
        bad = make_snapshot(self.private, issued=NOW, expires=NOW - dt.timedelta(seconds=1))
        with self.assertRaisesRegex(Reject, "malformed_time"):
            _advance(bad, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)

    def test_offline_accepted_capacity_survives_expiry_but_new_expired_sync_is_rejected(self):
        expired = make_snapshot(self.private, issued=NOW - dt.timedelta(days=4),
                                expires=NOW - dt.timedelta(days=3), quantity=6)
        with self.assertRaisesRegex(Reject, "expired"):
            _advance(expired, self.keys, IDENTITY, None, now=NOW, verifier=self.verify)
        accepted, _ = _advance(expired, self.keys, IDENTITY, None, now=NOW, verifier=self.verify,
                               allow_expired_first=True)
        # Re-reading accepted state ignores expiry while still checking signature and identity.
        _validate_state(accepted, self.keys, now=NOW + dt.timedelta(days=30), verifier=self.verify)
        later_expired = make_snapshot(self.private, issued=NOW - dt.timedelta(hours=1),
                                      expires=NOW - dt.timedelta(minutes=1), quantity=3)
        with self.assertRaisesRegex(Reject, "expired"):
            _advance(later_expired, self.keys, IDENTITY, accepted, now=NOW, verifier=self.verify)

    def test_current_store_identity_cannot_be_switched(self):
        accepted, _ = _advance(make_snapshot(self.private), self.keys, IDENTITY, None,
                               now=NOW, verifier=self.verify)
        other_identity = dict(IDENTITY, customer_id="cust-other")
        other = make_snapshot(self.private, issued=NOW + dt.timedelta(minutes=1), **other_identity)
        with self.assertRaisesRegex(Reject, "identity_mismatch|accepted_identity_changed"):
            _advance(other, self.keys, other_identity, accepted, now=NOW, verifier=self.verify)

    def test_root_appliance_identity_anchor_rejects_a_different_signed_device(self):
        _check_identity_anchor(IDENTITY, {"appliance_id": IDENTITY["appliance_id"]})
        with self.assertRaisesRegex(Reject, "identity_anchor_mismatch"):
            _check_identity_anchor(dict(IDENTITY, appliance_id="appl-other"),
                                  {"appliance_id": IDENTITY["appliance_id"]})


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "identity-pin filesystem test requires Linux/root ownership semantics")
class IdentityPinFilesystemTests(unittest.TestCase):
    def test_identity_bootstrap_refuses_unsigned_local_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity_dir, trust_dir = root / "recordings", root / "trust"
            identity_dir.mkdir(mode=0o750)
            trust_dir.mkdir(mode=0o700)
            identity_file = identity_dir / "appliance_identity.json"
            identity_file.write_text(json.dumps(IDENTITY))
            identity_fd = os.open(identity_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            trust_fd = os.open(trust_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.assertEqual(_bootstrap_identity(identity_fd, trust_fd), "bootstrapped")
                pin = trust_dir / IDENTITY_ANCHOR_FILE
                self.assertEqual(json.loads(pin.read_text()), {"schema": 1, "appliance_id": IDENTITY["appliance_id"]})
                self.assertEqual(os.stat(pin).st_uid, 0)
                self.assertEqual(_bootstrap_identity(identity_fd, trust_fd), "unchanged")
                identity_file.write_text(json.dumps({**IDENTITY, "appliance_id": "appl-other"}))
                self.assertEqual(_bootstrap_identity(identity_fd, trust_fd), "awaiting_signed_transition")
                self.assertEqual(json.loads(pin.read_text()), {"schema": 1, "appliance_id": IDENTITY["appliance_id"]})
            finally:
                os.close(identity_fd)
                os.close(trust_fd)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "acceptance filesystem tests require Linux/root ownership semantics")
class AcceptanceFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trust = self.root / "trust"
        self.stage_dir = self.root / "staging"
        self.identity_dir = self.root / "recordings"
        self.compat = self.root / "compat"
        for directory, mode in ((self.trust, 0o700), (self.stage_dir, 0o750),
                                (self.identity_dir, 0o750), (self.compat, 0o750)):
            directory.mkdir(mode=mode)
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)
        (self.trust / "entitlement_signing_keys.json").write_text(
            json.dumps({"keys": {"cloud-key": base64.b64encode(self.public).decode()}}), encoding="utf-8")
        (self.identity_dir / "appliance_identity.json").write_text(json.dumps(IDENTITY), encoding="utf-8")
        identity_fd = os.open(self.identity_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        trust_fd = os.open(self.trust, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _bootstrap_identity(identity_fd, trust_fd)
        finally:
            os.close(identity_fd)
            os.close(trust_fd)
        for path in self.trust.iterdir():
            path.chmod(0o600)
        self.stage = self.stage_dir / "vms_capacity_signed.json"
        self.accepted = self.trust / "vms_capacity_accepted.json"
        self.compat_file = self.compat / "vms_capacity.json"
        self.sentinel = self.root / "sentinel"
        self.sentinel.write_text("preserve", encoding="utf-8")

    def verifier(self, raw_key, message, signature):
        try:
            self.private.public_key().verify(signature, message)
            return raw_key == self.public
        except InvalidSignature:
            return False

    def call_accept(self, *, now=NOW):
        return accept_pending(staging_path=str(self.stage), identity_path=str(self.identity_dir / "appliance_identity.json"),
                              trust_dir=str(self.trust), accepted_file="vms_capacity_accepted.json",
                              compat_dir=str(self.compat), compat_file="vms_capacity.json",
                              now=now, verifier=self.verifier)

    def stage_snapshot(self, snapshot):
        self.stage.write_text(json.dumps(snapshot), encoding="utf-8")
        self.stage.chmod(0o600)

    def test_pending_snapshot_is_accepted_to_one_atomic_root_state(self):
        self.stage_snapshot(make_snapshot(self.private, quantity=6))
        self.assertEqual(self.call_accept(), "accepted")
        state = json.loads(self.accepted.read_text(encoding="utf-8"))
        self.assertEqual(state["identity"], IDENTITY)
        self.assertEqual(state["snapshot"]["camera_capacity"]["camera_slot_quantity"], 6)
        self.assertEqual(state["high_water"]["issued_at"], NOW.isoformat())
        self.assertEqual(os.stat(self.accepted).st_uid, 0)
        self.assertEqual(os.stat(self.accepted).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.compat_file.read_text())["camera_slot_quantity"], 6)

    def test_claim_transition_promotes_cloud_identity_and_updates_root_pin(self):
        provisional = "installer-generated-id"
        target = dict(IDENTITY, appliance_id="cloud-assigned-appliance")
        (self.trust / IDENTITY_ANCHOR_FILE).write_text(json.dumps({"schema": 1, "appliance_id": provisional}))
        (self.identity_dir / "appliance_identity.json").write_text(json.dumps(target), encoding="utf-8")
        transition = {"schema": 1, "transition_id": "claim-root-01", "kind": "initial_claim",
                      "from_appliance_id": provisional, "to_identity": target}
        self.stage_snapshot(make_snapshot(self.private, **target, identity_transition=transition))
        self.assertEqual(self.call_accept(), "accepted")
        state = json.loads(self.accepted.read_text(encoding="utf-8"))
        pin = json.loads((self.trust / IDENTITY_ANCHOR_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["identity"], target)
        self.assertEqual(pin["appliance_id"], target["appliance_id"])

    def test_replay_and_invalid_signature_preserve_last_accepted_state(self):
        self.stage_snapshot(make_snapshot(self.private, quantity=4))
        self.call_accept()
        old = self.accepted.read_bytes()
        older = make_snapshot(self.private, issued=NOW - dt.timedelta(days=1), quantity=64)
        self.stage_snapshot(older)
        with self.assertRaisesRegex(Reject, "replay"):
            self.call_accept()
        self.assertEqual(self.accepted.read_bytes(), old)
        self.assertEqual(json.loads(self.compat_file.read_text())["camera_slot_quantity"], 4)

    def test_symlinked_staging_and_identity_mismatch_are_rejected_without_following_links(self):
        self.stage.unlink()
        self.stage.symlink_to(self.sentinel)
        with self.assertRaisesRegex(Reject, "unsafe_file"):
            self.call_accept()
        self.assertEqual(self.sentinel.read_text(), "preserve")
        self.stage.unlink()
        self.stage_snapshot(make_snapshot(self.private))
        identity = self.identity_dir / "appliance_identity.json"
        identity.write_text(json.dumps(dict(IDENTITY, appliance_id="appl-other")), encoding="utf-8")
        with self.assertRaisesRegex(Reject, "identity_anchor_mismatch"):
            self.call_accept()
        self.assertFalse(self.accepted.exists())

    def test_symlinked_trust_key_or_accepted_store_fails_closed(self):
        self.stage_snapshot(make_snapshot(self.private))
        trust_key = self.trust / "entitlement_signing_keys.json"
        trust_key.unlink()
        trust_key.symlink_to(self.sentinel)
        with self.assertRaisesRegex(Reject, "unsafe_file"):
            self.call_accept()
        trust_key.unlink()
        trust_key.write_text(json.dumps({"keys": {"cloud-key": base64.b64encode(self.public).decode()}}))
        trust_key.chmod(0o600)
        self.stage_snapshot(make_snapshot(self.private))
        self.call_accept()
        self.accepted.unlink()
        self.accepted.symlink_to(self.sentinel)
        with self.assertRaisesRegex(Reject, "unsafe_file"):
            self.call_accept()
        self.assertEqual(self.sentinel.read_text(), "preserve")


@unittest.skipUnless(os.name == "posix" and os.open in getattr(os, "supports_dir_fd", set()),
                     "descriptor-relative atomic replacement requires Linux/POSIX")
class AtomicAcceptanceStoreTests(unittest.TestCase):
    def test_interrupted_replace_preserves_old_state_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            old = b'{"schema":1,"old":true}\n'
            target = folder / "accepted.json"
            target.write_bytes(old)
            directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.addCleanup(os.close, directory_fd)
            with mock.patch("capacity_accept.os.replace", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    _write_atomic_at(directory_fd, target.name, b'{"schema":1,"new":true}\n')
            self.assertEqual(target.read_bytes(), old)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["accepted.json"])

    def test_openssl_ed25519_verification_matches_cloud_canonical_json(self):
        openssl = shutil.which("openssl")
        git_openssl = Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe")
        if not openssl and git_openssl.is_file():
            openssl = str(git_openssl)
        if not openssl:
            self.skipTest("OpenSSL CLI is unavailable on this test host")
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                   serialization.PublicFormat.Raw)
        message = _canonical({"camera_capacity": {"camera_slot_quantity": 8}, "type": "anyaicam.feature_entitlements"})
        signature = private.sign(message)
        self.assertTrue(_openssl_verify(public, message, signature, openssl=openssl))
        self.assertFalse(_openssl_verify(public, message + b"x", signature, openssl=openssl))


if __name__ == "__main__":
    unittest.main()
