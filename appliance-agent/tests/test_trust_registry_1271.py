"""TRUST-1 + G2 (Linux 1.2.7.1): protected entitlement trust registry and last-known-good accepted capacity.

Covers the shared trust resolver (one stdlib-only block, byte-identical in the VMS, the root acceptor and the release
checks), the root acceptor's accepted-mode validation, rotation/revocation, registry rollback, lab scope and the
--migrate-trust-state upgrade. Green ids (GREEN-1271-TEST-DEFINITIONS.md 1b) are named in each test docstring.

All keys are fresh Ed25519 keys generated in-process; no real or lab key is used or written anywhere outside a
temporary directory. Time is injected; no clock is changed.
"""
import base64
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

SYSTEM_DIR = Path(__file__).resolve().parents[1] / "system"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SYSTEM_DIR))

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import capacity_accept as CA
from capacity_accept import Reject

IDENTITY = {"appliance_id": "appl-trust", "cloud_id": "AIC-trust", "customer_id": "cust-trust"}
NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc)
PURPOSES = ("analytics", "capacity", "features", "local_access")
BLOCK_BEGIN = "# ===== BEGIN SHARED ENTITLEMENT TRUST RESOLVER"
BLOCK_END = "# ===== END SHARED ENTITLEMENT TRUST RESOLVER ====="


def raw_public(private) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def snapshot(private, key_id, *, issued=None, expires=None, quantity=8, identity=IDENTITY, **changes):
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **identity,
            "features": {"talk_down": False, "voice_call": False},
            "camera_capacity": {"camera_slot_quantity": quantity, "plan_camera_slots": quantity,
                                "vms_license_capacity": 64},
            "issued_at": (issued or NOW).isoformat(),
            "expires_at": (expires or (issued or NOW) + dt.timedelta(hours=24)).isoformat()}
    body.update(changes)
    value = base64.urlsafe_b64encode(private.sign(CA._canonical(body))).decode("ascii").rstrip("=")
    return {**body, "signature": {"alg": "Ed25519", "key_id": key_id, "value": value}}


def verify_any(key, message, signature):
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


def registry(entries, *, generation=1, environment="production"):
    """entries: {key_id: (raw public key, state[, options])}."""
    keys = {}
    for key_id, spec in entries.items():
        raw, state = spec[0], spec[1]
        options = spec[2] if len(spec) > 2 else {}
        entry = {"public_key": b64(raw), "state": state,
                 "purposes": sorted(options.get("purposes", PURPOSES)),
                 "environment": options.get("environment", environment),
                 "changed_generation": options.get("changed_generation", 1)}
        if "appliance_ids" in options:
            entry["appliance_ids"] = options["appliance_ids"]
        keys[key_id] = entry
    return {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": environment,
            "generation": generation, "issued_at": NOW.isoformat(), "keys": keys}


def registry_sha(document) -> str:
    return hashlib.sha256(CA.canonical_trust_registry(document)).hexdigest()


def state_after(document, prior=None):
    return CA.next_trust_state(CA.parse_trust_registry(document), registry_sha(document), prior)


def context(document, state="derive"):
    if state == "derive":
        state = state_after(document)
    return CA.TrustContext(document, state)


def shared_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(BLOCK_BEGIN)
    end = text.index(BLOCK_END, start) + len(BLOCK_END)
    return text[start:end]


class Keys:
    def __init__(self):
        self.k1 = Ed25519PrivateKey.generate()
        self.k2 = Ed25519PrivateKey.generate()
        self.lab = Ed25519PrivateKey.generate()
        self.outsider = Ed25519PrivateKey.generate()
        self.p1, self.p2, self.plab, self.pout = (raw_public(k) for k in (self.k1, self.k2, self.lab, self.outsider))


# --------------------------------------------------------------------------------------------- shared resolver
class SharedResolverEquivalenceTests(unittest.TestCase):
    COPIES = (REPO / "app" / "appliance_entitlements.py",
              SYSTEM_DIR / "capacity_accept.py",
              REPO / "appliance-agent" / "anyaicam_agent" / "updater" / "release_checks.py")

    def test_the_resolver_block_is_byte_identical_in_every_copy(self):
        blocks = [shared_block(path) for path in self.COPIES]
        self.assertGreater(len(blocks[0]), 2000)
        for path, block in zip(self.COPIES[1:], blocks[1:]):
            self.assertEqual(block, blocks[0], f"{path} drifted from app/appliance_entitlements.py")

    def test_the_resolver_block_is_standard_library_only_and_self_contained(self):
        block = shared_block(self.COPIES[0])
        imported = set(re.findall(r"^\s*import (\w+)", block, re.M)) | set(re.findall(r"^\s*from (\w+)", block, re.M))
        self.assertTrue(imported <= {"base64", "datetime", "hashlib", "json", "re"}, imported)
        namespace = {"__name__": "isolated_trust_block"}
        exec(compile(block, "shared-trust-block", "exec"), namespace)  # noqa: S102 - our own source under test
        for name in ("TrustError", "TrustContext", "parse_trust_registry", "parse_trust_state", "next_trust_state",
                     "check_trust_registry_transition", "legacy_trust_registry", "canonical_trust_registry"):
            self.assertIn(name, namespace)


class ResolverLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keys()

    def test_active_signer_resolves_for_new_and_already_accepted_grants(self):
        trust = context(registry({"k1": (self.keys.p1, "active")}))
        for for_new in (True, False):
            self.assertEqual(trust.resolve("k1", purpose="capacity", for_new_grant=for_new,
                                           appliance_id="appl-trust"), self.keys.p1)

    def test_retired_signer_validates_accepted_grants_only(self):
        """U-T4 (resolver): RETIRED = verify-only."""
        trust = context(registry({"k1": (self.keys.p1, "retired"), "k2": (self.keys.p2, "active")}, generation=2))
        self.assertEqual(trust.resolve("k1", purpose="capacity", for_new_grant=False), self.keys.p1)
        with self.assertRaisesRegex(CA.TrustError, "retired_cannot_sign"):
            trust.resolve("k1", purpose="capacity", for_new_grant=True)
        self.assertEqual(trust.resolve("k2", purpose="capacity", for_new_grant=True), self.keys.p2)
        self.assertEqual(set(trust.active_keys("capacity")), {"k2"})

    def test_revoked_signer_is_refused_for_everything(self):
        """U-T1 / T-INV-3 (resolver)."""
        first = registry({"k1": (self.keys.p1, "active")})
        second = registry({"k1": (self.keys.p1, "revoked", {"changed_generation": 2}),
                           "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=2)
        trust = CA.TrustContext(second, state_after(second, state_after(first)))
        for purpose in PURPOSES:
            for for_new in (True, False):
                with self.assertRaisesRegex(CA.TrustError, "^revoked$"):
                    trust.resolve("k1", purpose=purpose, for_new_grant=for_new)

    def test_a_bound_key_absent_from_a_later_registry_is_revoked(self):
        """Owner decision 2: absent = revoked (the protected state remembers every binding)."""
        first = registry({"k1": (self.keys.p1, "active")})
        second = registry({"k2": (self.keys.p2, "active")}, generation=2)
        state = state_after(second, state_after(first))
        self.assertIn("k1", state["revoked"])
        with self.assertRaisesRegex(CA.TrustError, "^revoked$"):
            CA.TrustContext(second, state).resolve("k1", purpose="capacity", for_new_grant=False)

    def test_an_unknown_key_id_is_refused(self):
        """U-T2 (resolver)."""
        trust = context(registry({"k1": (self.keys.p1, "active")}))
        with self.assertRaisesRegex(CA.TrustError, "unknown_key"):
            trust.resolve("never-seen", purpose="capacity", for_new_grant=False)
        with self.assertRaisesRegex(CA.TrustError, "unknown_key"):
            trust.resolve(None, purpose="capacity", for_new_grant=False)

    def test_a_key_id_rebound_to_different_key_material_is_refused(self):
        first = registry({"k1": (self.keys.p1, "active")})
        rebound = registry({"k1": (self.keys.pout, "active")}, generation=2)
        with self.assertRaisesRegex(CA.TrustError, "binding_mismatch"):
            CA.TrustContext(rebound, state_after(first)).resolve("k1", purpose="capacity", for_new_grant=False)
        with self.assertRaisesRegex(CA.TrustError, "binding_mismatch"):
            state_after(rebound, state_after(first))

    def test_revoked_key_material_cannot_return_under_a_new_key_id(self):
        first = registry({"k1": (self.keys.p1, "revoked")})
        renamed = registry({"k1": (self.keys.p1, "revoked"), "k1-again": (self.keys.p2, "active")}, generation=2)
        state = state_after(renamed, state_after(first))
        smuggled = registry({"k1": (self.keys.p1, "revoked"), "k1-new": (self.keys.p1, "active")}, generation=3)
        with self.assertRaises(CA.TrustError):  # duplicate key material is refused by the parser
            CA.parse_trust_registry(smuggled)
        # Even a registry that omits the old id cannot re-trust the revoked material under another id.
        omitted = registry({"k1-again": (self.keys.p2, "active"), "k1-new": (self.keys.p1, "active")}, generation=3)
        with self.assertRaisesRegex(CA.TrustError, "revoked"):
            CA.TrustContext(omitted, state).resolve("k1-new", purpose="capacity", for_new_grant=False)

    def test_purpose_is_enforced(self):
        trust = context(registry({"k1": (self.keys.p1, "active", {"purposes": ["capacity"]})}))
        trust.resolve("k1", purpose="capacity", for_new_grant=True)
        for purpose in ("features", "analytics", "local_access"):
            with self.assertRaisesRegex(CA.TrustError, "purpose_denied"):
                trust.resolve("k1", purpose=purpose, for_new_grant=True)
        with self.assertRaisesRegex(CA.TrustError, "purpose_denied"):
            trust.resolve("k1", purpose="anything-else", for_new_grant=True)

    def test_lab_signer_never_authorizes_capacity_in_a_production_registry(self):
        """U-T5: a lab (other-environment) signer is never accepted for capacity, even allow-listed; analytics only
        through the explicit appliance allow-list (closes N2b)."""
        doc = registry({"k1": (self.keys.p1, "active"),
                        "lab-signer": (self.keys.plab, "active",
                                       {"environment": "lab", "appliance_ids": ["appl-trust"]}),
                        "lab-unlisted": (self.keys.pout, "active", {"environment": "lab"})})
        trust = context(doc)
        for for_new in (True, False):
            with self.assertRaisesRegex(CA.TrustError, "scope_denied"):
                trust.resolve("lab-signer", purpose="capacity", for_new_grant=for_new, appliance_id="appl-trust")
        self.assertEqual(trust.resolve("lab-signer", purpose="analytics", for_new_grant=True,
                                       appliance_id="appl-trust"), self.keys.plab)
        with self.assertRaisesRegex(CA.TrustError, "scope_denied"):
            trust.resolve("lab-signer", purpose="analytics", for_new_grant=True, appliance_id="appl-other")
        with self.assertRaisesRegex(CA.TrustError, "scope_denied"):
            trust.resolve("lab-unlisted", purpose="analytics", for_new_grant=True, appliance_id="appl-trust")
        self.assertNotIn("lab-signer", trust.active_keys("capacity", appliance_id="appl-trust"))

    def test_environment_is_pinned_by_the_protected_state(self):
        production = registry({"k1": (self.keys.p1, "active")})
        lab = registry({"k1": (self.keys.p1, "active")}, generation=2, environment="lab")
        with self.assertRaisesRegex(CA.TrustError, "environment_mismatch"):
            CA.TrustContext(lab, state_after(production)).resolve("k1", purpose="capacity", for_new_grant=False)
        with self.assertRaisesRegex(CA.TrustError, "environment_mismatch"):
            state_after(lab, state_after(production))


class RegistryRollbackTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keys()
        self.gen1 = registry({"k1": (self.keys.p1, "active")})
        self.gen2 = registry({"k1": (self.keys.p1, "retired", {"changed_generation": 2}),
                              "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=2)
        self.state2 = state_after(self.gen2, state_after(self.gen1))

    def test_a_lower_generation_registry_is_registry_rollback(self):
        """U-T3 (read side) / T-INV-2."""
        with self.assertRaisesRegex(CA.TrustError, "registry_rollback"):
            CA.TrustContext(self.gen1, self.state2).resolve("k1", purpose="capacity", for_new_grant=False)
        with self.assertRaisesRegex(CA.TrustError, "registry_rollback"):
            state_after(self.gen1, self.state2)

    def test_the_same_generation_with_different_content_is_refused_and_an_identical_replay_is_a_no_op(self):
        """U-T3 (install side): N again with the same bytes changes nothing; different bytes at N are refused."""
        self.assertEqual(state_after(self.gen2, self.state2), self.state2)
        tampered = json.loads(json.dumps(self.gen2))
        tampered["issued_at"] = (NOW + dt.timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(CA.TrustError, "registry_rollback"):
            state_after(tampered, self.state2)

    def test_a_missing_registry_after_generation_one_is_registry_rollback_never_legacy(self):
        """Green 9 / T-E7: with protected state generation >= 1, no registry never falls back to the flat keyset."""
        flat = CA.legacy_trust_registry({"k1": b64(self.keys.p1)})
        for registry_value in (None, flat):
            with self.subTest(registry=registry_value is not None):
                with self.assertRaisesRegex(CA.TrustError, "registry_rollback"):
                    CA.TrustContext(registry_value, self.state2).resolve("k1", purpose="capacity",
                                                                         for_new_grant=False)

    def test_an_accepted_state_from_a_registry_generation_needs_a_registry(self):
        """Restoring only the accepted state (trust_generation >= 1) onto a box without registry/state fails closed."""
        flat = CA.legacy_trust_registry({"k1": b64(self.keys.p1)})
        with self.assertRaisesRegex(CA.TrustError, "registry_rollback"):
            CA.TrustContext(flat, None).resolve("k1", purpose="capacity", for_new_grant=False, accepted_generation=2)
        with self.assertRaisesRegex(CA.TrustError, "registry_rollback"):
            CA.TrustContext(self.gen1, state_after(self.gen1)).resolve(
                "k1", purpose="capacity", for_new_grant=False, accepted_generation=2)

    def test_the_revoked_set_only_grows(self):
        gen3 = registry({"k1": (self.keys.p1, "revoked", {"changed_generation": 3}),
                         "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=3)
        state3 = state_after(gen3, self.state2)
        unrevoked = registry({"k1": (self.keys.p1, "retired", {"changed_generation": 4}),
                              "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=4)
        with self.assertRaisesRegex(CA.TrustError, "registry_rollback|lifecycle_backwards"):
            state_after(unrevoked, state3)
        self.assertEqual(set(state3["revoked"]), {"k1"})
        gen4 = registry({"k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=4)
        with self.assertRaises(CA.TrustError):  # tooling/installer guard: dropping a bound key is refused...
            CA.check_trust_registry_transition(CA.parse_trust_registry(gen3), CA.parse_trust_registry(gen4))
        state4 = state_after(gen4, state3)  # ...but if one is installed anyway, the revocation survives
        self.assertEqual(set(state4["revoked"]), {"k1"})


class LegacyModeTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keys()

    def test_legacy_flat_keys_are_active_for_every_purpose_at_generation_zero(self):
        trust = CA.TrustContext(CA.legacy_trust_registry({"k1": b64(self.keys.p1)}), None)
        self.assertEqual(trust.mode, "legacy")
        self.assertEqual(trust.generation, 0)
        for purpose in PURPOSES:
            self.assertEqual(trust.resolve("k1", purpose=purpose, for_new_grant=True), self.keys.p1)

    def test_legacy_mode_never_restores_a_removed_flat_key(self):
        trust = CA.TrustContext(CA.legacy_trust_registry({"k2": b64(self.keys.p2)}), None)
        with self.assertRaisesRegex(CA.TrustError, "unknown_key"):
            trust.resolve("k1", purpose="capacity", for_new_grant=False)

    def test_no_keys_at_all_is_no_trust_anchor(self):
        for value in (None, CA.legacy_trust_registry({})):
            with self.assertRaisesRegex(CA.TrustError, "no_trust_anchor"):
                CA.TrustContext(value, None).resolve("k1", purpose="capacity", for_new_grant=False)


class RegistryParsingAndTransitionTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keys()
        self.good = registry({"k1": (self.keys.p1, "active")})

    def test_malformed_registries_are_refused(self):
        def variant(change):
            document = json.loads(json.dumps(self.good))
            change(document)
            return document
        bad = {
            "type": variant(lambda d: d.update(type="other")),
            "version": variant(lambda d: d.update(version=2)),
            "generation zero": variant(lambda d: d.update(generation=0)),
            "generation bool": variant(lambda d: d.update(generation=True)),
            "unknown field": variant(lambda d: d.update(extra=1)),
            "no keys": variant(lambda d: d.update(keys={})),
            "bad key id": variant(lambda d: d["keys"].update({"bad id": d["keys"]["k1"]})),
            "short key": variant(lambda d: d["keys"]["k1"].update(public_key="AAAA")),
            "bad state": variant(lambda d: d["keys"]["k1"].update(state="paused")),
            "empty purposes": variant(lambda d: d["keys"]["k1"].update(purposes=[])),
            "unknown purpose": variant(lambda d: d["keys"]["k1"].update(purposes=["capacity", "root"])),
            "future change": variant(lambda d: d["keys"]["k1"].update(changed_generation=5)),
            "unknown key field": variant(lambda d: d["keys"]["k1"].update(private_key="x")),
            "bad issued_at": variant(lambda d: d.update(issued_at="yesterday")),
        }
        for label, document in bad.items():
            with self.subTest(label), self.assertRaises(CA.TrustError):
                CA.parse_trust_registry(document)
        CA.parse_trust_registry(self.good)

    def test_transition_guards_for_tooling_installer_and_applier(self):
        parse = CA.parse_trust_registry
        gen1 = parse(self.good)
        rotate = parse(registry({"k1": (self.keys.p1, "retired", {"changed_generation": 2}),
                                 "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=2))
        CA.check_trust_registry_transition(gen1, rotate)
        CA.check_trust_registry_transition(gen1, gen1)  # identical replay: allowed no-op
        CA.check_trust_registry_transition(None, gen1)  # first registry
        refused = {
            "lower generation": (rotate, gen1, "registry_rollback"),
            "dropped binding": (rotate, parse(registry({"k2": (self.keys.p2, "active")}, generation=3)),
                                "dropped_binding"),
            "retired to active": (rotate, parse(registry({"k1": (self.keys.p1, "active"),
                                                          "k2": (self.keys.p2, "active")}, generation=3)),
                                  "lifecycle_backwards"),
            "rebinding": (gen1, parse(registry({"k1": (self.keys.p2, "active")}, generation=2)), "binding_mismatch"),
            "environment": (gen1, parse(registry({"k1": (self.keys.p1, "active")}, generation=2,
                                                 environment="lab")), "environment_mismatch"),
        }
        for label, (old, new, reason) in refused.items():
            with self.subTest(label), self.assertRaisesRegex(CA.TrustError, reason):
                CA.check_trust_registry_transition(old, new)
        revoked = parse(registry({"k1": (self.keys.p1, "revoked", {"changed_generation": 3}),
                                  "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=3))
        CA.check_trust_registry_transition(rotate, revoked)
        back = parse(registry({"k1": (self.keys.p1, "retired", {"changed_generation": 4}),
                               "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=4))
        with self.assertRaisesRegex(CA.TrustError, "lifecycle_backwards"):
            CA.check_trust_registry_transition(revoked, back)


# --------------------------------------------------------------------------------------------- acceptor policy
class AcceptedModePolicyTests(unittest.TestCase):
    """G2 on the root acceptor: an already-accepted grant is validated without the future/expiry check."""

    def setUp(self):
        self.keys = Keys()
        self.gen1 = registry({"k1": (self.keys.p1, "active")})
        self.trust = context(self.gen1)
        self.accepted, outcome = CA._advance(snapshot(self.keys.k1, "k1", quantity=4), self.trust, IDENTITY, None,
                                            now=NOW, verifier=verify_any)
        self.assertEqual(outcome, "accepted")

    def test_option_a_state_names_its_signer_and_trust_generation_and_stays_127_readable(self):
        state = self.accepted
        self.assertEqual(state["schema"], 2)
        self.assertEqual(state["signer"], {"key_id": "k1",
                                           "public_key_sha256": hashlib.sha256(self.keys.p1).hexdigest()})
        self.assertEqual(state["trust_generation"], 1)
        # A 1.2.7 reader (schema {1,2}) verifies with accepted_key: it must still be the signer's exact key.
        self.assertEqual(base64.b64decode(state["accepted_key"]), self.keys.p1)

    def test_u_g1_clock_rollback_keeps_the_accepted_grant_valid(self):
        """U-G1: 24 h, 30 days and 1 year behind -> still valid (no bad_time / issued_in_future on accepted state)."""
        for delta in (dt.timedelta(hours=24), dt.timedelta(days=30), dt.timedelta(days=365)):
            with self.subTest(delta=delta):
                identity, high_water = CA._validate_state(self.accepted, self.trust, now=NOW - delta,
                                                          verifier=verify_any)
                self.assertEqual(identity, IDENTITY)
                self.assertEqual(high_water["issued_at"], NOW.isoformat())

    def test_u_g2_a_future_new_grant_is_refused_while_the_clock_is_behind(self):
        """U-G2: a NEW grant stays strict (issued_at more than 5 min after the clock)."""
        behind = NOW - dt.timedelta(days=30)
        future = snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(minutes=10), quantity=64)
        before = json.dumps(self.accepted, sort_keys=True)
        with self.assertRaisesRegex(Reject, "issued_in_future"):
            CA._advance(future, self.trust, IDENTITY, self.accepted, now=behind, verifier=verify_any)
        self.assertEqual(json.dumps(self.accepted, sort_keys=True), before)
        CA._validate_state(self.accepted, self.trust, now=behind, verifier=verify_any)  # LKG still good

    def test_u_g3_replay_tamper_and_wrong_identity_are_refused_with_and_without_rollback(self):
        """U-G3."""
        older = snapshot(self.keys.k1, "k1", issued=NOW - dt.timedelta(hours=1), quantity=64)
        tampered = dict(snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(minutes=1)),
                        camera_capacity={"camera_slot_quantity": 64})
        for clock in (NOW + dt.timedelta(minutes=2), NOW - dt.timedelta(days=1)):
            with self.subTest(clock=clock):
                with self.assertRaisesRegex(Reject, "replay|issued_in_future"):
                    CA._advance(older, self.trust, IDENTITY, self.accepted, now=clock, verifier=verify_any)
                with self.assertRaisesRegex(Reject, "bad_signature|issued_in_future"):
                    CA._advance(tampered, self.trust, IDENTITY, self.accepted, now=clock, verifier=verify_any)
                for field in ("appliance_id", "cloud_id", "customer_id"):
                    other = dict(IDENTITY, **{field: "someone-else"})
                    wrong = snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(minutes=1), identity=other)
                    with self.assertRaises(Reject):
                        CA._advance(wrong, self.trust, IDENTITY, self.accepted, now=clock, verifier=verify_any)
        replay_at_correct_clock = snapshot(self.keys.k1, "k1", issued=NOW - dt.timedelta(hours=1))
        with self.assertRaisesRegex(Reject, "replay"):
            CA._advance(replay_at_correct_clock, self.trust, IDENTITY, self.accepted,
                        now=NOW + dt.timedelta(minutes=1), verifier=verify_any)

    def test_u_g4_after_the_clock_is_corrected_a_newer_grant_replaces_last_known_good(self):
        """U-G4."""
        newer = snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(hours=2), quantity=6)
        with self.assertRaisesRegex(Reject, "issued_in_future"):
            CA._advance(newer, self.trust, IDENTITY, self.accepted, now=NOW - dt.timedelta(days=1),
                        verifier=verify_any)
        state, outcome = CA._advance(newer, self.trust, IDENTITY, self.accepted,
                                     now=NOW + dt.timedelta(hours=2), verifier=verify_any)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(state["snapshot"]["camera_capacity"]["camera_slot_quantity"], 6)

    def test_u_g5_editing_the_stored_last_known_good_fails_closed(self):
        """U-G5: capacity number, signer id, embedded key or high-water edited -> refused."""
        edits = {
            "capacity": lambda s: s["snapshot"]["camera_capacity"].update(camera_slot_quantity=64),
            "signer id": lambda s: s["signer"].update(key_id="k2"),
            "signer fingerprint": lambda s: s["signer"].update(public_key_sha256="0" * 64),
            "embedded key": lambda s: s.update(accepted_key=b64(self.keys.pout)),
            "high water": lambda s: s["high_water"].update(digest="0" * 64),
            "identity": lambda s: s["identity"].update(customer_id="cust-other"),
            "trust generation raised": lambda s: s.update(trust_generation=9),
        }
        for label, edit in edits.items():
            state = json.loads(json.dumps(self.accepted))
            edit(state)
            with self.subTest(label), self.assertRaises(Reject):
                CA._validate_state(state, self.trust, now=NOW, verifier=verify_any)


class AcceptorTrustLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keys()
        self.gen1 = registry({"k1": (self.keys.p1, "active")})
        self.state1 = state_after(self.gen1)
        self.accepted, _ = CA._advance(snapshot(self.keys.k1, "k1", quantity=4), CA.TrustContext(self.gen1, self.state1),
                                       IDENTITY, None, now=NOW, verifier=verify_any)

    def trust(self, entries, generation):
        document = registry(entries, generation=generation)
        return CA.TrustContext(document, state_after(document, self.state1))

    def test_u_t1_revoked_signer_refuses_the_cached_grant_retired_keeps_it(self):
        """U-T1 (acceptor)."""
        retired = self.trust({"k1": (self.keys.p1, "retired"), "k2": (self.keys.p2, "active")}, 2)
        CA._validate_state(self.accepted, retired, now=NOW, verifier=verify_any)
        revoked = self.trust({"k1": (self.keys.p1, "revoked"), "k2": (self.keys.p2, "active")}, 2)
        with self.assertRaisesRegex(Reject, "^revoked$"):
            CA._validate_state(self.accepted, revoked, now=NOW, verifier=verify_any)
        absent = self.trust({"k2": (self.keys.p2, "active")}, 2)
        with self.assertRaisesRegex(Reject, "^revoked$"):
            CA._validate_state(self.accepted, absent, now=NOW, verifier=verify_any)

    def test_u_t2_an_embedded_key_never_confers_trust(self):
        """U-T2 / T-INV-1: the b8 schema-2 state (accepted_key only) is refused once its key is not trusted --
        this inverts b8's test_accepted_key_survives_cloud_key_retirement."""
        b8_state = {key: value for key, value in self.accepted.items() if key not in ("signer", "trust_generation")}
        for trust in ({}, {"other": self.keys.p2}, CA.TrustContext(CA.legacy_trust_registry({}), None)):
            with self.subTest(trust=type(trust).__name__), self.assertRaises(Reject):
                CA._validate_state(b8_state, trust, now=NOW, verifier=verify_any)
        unknown = snapshot(self.keys.outsider, "outsider-key", issued=NOW + dt.timedelta(minutes=1))
        forged_state = dict(b8_state, snapshot=unknown, accepted_key=b64(self.keys.pout),
                            high_water={"issued_at": (NOW + dt.timedelta(minutes=1)).isoformat(),
                                        "digest": hashlib.sha256(CA._canonical(CA._body(unknown))).hexdigest()})
        with self.assertRaisesRegex(Reject, "unknown_key"):
            CA._validate_state(forged_state, CA.TrustContext(self.gen1, self.state1), now=NOW, verifier=verify_any)

    def test_u_t4_rotation_old_grant_works_new_old_key_grant_refused_new_key_grant_accepted(self):
        """U-T4 (acceptor)."""
        rotated = self.trust({"k1": (self.keys.p1, "retired"), "k2": (self.keys.p2, "active")}, 2)
        CA._validate_state(self.accepted, rotated, now=NOW, verifier=verify_any)
        k1_new = snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(minutes=1), quantity=64)
        with self.assertRaisesRegex(Reject, "retired_cannot_sign"):
            CA._advance(k1_new, rotated, IDENTITY, self.accepted, now=NOW + dt.timedelta(minutes=1),
                        verifier=verify_any)
        k2_new = snapshot(self.keys.k2, "k2", issued=NOW + dt.timedelta(minutes=1), quantity=6)
        state, outcome = CA._advance(k2_new, rotated, IDENTITY, self.accepted, now=NOW + dt.timedelta(minutes=1),
                                     verifier=verify_any)
        self.assertEqual((outcome, state["signer"]["key_id"], state["trust_generation"]), ("accepted", "k2", 2))
        CA._validate_state(state, rotated, now=NOW, verifier=verify_any)

    def test_a_newer_active_grant_replaces_a_revoked_signer_state_but_an_older_replay_is_still_refused(self):
        revoked = self.trust({"k1": (self.keys.p1, "revoked"), "k2": (self.keys.p2, "active")}, 2)
        older = snapshot(self.keys.k2, "k2", issued=NOW - dt.timedelta(hours=1), quantity=64)
        with self.assertRaisesRegex(Reject, "replay"):
            CA._advance(older, revoked, IDENTITY, self.accepted, now=NOW + dt.timedelta(minutes=1),
                        verifier=verify_any)
        newer = snapshot(self.keys.k2, "k2", issued=NOW + dt.timedelta(minutes=1), quantity=5)
        state, outcome = CA._advance(newer, revoked, IDENTITY, self.accepted, now=NOW + dt.timedelta(minutes=1),
                                     verifier=verify_any)
        self.assertEqual((outcome, state["signer"]["key_id"]), ("accepted", "k2"))
        revoked_signed = snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(minutes=2))
        with self.assertRaisesRegex(Reject, "^revoked$"):
            CA._advance(revoked_signed, revoked, IDENTITY, state, now=NOW + dt.timedelta(minutes=2),
                        verifier=verify_any)

    def test_u_t5_a_lab_signer_cannot_raise_capacity_for_a_production_identity(self):
        """U-T5 (acceptor): even allow-listed for this appliance and present on disk, never for capacity."""
        lab = self.trust({"k1": (self.keys.p1, "active"),
                          "lab-signer": (self.keys.plab, "active",
                                         {"environment": "lab", "appliance_ids": [IDENTITY["appliance_id"]]})}, 2)
        lab_grant = snapshot(self.keys.lab, "lab-signer", issued=NOW + dt.timedelta(minutes=1), quantity=64)
        with self.assertRaisesRegex(Reject, "scope_denied"):
            CA._advance(lab_grant, lab, IDENTITY, self.accepted, now=NOW + dt.timedelta(minutes=1),
                        verifier=verify_any)
        with self.assertRaisesRegex(Reject, "scope_denied"):
            CA._advance(lab_grant, lab, IDENTITY, None, now=NOW + dt.timedelta(minutes=1), verifier=verify_any)

    def test_registry_rollback_refuses_the_cached_grant(self):
        gen2 = registry({"k1": (self.keys.p1, "active"), "k2": (self.keys.p2, "active")}, generation=2)
        state2 = state_after(gen2, self.state1)
        with self.assertRaisesRegex(Reject, "registry_rollback"):
            CA._validate_state(self.accepted, CA.TrustContext(self.gen1, state2), now=NOW, verifier=verify_any)
        with self.assertRaisesRegex(Reject, "registry_rollback"):
            CA._validate_state(self.accepted, CA.TrustContext(None, state2), now=NOW, verifier=verify_any)

    def test_the_floor_is_structural_only(self):
        identity, high_water = CA._state_floor(self.accepted)
        self.assertEqual((identity, high_water["issued_at"]), (IDENTITY, NOW.isoformat()))
        broken = json.loads(json.dumps(self.accepted))
        broken["high_water"]["issued_at"] = (NOW + dt.timedelta(days=9)).isoformat()
        with self.assertRaisesRegex(Reject, "invalid_high_water"):
            CA._state_floor(broken)


# --------------------------------------------------------------------------------------------- filesystem (root)
@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "acceptance filesystem tests require Linux/root ownership semantics")
class TrustFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trust_dir, self.stage_dir, self.identity_dir, self.compat = (
            self.root / "trust", self.root / "staging", self.root / "recordings", self.root / "compat")
        for directory, mode in ((self.trust_dir, 0o755), (self.stage_dir, 0o750),
                                (self.identity_dir, 0o750), (self.compat, 0o750)):
            directory.mkdir(mode=mode)
            directory.chmod(mode)
        self.keys = Keys()
        (self.identity_dir / "appliance_identity.json").write_text(json.dumps(IDENTITY), encoding="utf-8")
        self.write_root(CA.TRUST_FILE, json.dumps({"keys": {"k1": b64(self.keys.p1)}}).encode())
        self.write_root(CA.IDENTITY_ANCHOR_FILE, json.dumps({"schema": 1, "appliance_id": IDENTITY["appliance_id"]}).encode(),
                        mode=0o600)
        self.stage = self.stage_dir / "vms_capacity_signed.json"
        self.accepted = self.trust_dir / CA.ACCEPTED_FILE
        self.recordings = self.root / "customer-recordings"
        self.recordings.mkdir()
        (self.recordings / "camera-1.mkv").write_bytes(os.urandom(4096))
        self.recordings_before = {p.name: p.read_bytes() for p in self.recordings.iterdir()}

    def write_root(self, name, data: bytes, mode=0o644):
        path = self.trust_dir / name
        path.write_bytes(data)
        os.chown(path, 0, 0)
        path.chmod(mode)
        return path

    def install_registry(self, document):
        data = CA.canonical_trust_registry(document)
        self.write_root(CA.TRUST_REGISTRY_FILE, data)
        prior = None
        state_path = self.trust_dir / CA.TRUST_STATE_FILE
        if state_path.exists():
            prior = json.loads(state_path.read_text())
        state = CA.next_trust_state(CA.parse_trust_registry(document), hashlib.sha256(data).hexdigest(), prior)
        self.write_root(CA.TRUST_STATE_FILE, (json.dumps(state, sort_keys=True) + "\n").encode())

    def call_accept(self, *, now=NOW):
        self.stage.chmod(0o600)
        return CA.accept_pending(staging_path=str(self.stage), identity_path=str(self.identity_dir / "appliance_identity.json"),
                                 trust_dir=str(self.trust_dir), accepted_file=CA.ACCEPTED_FILE,
                                 compat_dir=str(self.compat), compat_file="vms_capacity.json", now=now,
                                 verifier=verify_any)

    def stage_snapshot(self, value):
        self.stage.write_text(json.dumps(value), encoding="utf-8")

    def assert_recordings_untouched(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.recordings.iterdir()}, self.recordings_before)

    def test_acceptance_with_a_registry_records_signer_and_ratchets_nothing_backwards(self):
        self.install_registry(registry({"k1": (self.keys.p1, "active")}))
        self.stage_snapshot(snapshot(self.keys.k1, "k1", quantity=3))
        self.assertEqual(self.call_accept(), "accepted")
        state = json.loads(self.accepted.read_text())
        self.assertEqual((state["signer"]["key_id"], state["trust_generation"]), ("k1", 1))

    def test_a_deleted_registry_after_generation_one_refuses_new_grants_and_keeps_the_file(self):
        """Green 9.1/9.2: registry missing with state generation >= 1 = registry_rollback (fail closed)."""
        self.install_registry(registry({"k1": (self.keys.p1, "active")}))
        self.stage_snapshot(snapshot(self.keys.k1, "k1", quantity=3))
        self.call_accept()
        before = self.accepted.read_bytes()
        (self.trust_dir / CA.TRUST_REGISTRY_FILE).unlink()
        self.stage_snapshot(snapshot(self.keys.k1, "k1", issued=NOW + dt.timedelta(minutes=1), quantity=9))
        with self.assertRaisesRegex(Reject, "registry_rollback"):
            self.call_accept(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(self.accepted.read_bytes(), before)
        self.assert_recordings_untouched()

    def test_an_older_registry_on_disk_is_refused_and_state_is_unchanged(self):
        """U-T3 (acceptor): the state never moves backwards."""
        gen1 = registry({"k1": (self.keys.p1, "active")})
        gen2 = registry({"k1": (self.keys.p1, "active"), "k2": (self.keys.p2, "active", {"changed_generation": 2})},
                        generation=2)
        self.install_registry(gen1)
        self.install_registry(gen2)
        state_before = (self.trust_dir / CA.TRUST_STATE_FILE).read_bytes()
        self.write_root(CA.TRUST_REGISTRY_FILE, CA.canonical_trust_registry(gen1))
        self.stage_snapshot(snapshot(self.keys.k1, "k1"))
        with self.assertRaisesRegex(Reject, "registry_rollback"):
            self.call_accept()
        self.assertEqual((self.trust_dir / CA.TRUST_STATE_FILE).read_bytes(), state_before)
        self.assertFalse(self.accepted.exists())

    def test_revocation_after_acceptance_keeps_the_file_and_recordings_and_a_new_key_recovers(self):
        self.install_registry(registry({"k1": (self.keys.p1, "active")}))
        self.stage_snapshot(snapshot(self.keys.k1, "k1", quantity=3))
        self.call_accept()
        accepted_before = self.accepted.read_bytes()
        self.install_registry(registry({"k1": (self.keys.p1, "revoked", {"changed_generation": 2}),
                                        "k2": (self.keys.p2, "active", {"changed_generation": 2})}, generation=2))
        trust = CA._load_trust(os.open(self.trust_dir, os.O_RDONLY | os.O_DIRECTORY))
        with self.assertRaisesRegex(Reject, "^revoked$"):
            CA._validate_state(json.loads(accepted_before), trust, now=NOW, verifier=verify_any)
        self.assertEqual(self.accepted.read_bytes(), accepted_before)  # kept for the anti-replay floor
        self.stage_snapshot(snapshot(self.keys.k1, "k1", issued=NOW - dt.timedelta(hours=1)))
        with self.assertRaises(Reject):
            self.call_accept()
        self.stage_snapshot(snapshot(self.keys.k2, "k2", issued=NOW + dt.timedelta(minutes=1), quantity=2))
        self.assertEqual(self.call_accept(now=NOW + dt.timedelta(minutes=1)), "accepted")
        self.assert_recordings_untouched()

    def run_cli(self, *argv):
        constants = {"TRUST_DIR": str(self.trust_dir), "IDENTITY_FILE": str(self.identity_dir / "appliance_identity.json")}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.multiple(CA, **constants), \
                mock.patch.object(CA.sys, "argv", ["capacity_accept.py", *argv]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = CA.main()
        return code, stdout.getvalue().strip(), stderr.getvalue().strip()

    def b8_state(self):
        """A schema-2 accepted state exactly as 1.2.7 (b8) wrote it: accepted_key, no signer/trust_generation."""
        state, _ = CA._advance(snapshot(self.keys.k1, "k1", quantity=4), {"k1": self.keys.p1}, IDENTITY, None,
                               now=NOW, verifier=verify_any)
        for field in ("signer", "trust_generation"):
            state.pop(field, None)
        self.write_root(CA.ACCEPTED_FILE, (json.dumps(state, sort_keys=True) + "\n").encode(), mode=0o600)
        return state

    def test_migrate_trust_state_upgrades_an_existing_127_state_and_is_idempotent(self):
        old = self.b8_state()
        self.install_registry(registry({"k1": (self.keys.p1, "active")}))
        code, output, error = self.run_cli("--migrate-trust-state")
        self.assertEqual((code, output), (0, "migrated"), error)
        state = json.loads(self.accepted.read_text())
        self.assertEqual(state["signer"]["key_id"], "k1")
        self.assertEqual(state["trust_generation"], 1)
        self.assertEqual({k: v for k, v in state.items() if k not in ("signer", "trust_generation")}, old)
        self.assertEqual(self.run_cli("--migrate-trust-state")[:2], (0, "unchanged"))
        self.assertEqual(os.stat(self.accepted).st_mode & 0o777, 0o600)

    def test_migrate_trust_state_without_a_registry_is_a_legacy_no_op(self):
        before = (json.dumps(self.b8_state(), sort_keys=True) + "\n").encode()
        self.assertEqual(self.run_cli("--migrate-trust-state")[:2], (0, "unchanged"))
        self.assertEqual(self.accepted.read_bytes(), before)
        self.assertFalse((self.trust_dir / CA.TRUST_STATE_FILE).exists())

    def test_migrate_trust_state_never_rewrites_an_untrusted_state(self):
        before = (json.dumps(self.b8_state(), sort_keys=True) + "\n").encode()
        self.install_registry(registry({"k2": (self.keys.p2, "active")}))  # k1 is not in the first registry
        code, output, error = self.run_cli("--migrate-trust-state")
        self.assertEqual(code, 0)
        self.assertEqual(output, "accepted_state_untrusted")
        self.assertIn("unknown_key", error)
        self.assertEqual(self.accepted.read_bytes(), before)

    def test_bootstrap_identity_succeeds_under_clock_rollback(self):
        """G2: the b8 bootstrap test (accepted state issued 'in the future') now succeeds."""
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        self.install_registry(registry({"k1": (self.keys.p1, "active")}))
        state, _ = CA._advance(snapshot(self.keys.k1, "k1", issued=future), context(registry({"k1": (self.keys.p1, "active")})),
                               IDENTITY, None, now=future, verifier=verify_any)
        self.write_root(CA.ACCEPTED_FILE, (json.dumps(state) + "\n").encode(), mode=0o600)
        with mock.patch.object(CA, "_openssl_verify", side_effect=lambda key, message, sig, **_: verify_any(key, message, sig)):
            code, output, error = self.run_cli("--bootstrap-identity")
        self.assertEqual(code, 0, error)
        self.assertIn(output, {"unchanged", "reconciled_accepted_identity"})


if __name__ == "__main__":
    unittest.main()
