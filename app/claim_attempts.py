"""Failed one-time code attempts, counted in the partner database.

The count must hold across cloud workers and restarts, so it can't live in a
process. attempt() is one atomic statement, and callers take an attempt before
checking the code: parallel requests can't all pass a check made first. A window
is fixed, so a burst across its edge can reach twice the limit.
"""
from __future__ import annotations

import time

from partner_db import connection

COUNTER_RETENTION_SECONDS = 86400


def attempt(scope: str, window_seconds: int, now: float | None = None) -> int:
    """Counts one attempt and returns how many this window now holds."""
    moment = int(now if now is not None else time.time())
    expired = moment - window_seconds
    with connection() as db:
        db.execute('DELETE FROM claim_attempt_counters WHERE window_started_at<?', (moment - COUNTER_RETENTION_SECONDS,))
        return db.execute(
            'INSERT INTO claim_attempt_counters(scope,attempts,window_started_at) VALUES(?,1,?) '
            'ON CONFLICT(scope) DO UPDATE SET '
            'attempts=CASE WHEN claim_attempt_counters.window_started_at<=? THEN 1 ELSE claim_attempt_counters.attempts+1 END,'
            'window_started_at=CASE WHEN claim_attempt_counters.window_started_at<=? THEN excluded.window_started_at ELSE claim_attempt_counters.window_started_at END '
            'RETURNING attempts',
            (scope, moment, expired, expired)).fetchone()['attempts']


def give_back(scope: str) -> None:
    """A correct code costs nothing: lookup and confirm both present it."""
    with connection() as db:
        db.execute('UPDATE claim_attempt_counters SET attempts=attempts-1 WHERE scope=? AND attempts>0', (scope,))
