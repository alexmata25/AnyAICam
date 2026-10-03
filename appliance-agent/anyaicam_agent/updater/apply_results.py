"""Software Update (2026-10-03): relays the root applier's progress and
outcome (updates/results/<update_id>.json) into the agent's durable update
history and to the cloud's update ledger.

The root applier is the only writer of result files. This module only reads
them, records each new state once, reports it, and -- if the root side never
picked an activation request up -- gives up after a bounded wait so a stuck
request can never block future updates forever.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Callable, Optional

from .models import TERMINAL_STATES, UpdateResult, UpdateState

log = logging.getLogger("anyaicam.agent.update")

# Root-applier states -> the agent's UpdateState vocabulary.
ROOT_STATES = {
    "preflight": UpdateState.VERIFYING,
    "installing": UpdateState.INSTALLING,
    "validating": UpdateState.HEALTH_CHECKING,
    "rolling_back": UpdateState.ROLLING_BACK,
    "healthy": UpdateState.HEALTHY,
    "rejected": UpdateState.REJECTED,
    "install_failed": UpdateState.INSTALL_FAILED,
    "rolled_back": UpdateState.ROLLED_BACK,
    "rollback_failed": UpdateState.ROLLBACK_FAILED,
}

# No result file at all this long after the request: the root side never
# started (watcher not installed, host rebooted before it ran, ...).
ACTIVATION_PICKUP_TIMEOUT_SECONDS = 30 * 60


def read_result(path: Path) -> Optional[dict]:
    """A result written by the root applier. On POSIX hosts a result file
    not owned by root (or writable by anyone else) is ignored: only the
    root applier may decide that an update succeeded."""
    path = Path(path)
    try:
        info = os.lstat(path)
        if hasattr(os, "geteuid") and (info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode)):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("state") not in ROOT_STATES:
        return None
    return data


class ResultRelay:
    def __init__(self, config, *, history, report: Callable[[UpdateResult], None], now: Callable[[], float] = time.time):
        self.config = config
        self.history = history
        self.report = report
        self._now = now

    def _reported_marker(self, update_id: str) -> Path:
        return self.config.update_reported_dir / f"{update_id}.reported"

    def poll(self) -> list:
        """Processes every in-progress update that has reached the root
        side. Returns the UpdateResults reported this call."""
        reported = []
        for update_id in self.history.in_progress_update_ids():
            row = self.history.get(update_id) or {}
            if row.get("state") not in (UpdateState.ACTIVATION_REQUESTED.value, UpdateState.VERIFYING.value,
                                        UpdateState.INSTALLING.value, UpdateState.HEALTH_CHECKING.value,
                                        UpdateState.ROLLING_BACK.value):
                continue
            result = read_result(self.config.update_results_dir / f"{update_id}.json")
            if result is None:
                if row.get("state") == UpdateState.ACTIVATION_REQUESTED.value and \
                        self._now() - float(row.get("updated_at") or row.get("created_at") or self._now()) > ACTIVATION_PICKUP_TIMEOUT_SECONDS:
                    self.history.record_transition(update_id, UpdateState.INSTALL_FAILED,
                                                   "activation_not_started: the privileged updater never picked this release up",
                                                   now=self._now())
                    shutil.rmtree(self.config.update_staged_dir / update_id, ignore_errors=True)
                    reported.append(self._report(update_id, row, UpdateState.INSTALL_FAILED,
                                                 "activation_not_started: the privileged updater never picked this release up", {}))
                continue
            state = ROOT_STATES[result["state"]]
            marker = self._reported_marker(update_id)
            last = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
            if row.get("state") != state.value:
                self.history.record_transition(update_id, state, str(result.get("error") or "")[:500], now=self._now())
            if last != state.value:
                reported.append(self._report(update_id, row, state, str(result.get("error") or ""), result))
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(state.value, encoding="utf-8")
        return reported

    def _report(self, update_id: str, row: dict, state: UpdateState, error: str, result: dict) -> UpdateResult:
        outcome = UpdateResult(
            update_id=update_id,
            from_version=str(result.get("from_version") or row.get("from_version") or ""),
            to_version=str(result.get("to_version") or row.get("to_version") or ""),
            state=state,
            error=error[:500],
            rollback_from=result.get("to_version") if state in (UpdateState.ROLLED_BACK, UpdateState.ROLLBACK_FAILED) else None,
            duration_seconds=float(result.get("duration_seconds") or 0.0),
        )
        if state in TERMINAL_STATES:
            log.log(logging.INFO if state == UpdateState.HEALTHY else logging.WARNING,
                    "Software update %s: finished as %s (%s -> %s, %.1f s)%s", update_id, state.value,
                    outcome.from_version or "?", outcome.to_version or "?", outcome.duration_seconds,
                    f" -- {outcome.error}" if outcome.error else "")
        else:
            log.info("Software update %s: privileged updater reports %s", update_id, state.value)
        try:
            self.report(outcome)
        except Exception:  # noqa: BLE001 -- report_update_result queues offline itself; never let reporting block the relay
            log.exception("Software update %s: reporting %s to the cloud failed", update_id, state.value)
        return outcome

    @staticmethod
    def is_final(state: UpdateState) -> bool:
        return state in TERMINAL_STATES
