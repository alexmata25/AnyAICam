"""RDM-2 (device-side integration, Group 2F): focused tests for
anyaicam_agent.updater.health.make_health_check() -- the real
health_check implementation for UpdateStateMachine.

No real network -- PortalClient.request() is always a controllable
fake/MagicMock here. No real process exit, no real service restart. A
FakeClock (matching the same pattern already established in
test_updater_state_machine.py) gives deterministic control over the
retry/timeout budget.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.portal import PortalError
from anyaicam_agent.updater.health import _MAX_ATTEMPTS, _PER_ATTEMPT_TIMEOUT_SECONDS, _TOTAL_BUDGET_SECONDS, make_health_check


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class HealthCheckTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = AgentConfig(state_dir=self._tmp.name, config_dir=self._tmp.name, log_dir=self._tmp.name)
        self.client = MagicMock()
        self.client.timeout = 20  # PortalClient's normal default -- must never govern this probe
        self.clock = FakeClock()


# -- local self-checks ------------------------------------------------------

class LocalChecksTests(HealthCheckTestCase):
    def test_accessible_state_dir_and_successful_probe_is_healthy(self):
        self.client.request = MagicMock(return_value={"commands": []})
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertTrue(health_check())

    def test_inaccessible_state_dir_is_unhealthy_without_ever_probing_the_cloud(self):
        # A real OS-level condition (ENOTDIR), not a mock: state_dir is
        # pointed at a path whose PARENT component is itself a regular
        # file, not a directory.
        blocking_file = Path(self._tmp.name) / "blocking_file"
        blocking_file.write_text("not a directory", encoding="utf-8")
        self.config.state_dir = str(blocking_file / "state")
        self.client.request = MagicMock(return_value={"commands": []})
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())
        self.client.request.assert_not_called()  # local check fails first -- no cloud probe attempted

    def test_completely_malformed_config_never_raises(self):
        # A TypeError from Path(None) -- a failure mode _local_checks_pass()'s
        # own OSError-only catch does NOT anticipate -- proves the OUTER
        # defensive net in make_health_check() itself.
        self.config.state_dir = None
        self.client.request = MagicMock(return_value={"commands": []})
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())  # must not raise


# -- cloud probe --------------------------------------------------------

class CloudProbeTests(HealthCheckTestCase):
    def test_successful_probe_returns_true(self):
        self.client.request = MagicMock(return_value={"commands": []})
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertTrue(health_check())
        self.client.request.assert_called_once_with("GET", "/api/appliance/commands")

    def test_auth_failure_returns_false(self):
        self.client.request = MagicMock(side_effect=PortalError("unauthorized", status_code=401))
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())

    def test_network_failure_returns_false(self):
        self.client.request = MagicMock(side_effect=PortalError("connection refused"))
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())

    def test_unexpected_exception_returns_false_not_raises(self):
        self.client.request = MagicMock(side_effect=RuntimeError("bug"))
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())  # must not raise


# -- retry/timeout budget ----------------------------------------------------

class RetryBudgetTests(HealthCheckTestCase):
    def test_first_attempt_failure_retries_once_then_succeeds(self):
        self.client.request = MagicMock(side_effect=[PortalError("timed out"), {"commands": []}])
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertTrue(health_check())
        self.assertEqual(self.client.request.call_count, 2)

    def test_never_exceeds_the_approved_max_attempts(self):
        self.client.request = MagicMock(side_effect=PortalError("timed out"))
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())
        self.assertLessEqual(self.client.request.call_count, _MAX_ATTEMPTS)
        self.client.request.assert_called()

    def test_per_attempt_timeout_overrides_the_default_and_is_restored_afterward(self):
        original_timeout = self.client.timeout
        seen_timeouts = []

        def record_and_succeed(*args, **kwargs):
            seen_timeouts.append(self.client.timeout)
            return {"commands": []}

        self.client.request = MagicMock(side_effect=record_and_succeed)
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertTrue(health_check())
        self.assertTrue(all(t < original_timeout for t in seen_timeouts))
        self.assertEqual(self.client.timeout, original_timeout)  # restored, not left mutated

    def test_timeout_is_restored_even_when_every_attempt_fails(self):
        original_timeout = self.client.timeout
        self.client.request = MagicMock(side_effect=PortalError("timed out"))
        health_check = make_health_check(self.config, self.client, now=self.clock)
        health_check()
        self.assertEqual(self.client.timeout, original_timeout)

    def test_total_wall_clock_budget_is_not_exceeded(self):
        # Simulates the FIRST attempt alone consuming the entire budget
        # -- proves the outer deadline check, not just the attempt-count
        # cap, actually bounds this.
        def slow_failure(*args, **kwargs):
            self.clock.advance(_TOTAL_BUDGET_SECONDS)
            raise PortalError("timed out")

        self.client.request = MagicMock(side_effect=slow_failure)
        health_check = make_health_check(self.config, self.client, now=self.clock)
        self.assertFalse(health_check())
        self.assertEqual(self.client.request.call_count, 1)  # second attempt skipped -- budget exhausted

    def test_second_attempt_timeout_is_capped_to_the_remaining_budget_not_a_flat_value(self):
        # The deadline check alone only decides WHETHER a second attempt
        # starts -- it does nothing to shorten that attempt's OWN
        # timeout if most of the total budget was already consumed by
        # the first attempt. A flat per-attempt timeout on every attempt
        # (ignoring how much budget is actually left) means two attempts
        # could together exceed _TOTAL_BUDGET_SECONDS even though each
        # individually stays under _PER_ATTEMPT_TIMEOUT_SECONDS. This
        # proves client.timeout is EXPLICITLY recomputed from the
        # remaining budget before each attempt, not just set once.
        seen_timeouts = []
        call_count = {"n": 0}

        def fake_request(*args, **kwargs):
            seen_timeouts.append(self.client.timeout)
            call_count["n"] += 1
            if call_count["n"] == 1:
                self.clock.advance(_TOTAL_BUDGET_SECONDS - 1.0)  # only ~1s of budget left afterward
                raise PortalError("timed out")
            return {"commands": []}

        self.client.request = MagicMock(side_effect=fake_request)
        health_check = make_health_check(self.config, self.client, now=self.clock)

        self.assertTrue(health_check())

        self.assertEqual(len(seen_timeouts), 2)
        self.assertLess(seen_timeouts[1], _PER_ATTEMPT_TIMEOUT_SECONDS)
        self.assertLessEqual(seen_timeouts[1], 1.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()
