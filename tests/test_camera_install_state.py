"""camera_install_state.py: the customer-facing "installed/configured"
decision, now driven by real appliance-reported state as well as the
original customer-wizard-only cameras.status='configured' signal.

Pure logic tests, same established pattern as test_camera_access.py and
test_admin_partner_bridge.py.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from camera_install_state import camera_is_installed, camera_status_label  # noqa: E402


class CameraIsInstalledTests(unittest.TestCase):
    def test_customer_wizard_configured_status_is_installed(self):
        # Backward compatibility: a customer who completed
        # /customer/setup's own "Save camera setup" step keeps working
        # exactly as before, even with no appliance report at all.
        self.assertTrue(camera_is_installed(
            camera_status="configured", appliance_reported_online=False,
            appliance_reported_recording=False, has_cloud_recording=False,
        ))

    def test_installer_provisioned_camera_reporting_online_is_installed_even_without_wizard(self):
        # The exact bug this milestone fixes: an installer-provisioned
        # camera whose status column was never touched by the customer's
        # own wizard, but whose appliance heartbeat reports it online.
        self.assertTrue(camera_is_installed(
            camera_status=None, appliance_reported_online=True,
            appliance_reported_recording=False, has_cloud_recording=False,
        ))

    def test_installer_provisioned_camera_reporting_recording_is_installed(self):
        self.assertTrue(camera_is_installed(
            camera_status="pending", appliance_reported_online=False,
            appliance_reported_recording=True, has_cloud_recording=False,
        ))

    def test_camera_with_a_cloud_recordings_row_is_installed(self):
        self.assertTrue(camera_is_installed(
            camera_status=None, appliance_reported_online=False,
            appliance_reported_recording=False, has_cloud_recording=True,
        ))

    def test_camera_with_no_signal_at_all_is_not_installed(self):
        # The one case "Pending installation" must still legitimately
        # mean: truly never provisioned, never reported in, no footage.
        self.assertFalse(camera_is_installed(
            camera_status=None, appliance_reported_online=False,
            appliance_reported_recording=False, has_cloud_recording=False,
        ))

    def test_camera_with_a_non_configured_status_and_no_report_is_not_installed(self):
        self.assertFalse(camera_is_installed(
            camera_status="discovered", appliance_reported_online=False,
            appliance_reported_recording=False, has_cloud_recording=False,
        ))


class CameraStatusLabelTests(unittest.TestCase):
    def test_online_and_recording_shows_combined_label(self):
        self.assertEqual(
            camera_status_label(camera_status=None, appliance_reported_online=True,
                                 appliance_reported_recording=True, has_cloud_recording=False),
            "Online · Recording",
        )

    def test_online_only(self):
        self.assertEqual(
            camera_status_label(camera_status=None, appliance_reported_online=True,
                                 appliance_reported_recording=False, has_cloud_recording=False),
            "Online",
        )

    def test_recording_only(self):
        self.assertEqual(
            camera_status_label(camera_status=None, appliance_reported_online=False,
                                 appliance_reported_recording=True, has_cloud_recording=False),
            "Recording",
        )

    def test_configured_status_with_no_live_report_shows_configured(self):
        self.assertEqual(
            camera_status_label(camera_status="configured", appliance_reported_online=False,
                                 appliance_reported_recording=False, has_cloud_recording=False),
            "Configured",
        )

    def test_cloud_recording_only_shows_recorded_footage_available(self):
        self.assertEqual(
            camera_status_label(camera_status=None, appliance_reported_online=False,
                                 appliance_reported_recording=False, has_cloud_recording=True),
            "Recorded footage available",
        )

    def test_no_signal_at_all_shows_pending_installation(self):
        self.assertEqual(
            camera_status_label(camera_status=None, appliance_reported_online=False,
                                 appliance_reported_recording=False, has_cloud_recording=False),
            "Pending installation",
        )

    def test_live_appliance_report_beats_a_stale_configured_status(self):
        # A camera that completed the wizard long ago but is currently
        # online should show the more current, more specific label.
        self.assertEqual(
            camera_status_label(camera_status="configured", appliance_reported_online=True,
                                 appliance_reported_recording=True, has_cloud_recording=False),
            "Online · Recording",
        )

    def test_label_and_is_installed_never_disagree(self):
        # A camera counted as installed must never display "Pending
        # installation", and vice versa -- exhaustively check every
        # boolean combination.
        for camera_status in (None, "", "pending", "discovered", "configured"):
            for online in (True, False):
                for recording in (True, False):
                    for has_recording in (True, False):
                        installed = camera_is_installed(
                            camera_status=camera_status, appliance_reported_online=online,
                            appliance_reported_recording=recording, has_cloud_recording=has_recording,
                        )
                        label = camera_status_label(
                            camera_status=camera_status, appliance_reported_online=online,
                            appliance_reported_recording=recording, has_cloud_recording=has_recording,
                        )
                        self.assertEqual(installed, label != "Pending installation",
                                          (camera_status, online, recording, has_recording, label))


if __name__ == "__main__":
    unittest.main()
