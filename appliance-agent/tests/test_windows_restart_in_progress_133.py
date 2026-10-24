"""Windows 1.3.3 D3b (Blue, Sandbox F-H on 8f7a24b0, 2026-10-22): the cloud's restart_vms ran while the VMS's own
`restart!` (D3 fix) was still stopping the service; WinSW answered "FATAL Cannot stop 'AnyAiCamVMS' service" and the
command was reported failed although the VMS came back in the new mode. The agent now waits for a pending stop/start to
settle and runs the restart once more; every other failure is reported as before. Every external call is faked."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent import windows

STOP_PENDING = 'SERVICE_NAME: AnyAiCamVMS\n        STATE              : 3  STOP_PENDING\n'
START_PENDING = 'SERVICE_NAME: AnyAiCamVMS\n        STATE              : 2  START_PENDING\n'
RUNNING = 'SERVICE_NAME: AnyAiCamVMS\n        STATE              : 4  RUNNING\n'


class Scripted:
    """Answers `sc query` from a list of states and the WinSW wrapper from a list of results."""

    def __init__(self, wrapper_results, states):
        self.wrapper_results, self.states, self.calls = list(wrapper_results), list(states), []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'sc':
            return subprocess.CompletedProcess(argv, 0, self.states.pop(0) if len(self.states) > 1 else self.states[0], '')
        outcome = self.wrapper_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome[0], outcome[1], '')

    def wrapper_calls(self):
        return [c for c in self.calls if c[0] != 'sc']


class RestartInProgressTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(windows, 'IS_WINDOWS', True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'AnyAiCam'
        (self.root / 'service').mkdir(parents=True)
        for wrapper in ('AnyAiCamVMS.exe', 'AnyAiCamAgent.exe'):
            (self.root / 'service' / wrapper).write_bytes(b'')
        self.vms = [str(self.root / 'service' / 'AnyAiCamVMS.exe'), 'restart']
        self.slept = []

    def action(self, run, name='restart_vms'):
        return windows.run_privileged_action(name, root=self.root, run=run, sleep=self.slept.append)

    def test_d3b_restart_during_the_vms_self_restart_waits_and_completes(self):
        run = Scripted([(1, "FATAL - Cannot stop 'AnyAiCamVMS' service on computer '.'."), (0, '')],
                       [STOP_PENDING, STOP_PENDING, START_PENDING, RUNNING])
        status, result, error = self.action(run)
        self.assertEqual((status, error), ('completed', ''))
        self.assertEqual(result, {'requested': True, 'action': 'restart_vms',
                                  'retried_after_restart_in_progress': True, 'state_before_retry': 'running'})
        self.assertEqual(run.wrapper_calls(), [self.vms, self.vms])  # the cloud's restart really happens, once more
        self.assertTrue(self.slept)

    def test_a_busy_message_alone_is_enough_when_the_stop_already_finished(self):
        run = Scripted([(1, "FATAL - Cannot stop 'AnyAiCamVMS' service on computer '.'."), (0, '')], [RUNNING])
        self.assertEqual(self.action(run)[0], 'completed')
        self.assertEqual(run.wrapper_calls(), [self.vms, self.vms])

    def test_a_retry_refused_as_busy_completes_once_the_service_is_running(self):
        # D3c: a restart in progress after the command arrived that ends RUNNING is the restart the cloud asked for.
        run = Scripted([(1, 'Cannot stop'), (1, 'Cannot stop')], [STOP_PENDING, RUNNING])
        status, result, error = self.action(run)
        self.assertEqual((status, error), ('completed', ''))
        self.assertTrue(result['completed_by_restart_in_progress'])
        self.assertEqual(len(run.wrapper_calls()), 2)  # still one retry only

    def test_a_retry_that_fails_with_another_error_is_reported_failed(self):
        run = Scripted([(1, 'Cannot stop'), (5, 'Access is denied.')], [STOP_PENDING, RUNNING])
        status, result, error = self.action(run)
        self.assertEqual((status, result), ('failed', {}))
        self.assertIn('exited with 5', error)
        self.assertEqual(len(run.wrapper_calls()), 2)

    # ------------------------------------------------------------ D3c (Sandbox U132-L on 080bfe15, 2026-10-23)
    STOPPED = 'SERVICE_NAME: AnyAiCamVMS\n        STATE              : 1  STOPPED\n'

    def test_d3c_the_stopped_moment_inside_the_self_restart_is_not_settled(self):
        # restart! = stop, a brief STOPPED, then start: the retry must wait for the start, not run in between.
        run = Scripted([(1, "FATAL - Cannot stop 'AnyAiCamVMS' service on computer '.'."), (0, '')],
                       [STOP_PENDING, self.STOPPED, START_PENDING, RUNNING])
        status, result, error = self.action(run)
        self.assertEqual((status, error), ('completed', ''))
        self.assertEqual(result['state_before_retry'], 'running')

    def test_d3c_the_sandbox_sequence_cannot_stop_then_cannot_start_completes(self):
        # The exact U132-L log: "Cannot stop" (19:36:35), STOPPED between stop and start (19:36:58.55), the retry gets
        # "Cannot start" (19:36:59), and the VMS's own restart then finishes RUNNING. Old code: reported failed.
        run = Scripted([(1, "FATAL - Cannot stop 'AnyAiCamVMS' service on computer '.'."),
                        (1, "FATAL - Cannot start service 'AnyAiCamVMS' on computer '.'.")],
                       [STOP_PENDING, self.STOPPED, START_PENDING, RUNNING])
        status, result, error = self.action(run)
        self.assertEqual((status, error), ('completed', ''))
        self.assertEqual(result, {'requested': True, 'action': 'restart_vms', 'retried_after_restart_in_progress': True,
                                  'state_before_retry': 'running', 'completed_by_restart_in_progress': True})
        self.assertEqual(len(run.wrapper_calls()), 2)

    def test_a_service_that_stays_stopped_is_settled_after_the_stable_polls(self):
        run = Scripted([(1, 'Cannot stop'), (0, '')], [STOP_PENDING, self.STOPPED])
        status, result, _ = self.action(run)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['state_before_retry'], 'stopped')
        self.assertEqual(len(self.slept), windows.STOPPED_STABLE_POLLS - 1)

    def test_a_busy_retry_after_which_the_service_stays_stopped_is_reported_failed(self):
        run = Scripted([(1, 'Cannot stop'), (1, 'Cannot start')], [STOP_PENDING, self.STOPPED])
        status, result, error = self.action(run)
        self.assertEqual((status, result), ('failed', {}))
        self.assertIn('exited with 1', error)

    def test_a_retry_that_cannot_run_is_reported_failed(self):
        run = Scripted([(1, 'Cannot stop'), OSError('access denied')], [STOP_PENDING, RUNNING])
        status, _, error = self.action(run)
        self.assertEqual(status, 'failed')
        self.assertIn('after a restart already in progress', error)

    def test_a_service_stuck_pending_is_waited_for_a_bounded_time(self):
        clock = [0.0]

        def sleep(seconds):
            self.slept.append(seconds)
            clock[0] += seconds

        run = Scripted([(1, 'Cannot stop'), (1, '')], [STOP_PENDING])
        with patch.object(windows.time, 'monotonic', lambda: clock[0]):
            status = windows.run_privileged_action('restart_vms', root=self.root, run=run, sleep=sleep)[0]
        self.assertEqual(status, 'failed')
        self.assertLessEqual(sum(self.slept), windows.RESTART_SETTLE_SECONDS + 2)

    def test_other_failures_are_not_retried(self):
        run = Scripted([(5, 'Access is denied.')], [RUNNING])
        status, _, error = self.action(run)
        self.assertEqual(status, 'failed')
        self.assertIn('exited with 5', error)
        self.assertEqual(run.wrapper_calls(), [self.vms])
        self.assertEqual(self.slept, [])

    def test_the_agent_restart_is_unchanged(self):
        run = Scripted([(1, 'Cannot stop')], [STOP_PENDING])
        status = self.action(run, 'restart_agent')[0]
        self.assertEqual(status, 'failed')
        self.assertEqual(run.wrapper_calls(), [[str(self.root / 'service' / 'AnyAiCamAgent.exe'), 'restart!']])

    def test_a_normal_restart_is_unchanged(self):
        run = Scripted([(0, '')], [RUNNING])
        self.assertEqual(self.action(run), ('completed', {'requested': True, 'action': 'restart_vms'}, ''))
        self.assertEqual([c for c in run.calls if c[0] == 'sc'], [])


if __name__ == '__main__':
    unittest.main()
