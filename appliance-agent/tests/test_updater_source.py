"""RDM-1: focused tests for anyaicam_agent.updater.source -- the
UpdateSourceProvider interface, FakeUpdateSourceProvider test double, and
get_configured_source() fail-closed stub.

No network, no AWS, no real device files -- all downloads target paths
inside a per-test temporary directory.
"""

import sys
import tempfile
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.updater.source import (
    FakeUpdateSourceProvider,
    PackageDownloadError,
    PackageNotFound,
    SourceUnavailable,
    UpdateSourceProvider,
    get_configured_source,
)

MANIFEST = {
    "update_id": "upd-1",
    "version": "1.2.0",
    "sha256": "a" * 64,
    "target": "anyaicam-appliance",
    "platform": "linux",
    "architecture": "x86_64",
    "channel": "stable",
    "issued_at": "2026-08-14T00:00:00Z",
    "package_size_bytes": 26,
}
PACKAGE_BYTES = b"real update package bytes"


class InterfaceTests(unittest.TestCase):
    def test_cannot_instantiate_the_abstract_base_directly(self):
        with self.assertRaises(TypeError):
            UpdateSourceProvider()

    def test_fake_provider_is_an_update_source_provider(self):
        self.assertIsInstance(FakeUpdateSourceProvider(), UpdateSourceProvider)


class GetConfiguredSourceTests(unittest.TestCase):
    def test_returns_none(self):
        self.assertIsNone(get_configured_source())


class CheckForManifestTests(unittest.TestCase):
    def test_no_manifest_configured_means_no_update_available(self):
        source = FakeUpdateSourceProvider()
        self.assertIsNone(source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable"))

    def test_configured_manifest_is_returned_with_its_signature(self):
        source = FakeUpdateSourceProvider(manifest=MANIFEST, signature=b"sig-bytes")
        result = source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")
        self.assertEqual(result, (MANIFEST, b"sig-bytes"))

    def test_fail_check_raises_source_unavailable(self):
        source = FakeUpdateSourceProvider(fail_check=True)
        with self.assertRaises(SourceUnavailable):
            source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")

    def test_calls_are_recorded_in_order(self):
        source = FakeUpdateSourceProvider()
        source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")
        source.check_for_manifest("1.0.1", "anyaicam-appliance", "beta")
        self.assertEqual(
            source.check_calls,
            [("1.0.0", "anyaicam-appliance", "stable"), ("1.0.1", "anyaicam-appliance", "beta")],
        )


class DownloadPackageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.destination = Path(self._tmp.name) / "staging" / "upd-1.pkg"
        self.temp_sibling = self.destination.with_suffix(self.destination.suffix + ".tmp")


class DownloadPackageSuccessTests(DownloadPackageTestCase):
    def test_writes_exactly_the_configured_bytes(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES)
        source.download_package(MANIFEST, self.destination)
        self.assertEqual(self.destination.read_bytes(), PACKAGE_BYTES)

    def test_creates_missing_parent_directories(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES)
        source.download_package(MANIFEST, self.destination)
        self.assertTrue(self.destination.parent.is_dir())

    def test_no_temporary_sibling_survives_a_successful_download(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES)
        source.download_package(MANIFEST, self.destination)
        self.assertFalse(self.temp_sibling.exists())

    def test_calls_are_recorded_with_update_id_and_destination(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES)
        source.download_package(MANIFEST, self.destination)
        self.assertEqual(source.download_calls, [("upd-1", self.destination)])


class DownloadPackageFailureTests(DownloadPackageTestCase):
    def test_fail_download_raises_and_writes_nothing(self):
        source = FakeUpdateSourceProvider(fail_download=True)
        with self.assertRaises(PackageDownloadError):
            source.download_package(MANIFEST, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.temp_sibling.exists())

    def test_missing_package_raises_package_not_found_and_writes_nothing(self):
        source = FakeUpdateSourceProvider(missing_package=True)
        with self.assertRaises(PackageNotFound):
            source.download_package(MANIFEST, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.temp_sibling.exists())

    def test_package_not_found_is_a_package_download_error(self):
        self.assertTrue(issubclass(PackageNotFound, PackageDownloadError))


class DownloadPackageInterruptionTests(DownloadPackageTestCase):
    def test_interrupted_download_raises_package_download_error(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, interrupt_after_bytes=5)
        with self.assertRaises(PackageDownloadError):
            source.download_package(MANIFEST, self.destination)

    def test_interrupted_download_never_creates_destination_path(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, interrupt_after_bytes=5)
        with self.assertRaises(PackageDownloadError):
            source.download_package(MANIFEST, self.destination)
        self.assertFalse(self.destination.exists())

    def test_interrupted_download_leaves_only_the_partial_temporary_sibling(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, interrupt_after_bytes=5)
        with self.assertRaises(PackageDownloadError):
            source.download_package(MANIFEST, self.destination)
        self.assertEqual(self.temp_sibling.read_bytes(), PACKAGE_BYTES[:5])

    def test_interrupt_after_zero_bytes_leaves_an_empty_temporary_sibling(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, interrupt_after_bytes=0)
        with self.assertRaises(PackageDownloadError):
            source.download_package(MANIFEST, self.destination)
        self.assertEqual(self.temp_sibling.read_bytes(), b"")
        self.assertFalse(self.destination.exists())


class DownloadPackageCorruptionTests(DownloadPackageTestCase):
    def test_corrupt_flag_produces_bytes_different_from_the_configured_package(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, corrupt=True)
        source.download_package(MANIFEST, self.destination)
        downloaded = self.destination.read_bytes()
        self.assertNotEqual(downloaded, PACKAGE_BYTES)
        self.assertEqual(len(downloaded), len(PACKAGE_BYTES))  # same length -- one bit-flip, not truncation

    def test_corrupt_download_still_completes_and_renames_into_place(self):
        # Corruption is a content problem the checksum layer (verify.py)
        # catches -- it is not a transfer failure, so the fake still
        # "succeeds" at delivering a (corrupted) complete file.
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, corrupt=True)
        source.download_package(MANIFEST, self.destination)
        self.assertTrue(self.destination.exists())
        self.assertFalse(self.temp_sibling.exists())

    def test_corrupt_with_empty_package_bytes_still_produces_output(self):
        source = FakeUpdateSourceProvider(package_bytes=b"", corrupt=True)
        source.download_package(MANIFEST, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"\x00")

    def test_without_corrupt_flag_bytes_are_unmodified(self):
        source = FakeUpdateSourceProvider(package_bytes=PACKAGE_BYTES, corrupt=False)
        source.download_package(MANIFEST, self.destination)
        self.assertEqual(self.destination.read_bytes(), PACKAGE_BYTES)


class EndToEndWithVerifierTests(DownloadPackageTestCase):
    """Proves FakeUpdateSourceProvider's output is usable by
    updater.verify.PackageVerifier as-is -- checksum failure for a
    corrupted download, success for a clean one -- without this module
    depending on verify.py at runtime (the import here is test-only)."""

    def setUp(self):
        super().setUp()
        from anyaicam_agent.updater.verify import sha256_of_file

        good_path = Path(self._tmp.name) / "good_reference.pkg"
        good_path.write_bytes(PACKAGE_BYTES)
        self.manifest = {**MANIFEST, "sha256": sha256_of_file(good_path)}

    def test_clean_download_passes_checksum_verification(self):
        from anyaicam_agent.updater.verify import verify_package_checksum

        source = FakeUpdateSourceProvider(manifest=self.manifest, package_bytes=PACKAGE_BYTES)
        source.download_package(self.manifest, self.destination)
        verify_package_checksum(self.destination, self.manifest["sha256"])  # must not raise

    def test_corrupted_download_fails_checksum_verification(self):
        from anyaicam_agent.updater.verify import PackageChecksumMismatch, verify_package_checksum

        source = FakeUpdateSourceProvider(manifest=self.manifest, package_bytes=PACKAGE_BYTES, corrupt=True)
        source.download_package(self.manifest, self.destination)
        with self.assertRaises(PackageChecksumMismatch):
            verify_package_checksum(self.destination, self.manifest["sha256"])


if __name__ == "__main__":
    unittest.main()
