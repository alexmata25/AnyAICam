"""Windows 1.3.2 (Blue): the heartbeat tells the cloud this is a Windows PC (Software Update then never offers it the
Linux release); the Linux appliance's heartbeat is unchanged (no "platform" key)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent import metrics, windows
from anyaicam_agent.config import AgentConfig


class PlatformReportTests(unittest.TestCase):
    def collect(self, is_windows):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(config_dir=tmp, state_dir=tmp, log_dir=tmp, vms_recordings_path=tmp)
            with patch.object(windows, 'IS_WINDOWS', is_windows), \
                 patch.object(windows, 'uptime_seconds', return_value=1), patch.object(windows, 'cpu_percent', return_value=1.0), \
                 patch.object(windows, 'memory_percent', return_value=1.0), patch.object(metrics, 'local_ip', return_value='192.168.1.20'), \
                 patch.object(metrics, 'disk_summary', return_value={}), patch.object(metrics, 'local_storage_state', return_value={}), \
                 patch.object(metrics, '_cpu_percent', return_value=1.0), patch.object(metrics, '_memory_percent', return_value=1.0):
                return metrics.collect(config, [])

    def test_windows_reports_its_platform(self):
        self.assertEqual(self.collect(True)['platform'], 'windows')

    def test_linux_heartbeat_has_no_platform_key(self):
        self.assertNotIn('platform', self.collect(False))


if __name__ == '__main__':
    unittest.main()
