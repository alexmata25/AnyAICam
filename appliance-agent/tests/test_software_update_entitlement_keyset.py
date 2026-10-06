"""Software Update carries the paid-feature entitlement trust anchor
(2026-10-05, Codex review of 5c1556e): an already-installed appliance gets
the cloud's entitlement-signing PUBLIC keyset from a signed release, so it
does not have to re-run the installer.

The keyset is trusted only because the release is: the manifest signature
(release-signing key, root-owned on the appliance) covers the package hash,
which covers release.env (naming the keyset's SHA-256) and the keyset file.
The root applier validates it before anything changes and installs it
root:root 0644 at the fixed path the VMS reads, before any downtime; any
failure stops the update with the running release and the existing keyset
untouched.
"""
import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from unittest import mock

from software_update_helpers import (BUILD_B, MIGRATIONS_V1, generate_keypair, manifest_for, release_files, sign,
                                     stage_release, write_tarball)
from test_software_update_root_applier import ApplierTestCase, ar

import anyaicam_agent.updater.release_checks as release_checks

KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(range(32, 64))).decode()


def generated_private_pem() -> bytes:
    """A throwaway private key generated for this run (never a literal)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption())


def keyset(**keys) -> bytes:
    return release_checks.canonical_entitlement_keyset(keys)


class KeysetTestCase(ApplierTestCase):
    def setUp(self):
        super().setUp()
        self.paths.entitlement_keys = self.paths.trusted_key.parent / "entitlement_signing_keys.json"

    def stage_with(self, *, version="1.2.0", build=BUILD_B, signer=None, tamper=None, **keyset_args):
        files = release_files(version, build, migrations=MIGRATIONS_V1, **keyset_args)
        package = write_tarball(self.tmp / f"pkg-{version}.tar.gz", files)
        manifest = manifest_for(package, version=version, build_id=build)
        signature = sign(signer or self.private_key, manifest)
        if tamper:  # changes the package after it was signed
            files = dict(files)
            tamper(files)
            write_tarball(package, files)
        stage_release(self.paths.staged, manifest, signature, package)
        return manifest

    def anchor(self):
        return self.paths.entitlement_keys.read_bytes() if self.paths.entitlement_keys.exists() else None

    def install_existing(self, data: bytes):
        self.paths.entitlement_keys.write_bytes(data)


class ProvisioningTests(KeysetTestCase):
    def test_a_signed_release_installs_the_keyset_on_an_existing_appliance(self):
        self.assertIsNone(self.anchor())  # installed before entitlement keys existed
        self.stage_with(entitlement_keyset=keyset(**{"cloud-1": KEY_A}))
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "healthy", result.get("error"))
        self.assertEqual(result["entitlement_keyset"], "updated")
        self.assertEqual(self.anchor(), keyset(**{"cloud-1": KEY_A}))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.paths.entitlement_keys).st_mode), 0o644)

    def test_the_keyset_is_installed_before_any_downtime(self):
        self.stage_with(entitlement_keyset=keyset(**{"cloud-1": KEY_A}))
        seen = []
        original = ar.safe_replace

        def recording(directory, name, data, **kwargs):
            if name == "entitlement_signing_keys.json":
                seen.append(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls))
            return original(directory, name, data, **kwargs)
        with mock.patch.object(ar, "safe_replace", recording):
            self.assertEqual(self.applier().apply_staged()["state"], "healthy")
        self.assertEqual(seen, [False])

    def test_rotation_replaces_the_keyset_with_old_and_new_keys(self):
        self.install_existing(keyset(**{"cloud-1": KEY_A}))
        self.stage_with(entitlement_keyset=keyset(**{"cloud-1": KEY_A, "cloud-2": KEY_B}))
        self.assertEqual(self.applier().apply_staged()["state"], "healthy")
        self.assertEqual(json.loads(self.anchor())["keys"], {"cloud-1": KEY_A, "cloud-2": KEY_B})

    def test_a_release_without_a_keyset_leaves_the_existing_one(self):
        self.install_existing(keyset(**{"cloud-1": KEY_A}))
        self.stage_with()
        result = self.applier().apply_staged()
        self.assertEqual((result["state"], result["entitlement_keyset"]), ("healthy", "unchanged"))
        self.assertEqual(self.anchor(), keyset(**{"cloud-1": KEY_A}))


class RefusalTests(KeysetTestCase):
    """Nothing here may change the trust anchor or the running release."""

    def assert_refused(self, result, *, expected_anchor):
        self.assertIn(result["state"], ("rejected", "install_failed"), result)
        self.assertEqual(self.anchor(), expected_anchor)
        self.assertFalse(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls), "the VMS was stopped")
        self.assertEqual(self.marker()["release_version"], "1.1.0")

    def setUp(self):
        super().setUp()
        self.existing = keyset(**{"cloud-1": KEY_A})
        self.install_existing(self.existing)

    def test_a_keyset_that_does_not_match_release_env_is_refused(self):
        self.stage_with(entitlement_keyset=keyset(**{"attacker": KEY_B}), entitlement_keyset_sha256="0" * 64)
        result = self.applier().apply_staged()
        self.assert_refused(result, expected_anchor=self.existing)
        self.assertIn("bad_keyset", result["error"])

    def test_a_keyset_release_env_does_not_name_is_refused(self):
        self.stage_with(entitlement_keyset=keyset(**{"attacker": KEY_B}), entitlement_keyset_sha256="")
        self.assert_refused(self.applier().apply_staged(), expected_anchor=self.existing)

    def test_release_env_naming_a_missing_keyset_is_refused(self):
        self.stage_with(entitlement_keyset_sha256="a" * 64)
        self.assert_refused(self.applier().apply_staged(), expected_anchor=self.existing)

    def test_private_or_malformed_keysets_are_refused_even_when_their_hash_matches(self):
        bad = {
            "private PEM": generated_private_pem(),
            "private field": json.dumps({"keys": {"k": KEY_A}, "private_key_b64": KEY_B}).encode(),
            "extra field": json.dumps({"keys": {"k": KEY_A}, "note": "x"}, sort_keys=True, indent=2).encode() + b"\n",
            "64-byte value": keyset(k=base64.b64encode(bytes(64)).decode()),
            "short value": keyset(k="AAAA"),
            "bad id": keyset(**{"bad id": KEY_A}),
            "empty": keyset(),
            "not canonical": json.dumps({"keys": {"k": KEY_A}}).encode(),
            "not JSON": b"not json",
        }
        for label, data in bad.items():
            with self.subTest(label):
                self.host.calls.clear()
                self.stage_with(entitlement_keyset=data)
                result = self.applier().apply_staged()
                self.assert_refused(result, expected_anchor=self.existing)

    def test_a_package_changed_after_signing_cannot_replace_the_keyset(self):
        def swap(files):
            files["payload/keys/entitlement-signing-public-keys.json"] = keyset(attacker=KEY_B)
        self.stage_with(entitlement_keyset=keyset(**{"cloud-1": KEY_A, "cloud-2": KEY_B}), tamper=swap)
        result = self.applier().apply_staged()
        self.assert_refused(result, expected_anchor=self.existing)
        self.assertIn("bad_hash", result["error"])

    def test_a_release_signed_by_an_untrusted_key_cannot_replace_the_keyset(self):
        other, _public = generate_keypair()
        self.stage_with(entitlement_keyset=keyset(attacker=KEY_B), signer=other)
        result = self.applier().apply_staged()
        self.assert_refused(result, expected_anchor=self.existing)
        self.assertIn("bad_signature", result["error"])

    def test_a_failed_keyset_write_stops_the_update_with_nothing_changed(self):
        self.stage_with(entitlement_keyset=keyset(**{"cloud-1": KEY_A, "cloud-2": KEY_B}))
        original = ar.safe_replace

        def failing(directory, name, data, **kwargs):
            if name == "entitlement_signing_keys.json":
                raise OSError("disk full")
            return original(directory, name, data, **kwargs)
        with mock.patch.object(ar, "safe_replace", failing):
            result = self.applier().apply_staged()
        self.assert_refused(result, expected_anchor=self.existing)
        self.assertIn("trust_anchor_write_failed", result["error"])

    def test_a_missing_trust_anchor_directory_stops_the_update(self):
        self.paths.entitlement_keys = self.tmp / "no-such-dir" / "entitlement_signing_keys.json"
        self.stage_with(entitlement_keyset=keyset(**{"cloud-1": KEY_A}))
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "install_failed")
        self.assertIn("trust_anchor_unsafe", result["error"])
        self.assertFalse(self.paths.entitlement_keys.exists())


class FixedPathTests(KeysetTestCase):
    def test_the_applier_and_the_vms_use_the_same_fixed_path(self):
        self.assertEqual(ar.Paths().entitlement_keys, Path("/etc/anyaicam-update/entitlement_signing_keys.json"))
        app_source = (Path(__file__).resolve().parents[2] / "app" / "appliance_entitlements.py").read_text(encoding="utf-8")
        self.assertIn('TRUST_ANCHOR_FILE = Path("/etc/anyaicam-update/entitlement_signing_keys.json")', app_source)

    def test_the_applier_takes_no_arguments_or_environment_for_the_path(self):
        source = (Path(__file__).resolve().parents[1] / "system" / "apply_release.py").read_text(encoding="utf-8")
        self.assertNotIn("ANYAICAM_UPDATE_KEY_DIR", source)
        self.assertNotIn("os.environ", source.split("class Paths", 1)[1].split("@property", 1)[0])


def test_one_canonical_keyset_form_everywhere():
    """Builder, applier checks and the cloud digest tool agree byte for byte."""
    import importlib.util
    import sys
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "installer"))
    sys.path.insert(0, str(repo / "app"))
    import build_release_installer as builder
    spec = importlib.util.spec_from_file_location("entitlement_keyset_tool", repo / "app" / "entitlement_keyset.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    keys = {"z-key": KEY_B, "a-key": KEY_A}
    assert builder.canonical_entitlement_keyset(keys) == release_checks.canonical_entitlement_keyset(keys) == tool.canonical(keys)
    assert tool.digest(keys) == hashlib.sha256(release_checks.canonical_entitlement_keyset(keys)).hexdigest()
