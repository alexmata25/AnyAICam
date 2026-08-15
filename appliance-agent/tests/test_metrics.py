"""RDM-2 (device-side integration, Group 2A): focused tests for
anyaicam_agent.metrics.collect()'s current-version reconciliation --
software_version must reflect the real installed version once one has
ever been activated, not just the AgentConfig baseline forever.

No network, no AWS. Real /proc reads are exercised as-is (unmocked),
matching this module's existing, already-established style -- nothing
about /proc/stat, /proc/meminfo, or /proc/uptime is new to this file.
"""

import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.metrics import collect
from anyaicam_agent.updater import installer


class MetricsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = AgentConfig(
            state_dir=self._tmp.name, config_dir=self._tmp.name, log_dir=self._tmp.name,
            software_version="0.1.0",
        )

    def _activate_version(self, version: str) -> None:
        tar_path = Path(self._tmp.name) / f"{version}.tar"
        with tarfile.open(tar_path, mode="w") as tar:
            info = tarfile.TarInfo(name="VERSION")
            data = version.encode()
            info.size = len(data)
            import io
            tar.addfile(info, io.BytesIO(data))
        installer.install_candidate(
            tar_path, version, self.config.update_versions_dir, self.config.update_staging_dir,
        )
        installer.activate(version, self.config.update_versions_dir, self.config.current_version_pointer_file)


class SoftwareVersionReconciliationTests(MetricsTestCase):
    def test_reports_config_baseline_when_no_version_has_ever_been_activated(self):
        result = collect(self.config, [])
        self.assertEqual(result["software_version"], "0.1.0")

    def test_reports_the_real_installed_version_once_one_has_been_activated(self):
        self._activate_version("1.2.3")
        result = collect(self.config, [])
        self.assertEqual(result["software_version"], "1.2.3")

    def test_reports_the_latest_activated_version_after_an_upgrade(self):
        self._activate_version("1.0.0")
        self._activate_version("1.1.0")
        result = collect(self.config, [])
        self.assertEqual(result["software_version"], "1.1.0")

    def test_reports_the_rolled_back_version_after_a_rollback(self):
        self._activate_version("1.0.0")
        self._activate_version("1.1.0")
        installer.activate("1.0.0", self.config.update_versions_dir, self.config.current_version_pointer_file)
        result = collect(self.config, [])
        self.assertEqual(result["software_version"], "1.0.0")


class CollectShapeIsUnaffectedTests(MetricsTestCase):
    def test_all_existing_keys_are_still_present(self):
        result = collect(self.config, [])
        for key in (
            "software_version", "uptime_seconds", "cpu", "memory", "disk_capacity",
            "disk_used", "recording_used", "ip_address", "camera_capacity", "camera_count",
            "cameras", "last_error",
        ):
            self.assertIn(key, result)

    def test_camera_count_and_capacity_unaffected(self):
        result = collect(self.config, [{"id": "cam-1"}, {"id": "cam-2"}])
        self.assertEqual(result["camera_count"], 2)
        self.assertEqual(result["camera_capacity"], self.config.camera_capacity)


if __name__ == "__main__":
    unittest.main()
