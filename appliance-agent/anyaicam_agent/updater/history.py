"""RDM-1 (Remote Device Management, revised design approved 2026-08-14):
durable local update history -- the on-device audit log and idempotency
store for the update state machine (Group 6, not yet built).

Mirrors anyaicam_agent/queue.py's SQLite durability style: a small
schema, a `_db()` contextmanager that commits on success and always
closes, no ORM. Two tables:

  * update_attempts -- one row per update_id, the current/latest summary
    (state, attempt_count, timestamps). update_id is the PRIMARY KEY, so
    this table alone is the idempotency guard: begin_attempt() refuses to
    start fresh work for an update_id that is already terminal (see its
    docstring) -- exactly what protects against the cloud/portal
    re-delivering the same install_update command twice.
  * update_transitions -- an append-only log, one row per state change:
    the full audit trail for one update_id. Registration/check-in/update
    attempts/successes/failures/rollbacks all flow through this same
    table, since they are all just states an update_id passes through.

This module knows nothing about signatures, packages, or the filesystem
layout of staged/installed versions -- it only ever stores and retrieves
text/number rows. It imports anyaicam_agent.updater.models only for
UpdateState/TERMINAL_STATES, to know which state strings count as
"finished" for is_terminal()/in_progress_update_ids().
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

from .models import TERMINAL_STATES, UpdateState

PathLike = Union[str, Path]

# Not an UpdateState member -- deliberately NOT added to models.py's enum,
# since "abandoned" is a bookkeeping conclusion this history store itself
# reaches (via mark_abandoned(), called by future startup-recovery logic),
# not a state the update state machine itself ever transitions an
# update_id into. Treated as terminal by is_terminal() and excluded by
# in_progress_update_ids(), same as every real UpdateState in
# TERMINAL_STATES.
ABANDONED = "abandoned"

_TERMINAL_STATE_VALUES = tuple({state.value for state in TERMINAL_STATES} | {ABANDONED})


class UnknownUpdateId(Exception):
    """Raised by record_transition()/mark_abandoned() when called for an
    update_id that begin_attempt() has never been called for. Enforces
    that every transition is recorded against a row that already exists
    -- there is no implicit "create on first transition" path."""


class AlreadyTerminal(Exception):
    """Raised by begin_attempt() when update_id already has a terminal
    (or abandoned) outcome recorded. This is the idempotency/replay
    guard: callers MUST check is_terminal()/get() before calling
    begin_attempt(), and must not call it again once terminal -- doing so
    would silently re-run work that has already concluded."""


class UpdateHistory:
    def __init__(self, path: PathLike):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS update_attempts (
                    update_id TEXT PRIMARY KEY,
                    from_version TEXT NOT NULL,
                    to_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS update_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    occurred_at REAL NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_update_transitions_update_id ON update_transitions(update_id)"
            )

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # -- idempotency / attempt lifecycle -------------------------------

    def begin_attempt(
        self, update_id: str, from_version: str, to_version: str, *, now: Optional[float] = None
    ) -> int:
        """Starts (or resumes) processing for `update_id`. Returns the
        attempt number this call represents (1 for a brand-new
        update_id).

        This is the idempotency/replay-protection contract:
          * Unknown update_id -> a fresh row is inserted, attempt_count=1,
            state=VALIDATING_MANIFEST. Returns 1.
          * Known update_id, NOT YET terminal -> a previous attempt was
            interrupted (e.g. process crash mid-download, restarted with
            no durable pending-validation marker for this update_id --
            see the revised design's restart-resume protocol). This is a
            legitimate fresh attempt at the SAME update_id: attempt_count
            is incremented, state is reset to VALIDATING_MANIFEST, and
            the new attempt_count is returned.
          * Known update_id, ALREADY terminal (or abandoned) -> raises
            AlreadyTerminal. This is the replay guard: a duplicate
            install_update command for an update_id that has already
            concluded (successfully or not) must never be reprocessed.
            Callers are expected to check is_terminal()/get() first and
            short-circuit with the stored result instead of calling this.

        from_version/to_version must match the values already on record
        for a resumed (non-terminal) update_id -- a mismatch raises
        ValueError rather than silently overwriting them, since that
        would indicate the caller is confusing two different updates
        under one update_id.
        """
        occurred_at = time.time() if now is None else now
        with self._db() as db:
            existing = db.execute(
                "SELECT * FROM update_attempts WHERE update_id=?", (update_id,)
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO update_attempts"
                    "(update_id,from_version,to_version,state,attempt_count,error,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        update_id, from_version, to_version,
                        UpdateState.VALIDATING_MANIFEST.value, 1, "",
                        occurred_at, occurred_at,
                    ),
                )
                attempt_count = 1
            elif existing["state"] in _TERMINAL_STATE_VALUES:
                raise AlreadyTerminal(
                    f"update_id {update_id!r} already has a terminal outcome recorded: {existing['state']!r}."
                )
            else:
                if existing["from_version"] != from_version or existing["to_version"] != to_version:
                    raise ValueError(
                        f"update_id {update_id!r} is already on record for "
                        f"{existing['from_version']!r} -> {existing['to_version']!r}, "
                        f"not {from_version!r} -> {to_version!r}."
                    )
                attempt_count = existing["attempt_count"] + 1
                db.execute(
                    "UPDATE update_attempts SET state=?,attempt_count=?,updated_at=? WHERE update_id=?",
                    (UpdateState.VALIDATING_MANIFEST.value, attempt_count, occurred_at, update_id),
                )
            db.execute(
                "INSERT INTO update_transitions(update_id,state,detail,occurred_at) VALUES(?,?,?,?)",
                (update_id, UpdateState.VALIDATING_MANIFEST.value, f"attempt {attempt_count} started", occurred_at),
            )
        return attempt_count

    def record_transition(
        self, update_id: str, state: Union[UpdateState, str], detail: str = "", *, now: Optional[float] = None
    ) -> None:
        """Appends one state transition to the durable audit trail for
        `update_id` and updates its summary row's current state/error/
        updated_at. `state` may be an UpdateState member or the
        ABANDONED sentinel string (or any other plain string -- this
        method itself does not validate that `state` is a recognized
        value, so it can also serve as a generic append-only log for
        non-update_state events like registration/check-in, per the
        design's audit-history scope).

        Raises UnknownUpdateId if begin_attempt() was never called for
        this update_id -- there is no implicit row creation here.
        """
        state_value = state.value if isinstance(state, UpdateState) else str(state)
        occurred_at = time.time() if now is None else now
        with self._db() as db:
            existing = db.execute(
                "SELECT update_id FROM update_attempts WHERE update_id=?", (update_id,)
            ).fetchone()
            if existing is None:
                raise UnknownUpdateId(
                    f"update_id {update_id!r} has no attempt on record; call begin_attempt() first."
                )
            db.execute(
                "UPDATE update_attempts SET state=?,error=?,updated_at=? WHERE update_id=?",
                (state_value, detail, occurred_at, update_id),
            )
            db.execute(
                "INSERT INTO update_transitions(update_id,state,detail,occurred_at) VALUES(?,?,?,?)",
                (update_id, state_value, detail, occurred_at),
            )

    def mark_abandoned(self, update_id: str, detail: str = "", *, now: Optional[float] = None) -> None:
        """Records `update_id` as ABANDONED -- used by startup-recovery
        logic (a future group) for a non-terminal update_id it has
        decided NOT to resume (e.g. its staged artifacts are gone, or it
        predates a threshold this device trusts). This is a terminal
        conclusion for idempotency purposes: is_terminal() becomes True,
        and any future begin_attempt() for the same update_id raises
        AlreadyTerminal instead of reopening it. It is a bookkeeping
        record only -- it performs no filesystem cleanup itself; the
        orphaned-staging cleanup workflow is expected to call this
        AFTER it has decided (and, separately, acted on the decision)
        that a given in-progress update_id's on-disk artifacts should be
        treated as abandoned.
        """
        self.record_transition(update_id, ABANDONED, detail, now=now)

    # -- queries ----------------------------------------------------------

    def get(self, update_id: str) -> Optional[dict]:
        """Returns the current summary row for `update_id` as a dict, or
        None if begin_attempt() has never been called for it."""
        with self._db() as db:
            row = db.execute("SELECT * FROM update_attempts WHERE update_id=?", (update_id,)).fetchone()
        return dict(row) if row is not None else None

    def is_terminal(self, update_id: str) -> bool:
        """True iff `update_id` has reached a terminal UpdateState or was
        explicitly marked ABANDONED. An update_id this store has never
        seen is NOT terminal (it simply has not been attempted yet) --
        callers must not conflate "unknown" with "done"."""
        row = self.get(update_id)
        return row is not None and row["state"] in _TERMINAL_STATE_VALUES

    def in_progress_update_ids(self) -> list:
        """Returns every update_id whose current recorded state is
        neither terminal nor ABANDONED, oldest first -- the set a startup
        orphaned-staging sweep (installer, a future group) should
        cross-reference against whatever candidate/staging directories
        actually exist on disk, to decide what is a live in-progress
        update versus leftover debris from a crash that this history
        store already knows about."""
        placeholders = ",".join("?" for _ in _TERMINAL_STATE_VALUES)
        with self._db() as db:
            rows = db.execute(
                f"SELECT update_id FROM update_attempts WHERE state NOT IN ({placeholders}) ORDER BY created_at",
                _TERMINAL_STATE_VALUES,
            ).fetchall()
        return [row["update_id"] for row in rows]

    def transitions(self, update_id: str) -> list:
        """Returns the full ordered audit trail (oldest first) for
        `update_id` as a list of dicts -- every begin_attempt()/
        record_transition()/mark_abandoned() call recorded against it, in
        the order they occurred. Returns an empty list for an update_id
        that has never been seen."""
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM update_transitions WHERE update_id=? ORDER BY id", (update_id,)
            ).fetchall()
        return [dict(row) for row in rows]
