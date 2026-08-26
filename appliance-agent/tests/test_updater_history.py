"""RDM-1: focused tests for anyaicam_agent.updater.history -- the durable
local update history/idempotency store. All I/O is against SQLite files
in a per-test temporary directory; no network, no AWS, no real device
files."""

import sys
import tempfile
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.updater.history import (
    ABANDONED,
    AlreadyTerminal,
    UnknownUpdateId,
    UpdateHistory,
)
from anyaicam_agent.updater.models import UpdateState


class HistoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "updates" / "update_history.db"
        self.history = UpdateHistory(self.db_path)


class SchemaAndConstructionTests(HistoryTestCase):
    def test_constructor_creates_parent_directory(self):
        self.assertTrue(self.db_path.parent.is_dir())

    def test_reopening_the_same_file_does_not_raise(self):
        UpdateHistory(self.db_path)  # CREATE TABLE IF NOT EXISTS must be idempotent

    def test_reopened_instance_sees_prior_data(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        reopened = UpdateHistory(self.db_path)
        self.assertEqual(reopened.get("upd-1")["to_version"], "1.1.0")


class BeginAttemptTests(HistoryTestCase):
    def test_new_update_id_returns_attempt_one(self):
        self.assertEqual(self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0), 1)

    def test_new_update_id_is_recorded_as_validating_manifest(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        row = self.history.get("upd-1")
        self.assertEqual(row["state"], UpdateState.VALIDATING_MANIFEST.value)
        self.assertEqual(row["attempt_count"], 1)
        self.assertEqual(row["created_at"], 1000.0)
        self.assertEqual(row["updated_at"], 1000.0)

    def test_resuming_a_non_terminal_update_id_increments_attempt_count(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.DOWNLOADING, now=1001.0)
        second = self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1002.0)
        self.assertEqual(second, 2)
        row = self.history.get("upd-1")
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual(row["state"], UpdateState.VALIDATING_MANIFEST.value)
        self.assertEqual(row["updated_at"], 1002.0)

    def test_resuming_with_a_different_from_or_to_version_raises_value_error(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        with self.assertRaises(ValueError):
            self.history.begin_attempt("upd-1", "1.0.0", "9.9.9", now=1001.0)
        with self.assertRaises(ValueError):
            self.history.begin_attempt("upd-1", "0.0.1", "1.1.0", now=1001.0)

    def test_begin_attempt_on_a_terminal_update_id_raises_already_terminal(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.HEALTHY, now=1001.0)
        with self.assertRaises(AlreadyTerminal):
            self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1002.0)

    def test_begin_attempt_on_an_abandoned_update_id_raises_already_terminal(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.mark_abandoned("upd-1", "stale after crash", now=1001.0)
        with self.assertRaises(AlreadyTerminal):
            self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1002.0)

    def test_two_different_update_ids_are_independent(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.assertEqual(self.history.begin_attempt("upd-2", "1.1.0", "1.2.0", now=1000.0), 1)


class RecordTransitionTests(HistoryTestCase):
    def test_unknown_update_id_raises(self):
        with self.assertRaises(UnknownUpdateId):
            self.history.record_transition("never-began", UpdateState.DOWNLOADING)

    def test_updates_the_summary_row_state_and_error(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.VERIFY_FAILED, "bad signature", now=1005.0)
        row = self.history.get("upd-1")
        self.assertEqual(row["state"], UpdateState.VERIFY_FAILED.value)
        self.assertEqual(row["error"], "bad signature")
        self.assertEqual(row["updated_at"], 1005.0)
        self.assertEqual(row["created_at"], 1000.0)  # created_at never changes

    def test_accepts_a_plain_string_state(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", "custom_event", "note", now=1001.0)
        self.assertEqual(self.history.get("upd-1")["state"], "custom_event")

    def test_transitions_are_appended_in_order(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.DOWNLOADING, now=1001.0)
        self.history.record_transition("upd-1", UpdateState.DOWNLOADED, now=1002.0)
        self.history.record_transition("upd-1", UpdateState.VERIFYING, now=1003.0)
        states = [row["state"] for row in self.history.transitions("upd-1")]
        self.assertEqual(
            states,
            [
                UpdateState.VALIDATING_MANIFEST.value,  # from begin_attempt()
                UpdateState.DOWNLOADING.value,
                UpdateState.DOWNLOADED.value,
                UpdateState.VERIFYING.value,
            ],
        )

    def test_transitions_for_unknown_update_id_is_empty_list(self):
        self.assertEqual(self.history.transitions("never-began"), [])


class MarkAbandonedTests(HistoryTestCase):
    def test_marks_state_as_abandoned(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.DOWNLOADING, now=1001.0)
        self.history.mark_abandoned("upd-1", "no pending_validation marker found at restart", now=1010.0)
        row = self.history.get("upd-1")
        self.assertEqual(row["state"], ABANDONED)
        self.assertEqual(row["error"], "no pending_validation marker found at restart")

    def test_abandoned_update_id_is_terminal(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.mark_abandoned("upd-1", now=1001.0)
        self.assertTrue(self.history.is_terminal("upd-1"))

    def test_abandoned_update_id_appears_in_transitions(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.mark_abandoned("upd-1", "orphaned", now=1001.0)
        states = [row["state"] for row in self.history.transitions("upd-1")]
        self.assertIn(ABANDONED, states)


class IsTerminalTests(HistoryTestCase):
    def test_unknown_update_id_is_not_terminal(self):
        self.assertFalse(self.history.is_terminal("never-began"))

    def test_in_progress_update_id_is_not_terminal(self):
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.DOWNLOADING, now=1001.0)
        self.assertFalse(self.history.is_terminal("upd-1"))

    def test_every_terminal_state_is_reported_as_terminal(self):
        from anyaicam_agent.updater.models import TERMINAL_STATES
        for index, state in enumerate(TERMINAL_STATES):
            update_id = f"upd-terminal-{index}"
            with self.subTest(state=state):
                self.history.begin_attempt(update_id, "1.0.0", "1.1.0", now=1000.0)
                self.history.record_transition(update_id, state, now=1001.0)
                self.assertTrue(self.history.is_terminal(update_id))

    def test_non_terminal_states_are_not_reported_as_terminal(self):
        non_terminal = [
            UpdateState.VALIDATING_MANIFEST, UpdateState.DOWNLOADING, UpdateState.DOWNLOADED,
            UpdateState.VERIFYING, UpdateState.VERIFIED, UpdateState.INSTALLING, UpdateState.INSTALLED,
            UpdateState.ACTIVATING, UpdateState.ACTIVATED, UpdateState.RESTARTING,
            UpdateState.HEALTH_CHECKING, UpdateState.UNHEALTHY, UpdateState.RESTART_FAILED,
            UpdateState.ROLLING_BACK,
        ]
        for index, state in enumerate(non_terminal):
            update_id = f"upd-nonterminal-{index}"
            with self.subTest(state=state):
                self.history.begin_attempt(update_id, "1.0.0", "1.1.0", now=1000.0)
                self.history.record_transition(update_id, state, now=1001.0)
                self.assertFalse(self.history.is_terminal(update_id))


class InProgressUpdateIdsTests(HistoryTestCase):
    def test_empty_store_returns_empty_list(self):
        self.assertEqual(self.history.in_progress_update_ids(), [])

    def test_excludes_terminal_and_abandoned_includes_in_progress(self):
        self.history.begin_attempt("upd-healthy", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-healthy", UpdateState.HEALTHY, now=1001.0)

        self.history.begin_attempt("upd-abandoned", "1.0.0", "1.1.0", now=1000.0)
        self.history.mark_abandoned("upd-abandoned", now=1001.0)

        self.history.begin_attempt("upd-downloading", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-downloading", UpdateState.DOWNLOADING, now=1001.0)

        self.assertEqual(self.history.in_progress_update_ids(), ["upd-downloading"])

    def test_orphaned_staging_scenario_end_to_end(self):
        # Simulates the crash-recovery workflow this method exists for:
        # an update was left mid-install with no completion recorded.
        self.history.begin_attempt("upd-crashed", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-crashed", UpdateState.INSTALLING, now=1001.0)
        self.assertEqual(self.history.in_progress_update_ids(), ["upd-crashed"])

        # Startup recovery decides not to resume it and marks it abandoned.
        self.history.mark_abandoned("upd-crashed", "staging directory missing at restart", now=2000.0)
        self.assertEqual(self.history.in_progress_update_ids(), [])
        self.assertTrue(self.history.is_terminal("upd-crashed"))

    def test_ordered_oldest_first(self):
        self.history.begin_attempt("upd-b", "1.0.0", "1.1.0", now=2000.0)
        self.history.begin_attempt("upd-a", "1.0.0", "1.1.0", now=1000.0)
        self.assertEqual(self.history.in_progress_update_ids(), ["upd-a", "upd-b"])


class GetTests(HistoryTestCase):
    def test_unknown_update_id_returns_none(self):
        self.assertIsNone(self.history.get("never-began"))


class ReplayProtectionEndToEndTests(HistoryTestCase):
    def test_duplicate_install_command_after_success_is_rejected(self):
        # Simulates the cloud/portal re-delivering the same install_update
        # command a second time after it already succeeded.
        self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1000.0)
        self.history.record_transition("upd-1", UpdateState.DOWNLOADING, now=1001.0)
        self.history.record_transition("upd-1", UpdateState.VERIFYING, now=1002.0)
        self.history.record_transition("upd-1", UpdateState.INSTALLING, now=1003.0)
        self.history.record_transition("upd-1", UpdateState.ACTIVATED, now=1004.0)
        self.history.record_transition("upd-1", UpdateState.HEALTHY, now=1005.0)

        self.assertTrue(self.history.is_terminal("upd-1"))
        with self.assertRaises(AlreadyTerminal):
            self.history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=1006.0)

        # The full audit trail survived the (rejected) replay attempt.
        states = [row["state"] for row in self.history.transitions("upd-1")]
        self.assertEqual(len(states), 6)
        self.assertEqual(states[-1], UpdateState.HEALTHY.value)


if __name__ == "__main__":
    unittest.main()
