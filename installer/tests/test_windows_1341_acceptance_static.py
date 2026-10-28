"""Source-level acceptance checks that run without installing or starting VMS."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class Windows1341AcceptanceSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functional = (ROOT / "installer/windows/sandbox/functional-in-sandbox.ps1").read_text(encoding="utf-8")
        cls.builder = (ROOT / "installer/windows/build.ps1").read_text(encoding="utf-8")
        cls.wsb = (ROOT / "installer/windows/sandbox/make-wsb.ps1").read_text(encoding="utf-8")
        cls.ppe = (ROOT / "app/tests/test_ppe_lpr_facial_event_media_gap_audit.py").read_text(encoding="utf-8")

    def test_event_only_harness_does_not_request_continuous_recording(self):
        provision = re.search(r"Cloud POST '/api/customer/cameras/provision'.*", self.functional)
        self.assertIsNotNone(provision)
        self.assertNotIn("recording_mode", provision.group(0))
        self.assertIn("local_recording_mode -ne 'event'", self.functional)
        self.assertIn("finished playable event clip", self.functional)
        self.assertIn("no-motion interval", self.functional)
        self.assertIn("completed local event clip remains playable without the cloud", self.functional)

    def test_winsw_stop_scenario_checks_bound_and_playable_event_clip(self):
        self.assertIn("Stop-Service -Name AnyAiCamVMS -Force", self.functional)
        self.assertIn("Wait-Job -Job $stopJob -Timeout 35", self.functional)
        self.assertIn("event-recording-after-winsw-stop.mp4", self.functional)
        self.assertIn("post-stop event clip is not playable", self.functional)

    def test_local_system_tesseract_and_sapi_checks_are_present(self):
        self.assertIn("New-ScheduledTaskPrincipal -UserId 'SYSTEM'", self.functional)
        self.assertIn("--list-langs", self.functional)
        self.assertIn("SAPI.SpVoice", self.functional)
        self.assertIn("non-silent PCM samples", self.functional)

    def test_winsw_job_object_membership_and_cpu_cap_are_checked(self):
        self.assertIn("IsProcessInJob", self.functional)
        self.assertIn("resource limits applied \\(cpu=", self.functional)

    def test_installer_has_750_mb_and_full_catalog_member_gates(self):
        self.assertIn("[long]750000000", self.builder)
        self.assertIn("foreach ($member in $catalogMembers)", self.builder)
        self.assertIn("verify /pa /c", self.builder.lower())

    def test_upgrade_harness_accepts_four_part_test_setup_and_pins_accepted_134(self):
        self.assertIn("(?:\\.\\d+)?", self.wsb)
        self.assertIn("Accepted 1.3.4 baseline SHA-256 mismatch", self.wsb)
        self.assertIn("Get-AnyAiCamPublisherProblem", self.wsb)
        self.assertIn("U134", self.functional)

    def test_both_ppe_fixtures_enable_appliance_wide_switch(self):
        self.assertEqual(2, self.ppe.count('monkeypatch.setattr(main.ppe, "PPE_ENABLED", True)'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
