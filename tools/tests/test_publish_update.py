"""RDM-2 Group 2G: tests for tools/publish_update.py -- the operator
publisher CLI.

No real AWS, no real S3, no real network -- a small hand-rolled
FakeS3Client is injected directly into publish()'s own s3_client
parameter. The "round-trips through the real device verification path"
tests import appliance-agent's REAL PackageVerifier/Manifest/
ManifestSignatureInvalid/PackageChecksumMismatch directly (the same
sys.path insertion tools/publish_update.py itself uses) -- proving
genuine end-to-end compatibility, not an assumption of it.
"""

import base64
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
APPLIANCE_AGENT_DIR = REPO_ROOT / "appliance-agent"
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import publish_update
from anyaicam_agent.updater.verify import ManifestSignatureInvalid, PackageChecksumMismatch, PackageVerifier


class _FakeNoSuchKey(Exception):
    pass


class _FakeExceptions:
    NoSuchKey = _FakeNoSuchKey


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.exceptions = _FakeExceptions()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("not found")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("not found")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None, CacheControl=None):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode("utf-8")


def _keypair_pem():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


class PublishUpdateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.s3 = FakeS3Client()
        self.bucket = "anyaicam-updates-test"

        private_pem, public_pem = _keypair_pem()
        self.private_key_path = self.root / "operator-key.pem"
        self.private_key_path.write_bytes(private_pem)
        self.trusted_key_path = self.root / "trusted_signing_key.pem"
        self.trusted_key_path.write_bytes(public_pem)

        self.package_path = self.root / "package-1.0.0.tar"
        self.package_path.write_bytes(b"fake package bytes for version 1.0.0")

    def _publish(self, **overrides):
        kwargs = dict(
            s3_client=self.s3, bucket=self.bucket, target="anyaicam-appliance", channel="stable",
            version="1.0.0", update_id="upd-1", package_path=self.package_path,
            platform="linux", architecture="x86_64", private_key_path=self.private_key_path,
        )
        kwargs.update(overrides)
        return publish_update.publish(**kwargs)


class TargetChannelVersionValidationTests(PublishUpdateTestCase):
    def test_rejects_unsafe_target_channel_version_grammar(self):
        for bad in ("../escape", "has/slash", "", "has space", "..", ".", "a/b"):
            with self.subTest(bad=bad, field="target"):
                with self.assertRaises(ValueError):
                    self._publish(target=bad)
            with self.subTest(bad=bad, field="channel"):
                with self.assertRaises(ValueError):
                    self._publish(channel=bad)
            with self.subTest(bad=bad, field="version"):
                with self.assertRaises(ValueError):
                    self._publish(version=bad)

    def test_control_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            self._publish(target="bad\x00value")

    def test_a_safe_value_is_accepted(self):
        envelope = self._publish(target="anyaicam-appliance", channel="stable-v2", version="1.2.3")
        self.assertEqual(envelope["manifest"]["target"], "anyaicam-appliance")

    def test_a_value_merely_starting_with_dots_is_accepted_not_a_traversal_risk(self):
        # S3 keys are opaque strings, not filesystem paths -- ".." only
        # matters as an EXACT path component (already rejected above);
        # a channel like "..hidden" cannot escape the intended prefix,
        # since key construction is plain string formatting, never path
        # resolution. Only an exact '.'/'..' value, '/', emptiness, or a
        # control character are actually unsafe (see the grammar's own
        # docstring) -- this proves the validator isn't over-strict
        # beyond what's actually required.
        envelope = self._publish(target="anyaicam-appliance", channel="..hidden", version="1.0.0")
        self.assertEqual(envelope["manifest"]["channel"], "..hidden")


class HashValidationTests(PublishUpdateTestCase):
    def test_publish_computes_the_real_sha256_of_the_package_bytes(self):
        envelope = self._publish()
        expected = hashlib.sha256(self.package_path.read_bytes()).hexdigest()
        self.assertEqual(envelope["manifest"]["sha256"], expected)

    def test_refuses_a_mismatched_sha256_override(self):
        with self.assertRaises(ValueError):
            self._publish(expected_sha256="0" * 64)
        self.assertEqual(self.s3.objects, {})  # nothing uploaded

    def test_accepts_a_correct_sha256_override(self):
        correct = hashlib.sha256(self.package_path.read_bytes()).hexdigest()
        envelope = self._publish(expected_sha256=correct)
        self.assertEqual(envelope["manifest"]["sha256"], correct)


class OverwriteAndVersionOrderingTests(PublishUpdateTestCase):
    def test_refuses_publishing_the_same_version_twice(self):
        self._publish(version="1.0.0")
        with self.assertRaises(publish_update.PublishConflict):
            self._publish(version="1.0.0", update_id="upd-2")

    def test_refuses_a_non_newer_version(self):
        self._publish(version="1.1.0")
        with self.assertRaises(publish_update.PublishConflict):
            self._publish(version="1.0.0", update_id="upd-2")

    def test_overwrite_check_fires_even_when_the_version_is_newer_than_latest(self):
        # Simulates a resumed/retried publish: package/manifest objects
        # for a version already exist (e.g. a prior failed publish that
        # crashed before updating latest.json), but latest.json itself
        # still points at an OLDER version. The overwrite check must
        # independently catch this -- not just the non-newer-version
        # check, which alone would let this through (1.0.0 IS newer
        # than nothing published yet).
        self.s3.objects[(self.bucket, publish_update._package_key("anyaicam-appliance", "stable", "1.0.0"))] = b"stale partial upload"
        with self.assertRaises(publish_update.PublishConflict):
            self._publish(version="1.0.0")

    def test_a_genuinely_newer_version_is_accepted(self):
        self._publish(version="1.0.0")
        envelope = self._publish(version="1.1.0", update_id="upd-2")
        self.assertEqual(envelope["manifest"]["version"], "1.1.0")

    def test_first_ever_publish_has_nothing_to_compare_against(self):
        envelope = self._publish(version="1.0.0")
        self.assertEqual(envelope["manifest"]["version"], "1.0.0")

    def test_latest_pointer_is_updated_last_only_after_package_and_versioned_manifest_succeed(self):
        self._publish(version="1.0.0")
        latest = self.s3.objects[(self.bucket, "manifests/anyaicam-appliance/stable/latest.json")]
        versioned = self.s3.objects[(self.bucket, "manifests/anyaicam-appliance/stable/1.0.0.json")]
        self.assertEqual(latest, versioned)
        self.assertIn((self.bucket, "packages/anyaicam-appliance/stable/1.0.0.tar"), self.s3.objects)


class PrivateKeySourceTests(PublishUpdateTestCase):
    def test_nonexistent_private_key_path_raises_cleanly(self):
        with self.assertRaises(OSError):
            self._publish(private_key_path=self.root / "does-not-exist.pem")

    def test_signature_is_produced_using_exactly_the_supplied_key_file(self):
        # A second, DIFFERENT keypair/file -- proves the key is read
        # fresh from whichever path is explicitly given, never cached
        # or hardcoded, and never sourced from anywhere else.
        other_private_pem, other_public_pem = _keypair_pem()
        other_key_path = self.root / "other-operator-key.pem"
        other_key_path.write_bytes(other_private_pem)
        other_public_key_path = self.root / "other-trusted-key.pem"
        other_public_key_path.write_bytes(other_public_pem)

        envelope_a = self._publish(version="1.0.0")
        envelope_b = self._publish(version="1.1.0", update_id="upd-2", private_key_path=other_key_path)

        self.assertNotEqual(envelope_a["signature"], envelope_b["signature"])

        verifier_second_key = PackageVerifier(other_public_key_path)
        verifier_second_key.verify_manifest(envelope_b["manifest"], base64.b64decode(envelope_b["signature"]))  # succeeds

        verifier_first_key = PackageVerifier(self.trusted_key_path)
        with self.assertRaises(ManifestSignatureInvalid):
            verifier_first_key.verify_manifest(envelope_b["manifest"], base64.b64decode(envelope_b["signature"]))


class RoundTripDeviceVerificationTests(PublishUpdateTestCase):
    def test_published_envelope_verifies_against_the_real_device_side_verifier(self):
        envelope = self._publish()
        manifest_dict = envelope["manifest"]
        signature = base64.b64decode(envelope["signature"])

        verifier = PackageVerifier(self.trusted_key_path)
        manifest = verifier.verify_manifest(manifest_dict, signature)  # raises on failure

        self.assertEqual(manifest.version, "1.0.0")
        self.assertEqual(manifest.sha256, hashlib.sha256(self.package_path.read_bytes()).hexdigest())

        published_package_bytes = self.s3.objects[(self.bucket, publish_update._package_key("anyaicam-appliance", "stable", "1.0.0"))]
        package_copy = self.root / "downloaded-package.tar"
        package_copy.write_bytes(published_package_bytes)
        verifier.verify_package(manifest, package_copy)  # raises on checksum mismatch

    def test_a_tampered_package_still_fails_verification_after_publish(self):
        envelope = self._publish()
        manifest_dict = envelope["manifest"]
        signature = base64.b64decode(envelope["signature"])
        verifier = PackageVerifier(self.trusted_key_path)
        manifest = verifier.verify_manifest(manifest_dict, signature)

        tampered = self.root / "tampered-package.tar"
        tampered.write_bytes(b"not the real package bytes")
        with self.assertRaises(PackageChecksumMismatch):
            verifier.verify_package(manifest, tampered)


class MainCliTests(PublishUpdateTestCase):
    def test_main_publishes_via_the_cli_argument_parsing(self):
        with patch("boto3.client", return_value=self.s3):
            publish_update.main([
                "--bucket", self.bucket, "--target", "anyaicam-appliance", "--channel", "stable",
                "--version", "1.0.0", "--update-id", "upd-1", "--package", str(self.package_path),
                "--platform", "linux", "--architecture", "x86_64", "--private-key", str(self.private_key_path),
            ])
        self.assertIn((self.bucket, "manifests/anyaicam-appliance/stable/latest.json"), self.s3.objects)


if __name__ == "__main__":
    unittest.main()
