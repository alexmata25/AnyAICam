"""The appliance never installs an update on its own (2026-10-02 launch
blocker). ApplianceAgent.check_for_source_update() ran every 900 s and
called UpdateStateMachine.check_and_install(), so a published release was
downloaded, activated and restarted with no customer action. The periodic
path now only checks and remembers what is available; installing is a
separate, owner-confirmed action."""
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

from anyaicam_agent.service import ApplianceAgent


class _Machine:
    def __init__(self, available):
        self.available = available
        self.installs = 0

    def has_unresolved_activation(self):
        return False

    def check_available(self, current_version=None):
        self.checked_with = current_version
        return self.available

    def check_and_install(self, *args, **kwargs):
        self.installs += 1
        raise AssertionError("the periodic poll must never install")

    def process_install_update(self, *args, **kwargs):
        self.installs += 1
        raise AssertionError("the periodic poll must never install")


def _agent(machine):
    return SimpleNamespace(state_machine=machine, update_resume_failed=False, _next_source_check_at=0.0,
                           available_update=None, config=SimpleNamespace(update_check_interval_seconds=900,
                                                                    vms_release_marker_file=Path("no-such-marker.json")),
                           log=logging.getLogger("test"))


class PeriodicPollIsCheckOnlyTests(unittest.TestCase):
    def test_an_available_release_is_remembered_not_installed(self):
        machine = _Machine({"version": "1.2.0", "update_id": "u-1", "current_version": "1.1.0"})
        agent = _agent(machine)
        ApplianceAgent.check_for_source_update(agent)
        self.assertEqual(machine.installs, 0)
        self.assertEqual(agent.available_update["version"], "1.2.0")

    def test_every_later_poll_stays_check_only(self):
        machine = _Machine({"version": "1.2.0"})
        agent = _agent(machine)
        for _ in range(5):
            agent._next_source_check_at = 0.0
            ApplianceAgent.check_for_source_update(agent)
        self.assertEqual(machine.installs, 0)

    def test_nothing_available_clears_the_offer(self):
        agent = _agent(_Machine(None))
        agent.available_update = {"version": "old"}
        ApplianceAgent.check_for_source_update(agent)
        self.assertIsNone(agent.available_update)

    def test_the_poll_source_no_longer_calls_install(self):
        import inspect
        source = chr(10).join(line.split("#", 1)[0] for line in inspect.getsource(ApplianceAgent.check_for_source_update).splitlines())
        self.assertNotIn("check_and_install()", source)
        self.assertNotIn("process_install_update(", source)
