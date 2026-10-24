"""TRUST-1 (Linux 1.2.7.1): the release builder packages the entitlement trust registry (verified against a digest
obtained independently on the cloud host) and its flat keyset projection; installer step 13 installs the registry and
the monotonic protected trust state through the root applier's install_trust_registry() (registry first, then state),
refusing a lower generation, a different document at the same generation, a dropped binding or a backwards lifecycle
move; step 16 runs capacity_accept.py --migrate-trust-state; validate.sh checks registry and state. A 1.2.7.1+ build
must carry a registry, so a Linux 1.2.7.1 appliance never runs in legacy (flat keyset) mode after install."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
REPO = INSTALLER.parent
sys.path.insert(0, str(INSTALLER))

import build_release_installer as builder  # noqa: E402

BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(range(32, 64))).decode()
PURPOSES = ["analytics", "capacity", "features", "local_access"]


def registry(entries, generation, *, environment="production"):
    keys = {key_id: {"public_key": public, "state": state, "purposes": PURPOSES,
                     "environment": key_environment or environment, "changed_generation": changed}
            for key_id, (public, state, changed, key_environment) in entries.items()}
    document = {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": environment,
                "generation": generation, "issued_at": "2026-10-24T12:00:00+00:00", "keys": keys}
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("ascii")


GEN1 = registry({"anyaicam-identity-a": (KEY_A, "active", 1, None)}, 1)
GEN2 = registry({"anyaicam-identity-a": (KEY_A, "retired", 2, None), "anyaicam-identity-b": (KEY_B, "active", 2, None)}, 2)


def to_bash(path) -> str:
    text = Path(path).as_posix()
    return "/" + text[0].lower() + text[2:] if os.name == "nt" and text[1:2] == ":" else text


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class BuilderRegistryTests(unittest.TestCase):
    def file(self, data: bytes) -> str:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "registry.json"
        path.write_bytes(data)
        return str(path)

    def test_the_registry_matching_the_independent_digest_is_packaged_with_its_projection(self):
        registry_bytes, keyset_bytes = builder.read_entitlement_trust_registry(self.file(GEN2),
                                                                              hashlib.sha256(GEN2).hexdigest())
        self.assertEqual(registry_bytes, GEN2)
        self.assertEqual(json.loads(keyset_bytes)["keys"], {"anyaicam-identity-a": KEY_A, "anyaicam-identity-b": KEY_B})
        self.assertEqual(keyset_bytes, builder.canonical_entitlement_keyset(json.loads(keyset_bytes)["keys"]))

    def test_a_registry_that_does_not_match_the_digest_or_carries_a_lab_key_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "does not match"):
            builder.read_entitlement_trust_registry(self.file(GEN1), "0" * 64)
        lab = registry({"anyaicam-identity-a": (KEY_A, "active", 1, None), "lab-signer": (KEY_B, "active", 1, "lab")}, 1)
        with self.assertRaisesRegex(SystemExit, "lab|environment"):
            builder.read_entitlement_trust_registry(self.file(lab), hashlib.sha256(lab).hexdigest())

    def _run(self, extra):
        cmd = [sys.executable, str(INSTALLER / "build_release_installer.py"), "--vms-commit", "0" * 40,
               "--vms-repo", "/nonexistent-repo-path-never-reached", "--no-mediamtx", "--no-update-signing-key"] + extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_a_1271_build_must_carry_the_trust_registry(self):
        flat = self.file((json.dumps({"keys": {"k1": KEY_A}}, sort_keys=True, indent=2) + "\n").encode())
        digest = hashlib.sha256(Path(flat).read_bytes()).hexdigest()
        for extra in (["--entitlement-signing-public-keys", flat, "--entitlement-signing-keys-sha256", digest],
                      ["--no-entitlement-signing-keys"]):
            with self.subTest(extra=extra[0]):
                result = self._run(["--release-version", "1.2.7.1"] + extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--entitlement-trust-registry", result.stderr + result.stdout)

    def test_registry_and_flat_keyset_flags_are_mutually_exclusive(self):
        result = self._run(["--release-version", "1.2.7.1", "--entitlement-trust-registry", self.file(GEN1),
                            "--entitlement-trust-registry-sha256", hashlib.sha256(GEN1).hexdigest(),
                            "--no-entitlement-signing-keys"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", result.stderr + result.stdout)

    def test_release_env_and_manifest_carry_the_registry_hash(self):
        source = _text(INSTALLER / "build_release_installer.py")
        self.assertIn('f"ENTITLEMENT_TRUST_REGISTRY_SHA256={entitlement_registry_sha256}\\n"', source)
        self.assertIn('"entitlement_trust_registry_sha256": entitlement_registry_sha256', source)
        self.assertIn('package / "payload/keys/entitlement-trust-registry.json"', source)


CAN_INSTALL_HERE = os.name != "posix" or os.geteuid() == 0


@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class RegistryProvisioningTests(unittest.TestCase):
    """13-entitlement-signing-keys.sh run for real with the fixed destinations re-pointed after sourcing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.payload = self.tmp / "payload"
        (self.payload / "keys").mkdir(parents=True)
        agent = REPO / "appliance-agent"
        shutil.copytree(agent / "system", self.payload / "agent" / "system")
        shutil.copytree(agent / "anyaicam_agent", self.payload / "agent" / "anyaicam_agent")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        shim = self.bin / "python3"
        shim.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', newline="\n")
        shim.chmod(0o755)
        self.dir = self.tmp / "etc-anyaicam-update"

    def files(self):
        return {name: (self.dir / name).read_bytes() for name in ("entitlement_trust_registry.json",
                                                                  "entitlement_trust_state.json",
                                                                  "entitlement_signing_keys.json")
                if (self.dir / name).exists()}

    def run_step(self, registry_bytes, *, registry_sha=None):
        _, keyset_bytes = builder.read_entitlement_trust_registry(self._write(registry_bytes),
                                                                 hashlib.sha256(registry_bytes).hexdigest())
        (self.payload / "keys" / "entitlement-trust-registry.json").write_bytes(registry_bytes)
        (self.payload / "keys" / "entitlement-signing-public-keys.json").write_bytes(keyset_bytes)
        d = to_bash(self.dir)
        script = (f'set -e; log(){{ echo "$*"; }}; PAYLOAD_DIR="{to_bash(self.payload)}"; '
                  f'ENTITLEMENT_SIGNING_KEYS_SHA256="{hashlib.sha256(keyset_bytes).hexdigest()}"; '
                  f'ENTITLEMENT_TRUST_REGISTRY_SHA256="{registry_sha or hashlib.sha256(registry_bytes).hexdigest()}"; '
                  f'source "{to_bash(INSTALLER / "13-entitlement-signing-keys.sh")}"; '
                  f'ENTITLEMENT_KEYS_DIR="{d}"; ENTITLEMENT_KEYS_FILE="{d}/entitlement_signing_keys.json"; '
                  f'ENTITLEMENT_REGISTRY_FILE="{d}/entitlement_trust_registry.json"; '
                  f'ENTITLEMENT_TRUST_STATE_FILE="{d}/entitlement_trust_state.json"; '
                  f'provision_entitlement_signing_keys')
        env = dict(os.environ, PATH=to_bash(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)

    def _write(self, data):
        path = self.tmp / f"in-{hashlib.sha256(data).hexdigest()[:8]}.json"
        path.write_bytes(data)
        return str(path)

    @unittest.skipUnless(CAN_INSTALL_HERE, "installing needs a root-owned destination (run as root)")
    def test_step_13_installs_registry_then_state_then_projection(self):
        result = self.run_step(GEN1)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.files()
        self.assertEqual(files["entitlement_trust_registry.json"], GEN1)
        state = json.loads(files["entitlement_trust_state.json"])
        self.assertEqual((state["generation"], state["registry_sha256"]), (1, hashlib.sha256(GEN1).hexdigest()))
        self.assertEqual(json.loads(files["entitlement_signing_keys.json"])["keys"], {"anyaicam-identity-a": KEY_A})
        for name in files:
            info = os.stat(self.dir / name)
            self.assertEqual((info.st_uid, info.st_mode & 0o777), (0, 0o644), name)

    @unittest.skipUnless(CAN_INSTALL_HERE, "installing needs a root-owned destination (run as root)")
    def test_step_13_rotation_repair_and_rollback_refusal(self):
        self.assertEqual(self.run_step(GEN1).returncode, 0)
        self.assertEqual(self.run_step(GEN2).returncode, 0)
        before = self.files()
        repair = self.run_step(GEN2)  # installer --repair with the same release: no-op
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual(self.files(), before)
        older = self.run_step(GEN1)
        self.assertNotEqual(older.returncode, 0)
        self.assertIn("registry_rollback", older.stderr)
        self.assertEqual(self.files(), before)
        (self.dir / "entitlement_trust_registry.json").unlink()
        restored = self.run_step(GEN2)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.files(), before)

    def test_step_13_refuses_a_registry_that_does_not_match_release_env(self):
        result = self.run_step(GEN1, registry_sha="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertFalse(self.dir.exists())


class StepWiringTests(unittest.TestCase):
    def test_step_13_uses_the_applier_routine_and_fixed_paths(self):
        step = _text(INSTALLER / "13-entitlement-signing-keys.sh")
        self.assertIn('ENTITLEMENT_REGISTRY_FILE="$ENTITLEMENT_KEYS_DIR/entitlement_trust_registry.json"', step)
        self.assertIn('ENTITLEMENT_TRUST_STATE_FILE="$ENTITLEMENT_KEYS_DIR/entitlement_trust_state.json"', step)
        self.assertIn("apply_release.install_trust_registry(", step)
        self.assertIn("release_checks.validate_entitlement_trust_registry(", step)
        self.assertLess(step.index("apply_release.install_trust_registry("),
                        step.index("apply_release.install_trust_anchor("))

    def test_step_16_migrates_the_accepted_state_under_the_lock(self):
        library = _text(REPO / "appliance-agent" / "scripts" / "lib-capacity-accept.sh")
        self.assertIn('"$privileged_dir/capacity_accept.py" --migrate-trust-state', library)
        step = _text(INSTALLER / "16-capacity-accept.sh")
        self.assertIn("migrate_capacity_trust_state", step)

    def test_validate_checks_registry_and_trust_state(self):
        script = _text(INSTALLER / "validate.sh")
        self.assertIn('check "Entitlement trust registry and protected trust state are provisioned root-owned" '
                      'entitlement_trust_registry_ok', script)
        self.assertIn("ENTITLEMENT_TRUST_REGISTRY_SHA256", script)


if __name__ == "__main__":
    unittest.main()
