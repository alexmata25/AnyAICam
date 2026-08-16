"""RDM-2 (device-side integration, Group 2G): focused tests for
anyaicam_agent.updater.s3_source.ManifestSource -- the real
UpdateSourceProvider implementation.

No real network, no real AWS, no real S3 -- the PortalClient is always a
small fake here (this file is about ManifestSource's own logic, not
PortalClient's, which is already covered elsewhere), and package
downloads use a fake HTTP response fixture, never a live socket.
"""

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.portal import PortalError
from anyaicam_agent.updater.factory import _UnconfiguredSource, build_update_state_machine
from anyaicam_agent.updater.source import PackageDownloadError, PackageNotFound, SourceUnavailable
from anyaicam_agent.updater import s3_source
from anyaicam_agent.updater.s3_source import ManifestSource, make_manifest_source
from anyaicam_agent.config import AgentConfig

import urllib.error


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if self.error is not None:
            raise self.error
        return self.response


GOOD_MANIFEST = {
    "update_id": "upd-1", "version": "1.1.0", "sha256": "a" * 64,
    "target": "anyaicam-appliance", "platform": "linux", "architecture": "x86_64",
    "channel": "stable", "issued_at": "2026-08-20T00:00:00Z", "package_size_bytes": 100,
}
GOOD_SIGNATURE_B64 = base64.b64encode(b"fake-signature-bytes").decode("ascii")


class FactoryDefaultRegressionTests(unittest.TestCase):
    """Regression guard: factory.py's own _UnconfiguredSource default
    (used when a caller doesn't override `source=`) is UNCHANGED by
    this group -- only service.py now always passes a real one."""

    def test_factory_default_source_is_still_unconfigured_when_not_overridden(self):
        with tempfile.TemporaryDirectory() as folder:
            config = AgentConfig(state_dir=folder, config_dir=folder, log_dir=folder)
            machine = build_update_state_machine(config)
            self.assertIsInstance(machine.source, _UnconfiguredSource)


class CheckForManifestTests(unittest.TestCase):
    def test_target_and_channel_are_validated_before_any_request(self):
        client = FakeClient()
        source = ManifestSource(client)
        for bad in ("../escape", "has/slash", "", "has space", "..", "."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    source.check_for_manifest("1.0.0", bad, "stable")
                self.assertEqual(client.calls, [])  # never even attempted the request

    def test_no_update_available_returns_none(self):
        client = FakeClient(response={"status": "no_update_available"})
        source = ManifestSource(client)
        self.assertIsNone(source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable"))

    def test_valid_manifest_envelope_is_parsed_correctly(self):
        client = FakeClient(response={
            "manifest": GOOD_MANIFEST, "signature": GOOD_SIGNATURE_B64,
            "package_url": "https://fake-s3.example/packages/anyaicam-appliance/stable/1.1.0.tar",
        })
        source = ManifestSource(client)
        manifest_dict, signature = source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")
        self.assertEqual(manifest_dict, GOOD_MANIFEST)
        self.assertEqual(signature, b"fake-signature-bytes")
        self.assertEqual(client.calls, [("GET", "/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable")])

    def test_source_unavailable_on_portal_error(self):
        client = FakeClient(error=PortalError("cloud unreachable"))
        source = ManifestSource(client)
        with self.assertRaises(SourceUnavailable):
            source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")

    def test_source_unavailable_when_feature_disabled_404(self):
        client = FakeClient(error=PortalError("not enabled", status_code=404))
        source = ManifestSource(client)
        with self.assertRaises(SourceUnavailable):
            source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")

    def test_malformed_response_missing_manifest_is_source_unavailable(self):
        client = FakeClient(response={"signature": GOOD_SIGNATURE_B64, "package_url": "https://x"})
        source = ManifestSource(client)
        with self.assertRaises(SourceUnavailable):
            source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")

    def test_malformed_base64_signature_is_source_unavailable(self):
        client = FakeClient(response={
            "manifest": GOOD_MANIFEST, "signature": "not-valid-base64!!!",
            "package_url": "https://x",
        })
        source = ManifestSource(client)
        with self.assertRaises(SourceUnavailable):
            source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")


class _FakeHTTPResponse:
    """Minimal fake for urllib.request.urlopen()'s return value --
    supports the context-manager protocol and chunked .read(size), the
    only two things download_package() actually uses."""

    def __init__(self, chunks, raise_after=None):
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self._reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, size=-1):
        self._reads += 1
        if self._raise_after is not None and self._reads > self._raise_after:
            raise OSError("simulated connection drop mid-transfer")
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class DownloadPackageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.destination = Path(self._tmp.name) / "package.tar"
        self.source = ManifestSource(FakeClient())
        self.source._last_package_url = "https://fake-s3.example/packages/x/y/1.0.0.tar"

    def test_successful_download_writes_the_full_package(self):
        with patch.object(s3_source.urllib.request, "urlopen", return_value=_FakeHTTPResponse([b"hello ", b"world"])):
            self.source.download_package({"update_id": "upd-1"}, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"hello world")

    def test_no_package_url_raises_download_error(self):
        source = ManifestSource(FakeClient())  # _last_package_url never set
        with self.assertRaises(PackageDownloadError):
            source.download_package({"update_id": "upd-1"}, self.destination)
        self.assertFalse(self.destination.exists())

    def test_http_404_raises_package_not_found_and_leaves_no_artifact(self):
        error = urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)
        with patch.object(s3_source.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(PackageNotFound):
                self.source.download_package({"update_id": "upd-1"}, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".tar.tmp").exists())

    def test_http_403_raises_package_download_error(self):
        # An expired presigned URL surfaces as a 403 from S3.
        error = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        with patch.object(s3_source.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(PackageDownloadError):
                self.source.download_package({"update_id": "upd-1"}, self.destination)
        self.assertFalse(self.destination.exists())

    def test_partial_download_leaves_no_destination_artifact(self):
        with patch.object(s3_source.urllib.request, "urlopen", return_value=_FakeHTTPResponse([b"partial-data"], raise_after=1)):
            with self.assertRaises(PackageDownloadError):
                self.source.download_package({"update_id": "upd-1"}, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".tar.tmp").exists())  # temp sibling cleaned up too


class MakeManifestSourceTests(unittest.TestCase):
    def test_returns_a_manifest_source_bound_to_the_given_client(self):
        client = FakeClient()
        source = make_manifest_source(client)
        self.assertIsInstance(source, ManifestSource)
        self.assertIs(source._client, client)


if __name__ == "__main__":
    unittest.main()
