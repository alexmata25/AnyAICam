"""TRUST-1 (Linux 1.2.7.1): the entitlement trust registry is delivered only through the release chain.

Release key -> signed manifest -> package hash -> release.env ENTITLEMENT_TRUST_REGISTRY_SHA256 -> registry. The root
applier (and installer step 13, through the same install_trust_registry()) validates it, refuses a lower generation, a
same-generation different document, a dropped key binding or a backwards lifecycle move, then writes the registry and
the monotonic protected trust state -- before any downtime, so a refusal leaves the running release untouched. The flat
keyset it also ships is the registry's projection (active + retired) for 1.2.7 readers and rollback.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

import anyaicam_agent.updater.release_checks as release_checks
from software_update_helpers import BUILD_B, BUILD_C, MIGRATIONS_V1, manifest_for, release_files, sign, stage_release, write_tarball
from test_software_update_root_applier import ApplierTestCase, ar

KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(range(32, 64))).decode()
KEY_C = base64.b64encode(bytes(range(64, 96))).decode()
PURPOSES = ["analytics", "capacity", "features", "local_access"]


def registry(entries, generation, *, environment="production", issued="2026-10-24T12:00:00+00:00"):
    keys = {}
    for key_id, (public, state, *rest) in entries.items():
        options = rest[0] if rest else {}
        keys[key_id] = {"public_key": public, "state": state, "purposes": options.get("purposes", PURPOSES),
                        "environment": options.get("environment", environment),
                        "changed_generation": options.get("changed_generation", 1)}
    document = {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": environment,
                "generation": generation, "issued_at": issued, "keys": keys}
    return release_checks.canonical_trust_registry(document)


def projection(data: bytes) -> bytes:
    return release_checks.entitlement_keyset_projection(data)


GEN1 = registry({"anyaicam-identity-a": (KEY_A, "active")}, 1)
GEN2 = registry({"anyaicam-identity-a": (KEY_A, "retired", {"changed_generation": 2}),
                 "anyaicam-identity-b": (KEY_B, "active", {"changed_generation": 2})}, 2)


# ------------------------------------------------------------------------------------------- release checks
def test_a_canonical_registry_validates_and_projects_the_flat_keyset():
    parsed = release_checks.validate_entitlement_trust_registry(GEN2)
    assert parsed["generation"] == 2
    assert json.loads(projection(GEN2))["keys"] == {"anyaicam-identity-a": KEY_A, "anyaicam-identity-b": KEY_B}
    revoked = registry({"anyaicam-identity-a": (KEY_A, "revoked", {"changed_generation": 3}),
                        "anyaicam-identity-b": (KEY_B, "active", {"changed_generation": 2})}, 3)
    assert json.loads(projection(revoked))["keys"] == {"anyaicam-identity-b": KEY_B}  # revoked never projected


@pytest.mark.parametrize("label,data", [
    ("not canonical", json.dumps(json.loads(GEN1)).encode()),
    ("private material", GEN1.replace(b'"version"', b'"private_key": "x", "version"')),
    ("lab environment key", registry({"anyaicam-identity-a": (KEY_A, "active"),
                                      "lab-signer": (KEY_B, "active", {"environment": "lab"})}, 1)),
    ("lab namespace in production", registry({"anyaicam-identity-a": (KEY_A, "active"),
                                              "lab-signer": (KEY_B, "active")}, 1)),
    ("test namespace in production", registry({"anyaicam-identity-a": (KEY_A, "active"),
                                               "test-key": (KEY_B, "active")}, 1)),
    ("no active key", registry({"anyaicam-identity-a": (KEY_A, "retired")}, 1)),
    ("not json", b"{nope"),
])
def test_unsafe_or_malformed_registries_are_refused(label, data):
    with pytest.raises(release_checks.ReleaseCheckError) as error:
        release_checks.validate_entitlement_trust_registry(data)
    assert error.value.code == "bad_trust_registry", label


def _tree(tmp_path, files):
    for relative, data in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return tmp_path


def test_release_registry_must_match_release_env_and_ship_its_projection(tmp_path):
    good = {"payload/keys/entitlement-trust-registry.json": GEN1,
            "payload/keys/entitlement-signing-public-keys.json": projection(GEN1)}
    env = {"ENTITLEMENT_TRUST_REGISTRY_SHA256": hashlib.sha256(GEN1).hexdigest(),
           "ENTITLEMENT_SIGNING_KEYS_SHA256": hashlib.sha256(projection(GEN1)).hexdigest()}
    assert release_checks.release_entitlement_trust_registry(_tree(tmp_path / "ok", good), env) == GEN1
    with pytest.raises(release_checks.ReleaseCheckError, match="does not match release.env"):
        release_checks.release_entitlement_trust_registry(
            _tree(tmp_path / "sha", good), dict(env, ENTITLEMENT_TRUST_REGISTRY_SHA256="0" * 64))
    with pytest.raises(release_checks.ReleaseCheckError, match="does not name"):
        release_checks.release_entitlement_trust_registry(_tree(tmp_path / "unnamed", good),
                                                          {"ENTITLEMENT_SIGNING_KEYS_SHA256": env["ENTITLEMENT_SIGNING_KEYS_SHA256"]})
    with pytest.raises(release_checks.ReleaseCheckError, match="projection"):
        release_checks.release_entitlement_trust_registry(
            _tree(tmp_path / "proj", dict(good, **{"payload/keys/entitlement-signing-public-keys.json":
                                                   projection(GEN2)})),
            dict(env, ENTITLEMENT_SIGNING_KEYS_SHA256=hashlib.sha256(projection(GEN2)).hexdigest()))
    assert release_checks.release_entitlement_trust_registry(_tree(tmp_path / "none", {}), {}) is None


# ------------------------------------------------------------------------------------------- the root applier
class RegistryTestCase(ApplierTestCase):
    def setUp(self):
        super().setUp()
        base = self.paths.trusted_key.parent
        self.paths.entitlement_keys = base / "entitlement_signing_keys.json"
        self.paths.entitlement_registry = base / "entitlement_trust_registry.json"
        self.paths.entitlement_trust_state = base / "entitlement_trust_state.json"

    def stage(self, data, *, version="1.2.0", build=BUILD_B, **extra):
        files = release_files(version, build, migrations=MIGRATIONS_V1, entitlement_keyset=projection(data),
                              entitlement_registry=data, **extra)
        package = write_tarball(self.tmp / f"pkg-{version}.tar.gz", files)
        manifest = manifest_for(package, version=version, build_id=build)
        stage_release(self.paths.staged, manifest, sign(self.private_key, manifest), package)

    def snapshot_files(self):
        return {path.name: path.read_bytes() for path in (self.paths.entitlement_registry,
                                                          self.paths.entitlement_trust_state,
                                                          self.paths.entitlement_keys) if path.exists()}

    def state(self):
        return json.loads(self.paths.entitlement_trust_state.read_text())


class ApplierRegistryTests(RegistryTestCase):
    def test_a_signed_release_installs_registry_state_and_projection_before_downtime(self):
        self.stage(GEN1)
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "healthy", result.get("error"))
        self.assertEqual(result["entitlement_trust_registry"], "installed")
        self.assertEqual(self.paths.entitlement_registry.read_bytes(), GEN1)
        self.assertEqual(self.paths.entitlement_keys.read_bytes(), projection(GEN1))
        state = self.state()
        self.assertEqual((state["generation"], state["registry_sha256"]), (1, hashlib.sha256(GEN1).hexdigest()))
        calls = self.host.calls
        stop = next(i for i, call in enumerate(calls) if call[:2] == ["systemctl", "stop"])
        migrate = next(i for i, call in enumerate(calls) if call[-1:] == ["--migrate-trust-state"])
        self.assertLess(migrate, stop)
        if os.name == "posix":
            for path in (self.paths.entitlement_registry, self.paths.entitlement_trust_state):
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_rotation_then_an_identical_repair_replay_is_a_no_op(self):
        self.stage(GEN1)
        self.assertEqual(self.applier().apply_staged()["state"], "healthy")
        self.stage(GEN2, version="1.3.0", build=BUILD_C)
        result = self.applier().apply_staged()
        self.assertEqual((result["state"], result["entitlement_trust_registry"]), ("healthy", "installed"))
        self.assertEqual(self.state()["generation"], 2)
        before = self.snapshot_files()
        self.assertEqual(ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state,
                                                   GEN2), "unchanged")
        self.assertEqual(self.snapshot_files(), before)

    def test_a_lower_generation_release_is_refused_with_nothing_changed(self):
        """U-T3 (install side)."""
        ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, GEN1)
        ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, GEN2)
        before = self.snapshot_files()
        self.stage(GEN1)
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "install_failed")
        self.assertIn("registry_rollback", result["error"])
        self.assertEqual(self.snapshot_files(), before)
        self.assertFalse(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls))
        self.assertEqual(self.marker()["release_version"], "1.1.0")

    def test_a_tampered_same_generation_or_dropped_binding_is_refused(self):
        ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, GEN2)
        before = self.snapshot_files()
        tampered = registry({"anyaicam-identity-a": (KEY_A, "retired", {"changed_generation": 2}),
                             "anyaicam-identity-b": (KEY_B, "active", {"changed_generation": 2})}, 2,
                            issued="2026-10-24T13:00:00+00:00")
        dropped = registry({"anyaicam-identity-b": (KEY_B, "active", {"changed_generation": 2}),
                            "anyaicam-identity-c": (KEY_C, "active", {"changed_generation": 3})}, 3)
        backwards = registry({"anyaicam-identity-a": (KEY_A, "active", {"changed_generation": 3}),
                              "anyaicam-identity-b": (KEY_B, "active", {"changed_generation": 2})}, 3)
        for label, data, reason in (("same generation", tampered, "registry_rollback"),
                                    ("dropped binding", dropped, "dropped_binding"),
                                    ("retired back to active", backwards, "lifecycle_backwards")):
            with self.subTest(label), self.assertRaisesRegex(ar.TrustAnchorError, reason):
                ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, data)
            self.assertEqual(self.snapshot_files(), before)

    def test_a_deleted_registry_is_restored_by_reinstalling_the_current_generation_only(self):
        """Green 9.5 recovery: the protected state remembers generation and digest."""
        ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, GEN2)
        state_before = self.paths.entitlement_trust_state.read_bytes()
        self.paths.entitlement_registry.unlink()
        with self.assertRaisesRegex(ar.TrustAnchorError, "registry_rollback"):
            ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, GEN1)
        self.assertFalse(self.paths.entitlement_registry.exists())
        self.assertEqual(ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state,
                                                   GEN2), "restored")
        self.assertEqual(self.paths.entitlement_registry.read_bytes(), GEN2)
        self.assertEqual(self.paths.entitlement_trust_state.read_bytes(), state_before)

    def test_a_release_without_a_registry_keeps_the_installed_one(self):
        ar.install_trust_registry(self.paths.entitlement_registry, self.paths.entitlement_trust_state, GEN1)
        before = self.snapshot_files()
        files = release_files("1.2.0", BUILD_B, migrations=MIGRATIONS_V1)
        package = write_tarball(self.tmp / "pkg-none.tar.gz", files)
        manifest = manifest_for(package, version="1.2.0", build_id=BUILD_B)
        stage_release(self.paths.staged, manifest, sign(self.private_key, manifest), package)
        result = self.applier().apply_staged()
        self.assertEqual((result["state"], result["entitlement_trust_registry"]), ("healthy", "unchanged"))
        self.assertEqual(self.snapshot_files(), before)

    def test_the_applier_uses_fixed_registry_and_state_paths(self):
        self.assertEqual(ar.Paths().entitlement_registry, Path("/etc/anyaicam-update/entitlement_trust_registry.json"))
        self.assertEqual(ar.Paths().entitlement_trust_state, Path("/etc/anyaicam-update/entitlement_trust_state.json"))
