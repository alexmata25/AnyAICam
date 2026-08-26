"""RDM-1: focused tests for anyaicam_agent.updater.verify -- canonical
manifest serialization, pinned-public-key loading, RSA signature
verification, SHA-256 package checksum verification, and the
PackageVerifier pipeline.

Deliberately self-contained: no network, no AWS, no real production key
material -- every keypair here is a throwaway in-memory RSA key generated
fresh per test run, mirroring app/tests/test_live_cdn_signing.py's
established style for this codebase's crypto-adjacent tests.
"""

import json
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.updater.models import Manifest
from anyaicam_agent.updater.verify import (
    ManifestSignatureInvalid,
    PackageChecksumMismatch,
    PackageTargetMismatch,
    PackageVerifier,
    TrustedKeyUnavailable,
    canonical_manifest_bytes,
    load_trusted_public_key,
    sha256_of_file,
    verify_manifest_signature,
    verify_package_checksum,
)

VALID_MANIFEST_DICT = {
    "update_id": "upd-1",
    "version": "1.2.0",
    "sha256": "a" * 64,
    "target": "anyaicam-appliance",
    "platform": "linux",
    "architecture": "x86_64",
    "channel": "stable",
    "issued_at": "2026-08-14T00:00:00Z",
    "package_size_bytes": 123,
}


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _pem_bytes(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _sign(private_key, manifest_dict: dict) -> bytes:
    return private_key.sign(canonical_manifest_bytes(manifest_dict), padding.PKCS1v15(), hashes.SHA256())


class CanonicalManifestBytesTests(unittest.TestCase):
    def test_key_order_does_not_affect_output(self):
        a = {"z": 1, "a": 2, "m": 3}
        b = {"a": 2, "m": 3, "z": 1}
        self.assertEqual(canonical_manifest_bytes(a), canonical_manifest_bytes(b))

    def test_output_has_no_incidental_whitespace(self):
        result = canonical_manifest_bytes({"a": 1, "b": 2})
        self.assertEqual(result, b'{"a":1,"b":2}')

    def test_matches_manual_json_dumps_with_same_options(self):
        expected = json.dumps(VALID_MANIFEST_DICT, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(canonical_manifest_bytes(VALID_MANIFEST_DICT), expected)


class LoadTrustedPublicKeyTests(unittest.TestCase):
    def setUp(self):
        self.private_key, self.public_key = _generate_keypair()

    def test_loads_a_valid_rsa_public_key(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "trusted.pem"
            key_path.write_bytes(_pem_bytes(self.public_key))
            loaded = load_trusted_public_key(key_path)
            self.assertEqual(
                loaded.public_numbers().n,
                self.public_key.public_numbers().n,
            )

    def test_missing_file_raises_trusted_key_unavailable(self):
        with self.assertRaises(TrustedKeyUnavailable):
            load_trusted_public_key("/nonexistent/path/does-not-exist.pem")

    def test_corrupt_pem_raises_trusted_key_unavailable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "trusted.pem"
            key_path.write_bytes(b"-----BEGIN PUBLIC KEY-----\nnot valid base64 content\n-----END PUBLIC KEY-----\n")
            with self.assertRaises(TrustedKeyUnavailable):
                load_trusted_public_key(key_path)

    def test_empty_file_raises_trusted_key_unavailable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "trusted.pem"
            key_path.write_bytes(b"")
            with self.assertRaises(TrustedKeyUnavailable):
                load_trusted_public_key(key_path)

    def test_non_rsa_key_raises_trusted_key_unavailable(self):
        import tempfile
        from cryptography.hazmat.primitives.asymmetric import ed25519
        with tempfile.TemporaryDirectory() as tmp:
            ed_public = ed25519.Ed25519PrivateKey.generate().public_key()
            key_path = Path(tmp) / "trusted.pem"
            key_path.write_bytes(
                ed_public.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            with self.assertRaises(TrustedKeyUnavailable):
                load_trusted_public_key(key_path)


class VerifyManifestSignatureTests(unittest.TestCase):
    def setUp(self):
        self.private_key, self.public_key = _generate_keypair()
        self.signature = _sign(self.private_key, VALID_MANIFEST_DICT)

    def test_valid_signature_verifies_without_raising(self):
        verify_manifest_signature(VALID_MANIFEST_DICT, self.signature, self.public_key)  # must not raise

    def test_tampered_manifest_content_is_rejected(self):
        tampered = {**VALID_MANIFEST_DICT, "version": "9.9.9"}
        with self.assertRaises(ManifestSignatureInvalid):
            verify_manifest_signature(tampered, self.signature, self.public_key)

    def test_signature_from_a_different_key_is_rejected(self):
        _, other_public_key = _generate_keypair()
        with self.assertRaises(ManifestSignatureInvalid):
            verify_manifest_signature(VALID_MANIFEST_DICT, self.signature, other_public_key)

    def test_garbage_signature_bytes_are_rejected(self):
        with self.assertRaises(ManifestSignatureInvalid):
            verify_manifest_signature(VALID_MANIFEST_DICT, b"not-a-real-signature", self.public_key)


class Sha256OfFileTests(unittest.TestCase):
    def test_computes_expected_digest(self):
        import hashlib
        import tempfile
        content = b"anyaicam update package contents" * 1000
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.tar"
            path.write_bytes(content)
            self.assertEqual(sha256_of_file(path), hashlib.sha256(content).hexdigest())

    def test_empty_file_matches_hashlib_empty_digest(self):
        import hashlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.bin"
            path.write_bytes(b"")
            self.assertEqual(sha256_of_file(path), hashlib.sha256(b"").hexdigest())


class VerifyPackageChecksumTests(unittest.TestCase):
    def test_matching_checksum_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.tar"
            path.write_bytes(b"package-bytes")
            digest = sha256_of_file(path)
            verify_package_checksum(path, digest)  # must not raise

    def test_mismatched_checksum_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.tar"
            path.write_bytes(b"package-bytes")
            with self.assertRaises(PackageChecksumMismatch):
                verify_package_checksum(path, "0" * 64)

    def test_comparison_is_case_insensitive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.tar"
            path.write_bytes(b"package-bytes")
            digest = sha256_of_file(path)
            verify_package_checksum(path, digest.upper())  # must not raise


class PackageVerifierTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)

        self.private_key, self.public_key = _generate_keypair()
        self.trusted_key_path = tmp_path / "trusted.pem"
        self.trusted_key_path.write_bytes(_pem_bytes(self.public_key))

        self.package_path = tmp_path / "package.tar"
        self.package_path.write_bytes(b"real update package bytes")
        self.package_sha256 = sha256_of_file(self.package_path)

        self.manifest_dict = {**VALID_MANIFEST_DICT, "sha256": self.package_sha256}
        self.signature = _sign(self.private_key, self.manifest_dict)
        self.verifier = PackageVerifier(self.trusted_key_path)

    def test_verify_manifest_returns_a_trusted_manifest(self):
        manifest = self.verifier.verify_manifest(self.manifest_dict, self.signature)
        self.assertIsInstance(manifest, Manifest)
        self.assertEqual(manifest.update_id, "upd-1")

    def test_verify_manifest_fails_closed_when_trusted_key_file_is_absent(self):
        verifier = PackageVerifier(Path(self._tmp.name) / "does-not-exist.pem")
        with self.assertRaises(TrustedKeyUnavailable):
            verifier.verify_manifest(self.manifest_dict, self.signature)

    def test_target_matches_true_and_false(self):
        manifest = self.verifier.verify_manifest(self.manifest_dict, self.signature)
        self.assertTrue(self.verifier.target_matches(manifest, "anyaicam-appliance"))
        self.assertFalse(self.verifier.target_matches(manifest, "some-other-device"))

    def test_verify_package_succeeds_for_matching_bytes(self):
        manifest = self.verifier.verify_manifest(self.manifest_dict, self.signature)
        self.verifier.verify_package(manifest, self.package_path)  # must not raise

    def test_verify_package_rejects_substituted_bytes(self):
        manifest = self.verifier.verify_manifest(self.manifest_dict, self.signature)
        self.package_path.write_bytes(b"substituted, different bytes")
        with self.assertRaises(PackageChecksumMismatch):
            self.verifier.verify_package(manifest, self.package_path)

    def test_full_pipeline_succeeds_end_to_end(self):
        manifest = self.verifier.verify(
            self.manifest_dict, self.signature, self.package_path, expected_target="anyaicam-appliance"
        )
        self.assertEqual(manifest.sha256, self.package_sha256)

    def test_full_pipeline_rejects_correctly_signed_wrong_target_package(self):
        with self.assertRaises(PackageTargetMismatch):
            self.verifier.verify(
                self.manifest_dict, self.signature, self.package_path, expected_target="some-other-device"
            )

    def test_full_pipeline_rejects_checksum_mismatch_after_valid_signature_and_target(self):
        self.package_path.write_bytes(b"corrupted-in-transit")
        with self.assertRaises(PackageChecksumMismatch):
            self.verifier.verify(
                self.manifest_dict, self.signature, self.package_path, expected_target="anyaicam-appliance"
            )

    def test_full_pipeline_rejects_tampered_manifest_before_touching_package(self):
        tampered = {**self.manifest_dict, "version": "9.9.9"}
        with self.assertRaises(ManifestSignatureInvalid):
            self.verifier.verify(
                tampered, self.signature, self.package_path, expected_target="anyaicam-appliance"
            )

    def test_full_pipeline_fails_closed_with_no_trusted_key_even_with_valid_signature(self):
        verifier = PackageVerifier(Path(self._tmp.name) / "missing-trusted-key.pem")
        with self.assertRaises(TrustedKeyUnavailable):
            verifier.verify(
                self.manifest_dict, self.signature, self.package_path, expected_target="anyaicam-appliance"
            )


if __name__ == "__main__":
    unittest.main()
