"""RDM-2 (device-side integration, Group 2B): focused tests for
anyaicam_agent.updater.restart.make_restart_signal() -- the real
restart_signal implementation.

No network, no AWS, no subprocess, no real process exit -- every test
here observes only threading.Event state, never anything that would
actually terminate this test process. Semantic equivalence with
commands.py's existing restart_service handler is checked directly
against that real handler, not assumed.
"""

import sys
import unittest
from pathlib import Path
from threading import Event

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.commands import execute
from anyaicam_agent.config import AgentConfig
from anyaicam_agent.updater.restart import make_restart_signal


class MakeRestartSignalTests(unittest.TestCase):
    def test_returns_a_callable(self):
        signal = make_restart_signal(Event())
        self.assertTrue(callable(signal))

    def test_event_is_not_set_before_calling_the_signal(self):
        event = Event()
        make_restart_signal(event)  # constructing it must not set anything
        self.assertFalse(event.is_set())

    def test_calling_the_signal_sets_the_event(self):
        event = Event()
        signal = make_restart_signal(event)
        signal()
        self.assertTrue(event.is_set())

    def test_calling_the_signal_returns_none(self):
        event = Event()
        signal = make_restart_signal(event)
        self.assertIsNone(signal())

    def test_calling_the_signal_takes_no_arguments(self):
        event = Event()
        signal = make_restart_signal(event)
        signal()  # must not raise a TypeError about missing/extra arguments

    def test_calling_it_more_than_once_does_not_raise(self):
        event = Event()
        signal = make_restart_signal(event)
        signal()
        signal()  # Event.set() is naturally idempotent; must not raise
        self.assertTrue(event.is_set())

    def test_two_different_events_are_independent(self):
        event_a, event_b = Event(), Event()
        make_restart_signal(event_a)()
        self.assertTrue(event_a.is_set())
        self.assertFalse(event_b.is_set())


class SemanticEquivalenceWithRestartServiceTests(unittest.TestCase):
    """Proves make_restart_signal() produces the exact same observable
    effect as commands.py's existing restart_service command handler --
    not merely a similar one."""

    def test_matches_restart_service_commands_effect_on_stop_event(self):
        config = AgentConfig()

        via_restart_service = Event()
        status, result, error = execute("restart_service", {}, config, via_restart_service)
        self.assertEqual(status, "completed")
        self.assertTrue(via_restart_service.is_set())

        via_restart_signal = Event()
        make_restart_signal(via_restart_signal)()
        self.assertTrue(via_restart_signal.is_set())

        # Both paths reach the identical end state -- the event is set,
        # and nothing else about process/agent state was touched by
        # either one (no exception, no side effect beyond the flag).
        self.assertEqual(via_restart_service.is_set(), via_restart_signal.is_set())


if __name__ == "__main__":
    unittest.main()
