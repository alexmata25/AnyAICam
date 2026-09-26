"""RDM-1: focused tests for anyaicam_agent.updater.models -- pure data
model tests, no I/O, no dependency on anything else in the updater
package."""

import sys
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.updater.models import (
    Manifest,
    PendingValidation,
    POST_ACTIVATION_STATES,
    TERMINAL_STATES,
    UpdateResult,
    UpdateState,
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


class ManifestTests(unittest.TestCase):
    def test_from_dict_round_trips_through_as_dict(self):
        manifest = Manifest.from_dict(VALID_MANIFEST_DICT)
        self.assertEqual(manifest.as_dict(), VALID_MANIFEST_DICT)

    def test_from_dict_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            Manifest.from_dict("not-a-dict")

    def test_from_dict_rejects_missing_required_field(self):
        for missing in VALID_MANIFEST_DICT:
            with self.subTest(missing=missing):
                incomplete = {key: value for key, value in VALID_MANIFEST_DICT.items() if key != missing}
                with self.assertRaises(ValueError):
                    Manifest.from_dict(incomplete)

    def test_from_dict_rejects_non_integer_package_size(self):
        bad = {**VALID_MANIFEST_DICT, "package_size_bytes": "not-a-number"}
        with self.assertRaises(ValueError):
            Manifest.from_dict(bad)

    def test_manifest_is_frozen(self):
        manifest = Manifest.from_dict(VALID_MANIFEST_DICT)
        with self.assertRaises(Exception):
            manifest.version = "9.9.9"


class PendingValidationTests(unittest.TestCase):
    def test_round_trip(self):
        marker = PendingValidation(update_id="upd-1", from_version="1.1.0", to_version="1.2.0", attempt="install", deadline="2026-08-14T00:02:00Z")
        self.assertEqual(PendingValidation.from_dict(marker.as_dict()), marker)

    def test_from_dict_rejects_missing_field(self):
        with self.assertRaises(ValueError):
            PendingValidation.from_dict({"update_id": "upd-1", "from_version": "1.1.0"})

    def test_from_dict_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            PendingValidation.from_dict(["not", "a", "dict"])


class UpdateResultTests(unittest.TestCase):
    def test_as_dict_uses_state_value_not_enum_member(self):
        result = UpdateResult(update_id="upd-1", from_version="1.1.0", to_version="1.2.0", state=UpdateState.HEALTHY)
        self.assertEqual(result.as_dict()["state"], "healthy")

    def test_defaults(self):
        result = UpdateResult(update_id="upd-1", from_version="1.1.0", to_version="1.2.0", state=UpdateState.REJECTED)
        self.assertEqual(result.error, "")
        self.assertIsNone(result.rollback_from)
        self.assertEqual(result.duration_seconds, 0.0)


class StateSetTests(unittest.TestCase):
    def test_terminal_states_are_disjoint_from_in_progress_states(self):
        in_progress = {
            UpdateState.VALIDATING_MANIFEST, UpdateState.DOWNLOADING, UpdateState.VERIFYING,
            UpdateState.INSTALLING, UpdateState.ACTIVATING, UpdateState.RESTARTING,
            UpdateState.HEALTH_CHECKING, UpdateState.ROLLING_BACK,
        }
        self.assertEqual(TERMINAL_STATES & in_progress, set())

    def test_post_activation_states_exclude_every_pre_activation_state(self):
        pre_activation = {
            UpdateState.VALIDATING_MANIFEST, UpdateState.REJECTED, UpdateState.DOWNLOADING,
            UpdateState.DOWNLOADED, UpdateState.DOWNLOAD_FAILED, UpdateState.VERIFYING,
            UpdateState.VERIFIED, UpdateState.VERIFY_FAILED, UpdateState.INSTALLING,
            UpdateState.INSTALLED, UpdateState.INSTALL_FAILED, UpdateState.ACTIVATING,
            UpdateState.ACTIVATION_FAILED,
        }
        self.assertEqual(POST_ACTIVATION_STATES & pre_activation, set())

    def test_all_enum_members_are_str_values(self):
        for state in UpdateState:
            self.assertIsInstance(state.value, str)


if __name__ == "__main__":
    unittest.main()
