"""Phase 6e (docs/AI_HANDOFF.md Sec 8) tests for app/live_relay_idle_sweep.py.

Covers the idle-relay auto-stop lifecycle: idle detection (Phase A/B),
the atomic claim-and-stop-if-still-idle gate (Phase C), and the
cross-module interaction with live_view_sessions.start_live_view()'s own
tracking-row cleanup. Uses the same lightweight in-memory SQLite fixture
pattern already established in test_live_view_sessions.py/
test_live_playlist.py; connection()/audit() are patched, no real
partner_db, no real network/AWS call anywhere.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-relay-idle-sweep-wiring-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-relay-idle-sweep-manifest-import-guard.json"),
)

from fastapi import FastAPI  # noqa: E402

import live_relay_idle_sweep  # noqa: E402
import live_view_sessions  # noqa: E402


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
live_view_sessions.register_live_view_session_routes(_ROUTE_APP)
start_live_view = _endpoint(_ROUTE_APP, "/api/customer/cameras/{camera_id}/live/start")
stop_live_view = _endpoint(_ROUTE_APP, "/api/customer/live/sessions/{session_id}/stop")


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, "
        "appliance_id TEXT, camera_number INTEGER)"
    )
    db.execute("CREATE TABLE partner_users(id TEXT PRIMARY KEY, email TEXT)")
    db.execute(
        "CREATE TABLE customer_camera_permissions(user_id TEXT, camera_id TEXT, can_live INTEGER)"
    )
    db.execute(
        "CREATE TABLE appliance_commands(id TEXT PRIMARY KEY, appliance_id TEXT, command TEXT, "
        "payload_json TEXT, status TEXT, created_at TEXT, delivered_at TEXT, completed_at TEXT, "
        "expires_at TEXT, error TEXT, created_by TEXT)"
    )
    db.execute(
        "CREATE TABLE live_view_sessions(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, "
        "camera_id TEXT, user_id TEXT, requested_by TEXT, role TEXT, state TEXT, transport TEXT, "
        "requested_at TEXT, ready_at TEXT, failed_at TEXT, expires_at TEXT, error TEXT, relay_reference TEXT)"
    )
    db.execute(
        "CREATE TABLE live_relay_idle_tracking(camera_id TEXT PRIMARY KEY, appliance_id TEXT, "
        "idle_since TEXT, stop_queued_at TEXT)"
    )
    return db


def _seed_camera(db, *, camera_id="cam-1", customer_id="cust-1", appliance_id="app-1", camera_number=5):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number) VALUES(?,?,?,?,?)",
        (camera_id, customer_id, "site-1", appliance_id, camera_number),
    )


def _seed_customer_owner_permission(db, camera_id="cam-1"):
    db.execute("INSERT OR IGNORE INTO partner_users(id,email) VALUES(?,?)", ("user-1", "owner@example.com"))
    db.execute(
        "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES(?,?,?)",
        ("user-1", camera_id, 1),
    )


def _insert_session(db, *, session_id, camera_id="cam-1", customer_id="cust-1", state="requested", when=None):
    when = when or datetime.now()
    db.execute(
        "INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
        "transport,requested_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, customer_id, "site-1", camera_id, "user-1", "owner@example.com", "customer_owner",
         state, "not_configured", when.isoformat(), (when + timedelta(seconds=1800)).isoformat()),
    )


def _tracking_row(db, camera_id="cam-1"):
    row = db.execute("SELECT * FROM live_relay_idle_tracking WHERE camera_id=?", (camera_id,)).fetchone()
    return dict(row) if row else None


def _stop_commands(db):
    return [
        dict(row) for row in db.execute(
            "SELECT * FROM appliance_commands WHERE command='stop_live_relay' ORDER BY created_at"
        ).fetchall()
    ]


def _idle_sweep_stop_commands(db) -> int:
    """Count only stop_live_relay commands the idle sweep itself queued
    (created_by='live-relay-idle-sweep'), excluding any the customer's
    own explicit stop_live_view() call queued -- the two are independent
    and this project's established convention already distinguishes
    queued-command origin via created_by (e.g. 'customer-live-view')."""
    return len([row for row in _stop_commands(db) if row["created_by"] == "live-relay-idle-sweep"])


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


IDENTITY = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"}
GRACE = live_relay_idle_sweep.IDLE_GRACE_PERIOD_SECONDS


class IdleSweepTestCase(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self._patches = [
            patch.object(live_relay_idle_sweep, "connection", return_value=_ConnectionContext(self.db)),
            patch.object(live_relay_idle_sweep, "audit"),
            patch.object(live_view_sessions, "partner_identity", return_value=IDENTITY),
            patch.object(live_view_sessions, "connection", return_value=_ConnectionContext(self.db)),
            patch.object(live_view_sessions, "audit"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.db.close()


# -- discovery / tracking (Phase A / B) -----------------------------------

class DiscoveryAndTrackingTests(IdleSweepTestCase):
    def test_camera_with_an_active_session_is_never_tracked(self):
        _seed_camera(self.db)
        _insert_session(self.db, session_id="s1", state="requested")
        live_relay_idle_sweep.run_idle_sweep_tick(now=datetime.now())
        self.assertIsNone(_tracking_row(self.db))

    def test_camera_with_no_session_history_is_never_tracked(self):
        _seed_camera(self.db)
        live_relay_idle_sweep.run_idle_sweep_tick(now=datetime.now())
        self.assertIsNone(_tracking_row(self.db))

    def test_idle_since_is_not_reset_by_repeated_observation(self):
        _seed_camera(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)
        first_idle_since = _tracking_row(self.db)["idle_since"]

        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=5))

        self.assertEqual(_tracking_row(self.db)["idle_since"], first_idle_since)

    def test_deleted_camera_is_never_tracked(self):
        _insert_session(self.db, session_id="s1", camera_id="cam-ghost", state="stopped")
        live_relay_idle_sweep.run_idle_sweep_tick(now=datetime.now())
        self.assertIsNone(_tracking_row(self.db, "cam-ghost"))


# -- exactly one stop per idle cycle (Phase C claim) -----------------------

class OneStopPerIdleCycleTests(IdleSweepTestCase):
    def test_one_idle_cycle_queues_at_most_one_stop_across_unlimited_later_sweeps(self):
        _seed_camera(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")  # no active viewer
        t0 = datetime(2026, 1, 1, 12, 0, 0)

        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)
        self.assertIsNone(_tracking_row(self.db)["stop_queued_at"])
        self.assertEqual(len(_stop_commands(self.db)), 0)  # not due yet

        after_grace = t0 + timedelta(seconds=GRACE + 1)
        live_relay_idle_sweep.run_idle_sweep_tick(now=after_grace)
        self.assertEqual(len(_stop_commands(self.db)), 1)
        first_stop_queued_at = _tracking_row(self.db)["stop_queued_at"]
        self.assertIsNotNone(first_stop_queued_at)

        for extra_seconds in (10, 100, 10_000):
            live_relay_idle_sweep.run_idle_sweep_tick(now=after_grace + timedelta(seconds=extra_seconds))

        self.assertEqual(len(_stop_commands(self.db)), 1)  # still exactly one, ever
        self.assertEqual(_tracking_row(self.db)["stop_queued_at"], first_stop_queued_at)

    def test_not_due_before_the_grace_period_elapses(self):
        _seed_camera(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)

        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE - 1))

        self.assertEqual(len(_stop_commands(self.db)), 0)


# -- viewer-return races -----------------------------------------------

class ViewerReturnTests(IdleSweepTestCase):
    def test_viewer_return_clears_the_completed_idle_cycle(self):
        _seed_camera(self.db)
        _seed_customer_owner_permission(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 1))
        self.assertEqual(len(_stop_commands(self.db)), 1)
        self.assertIsNotNone(_tracking_row(self.db)["stop_queued_at"])

        start_live_view(request=object(), camera_id="cam-1")  # a viewer returns

        self.assertIsNone(_tracking_row(self.db))  # tracking row fully cleared

    def test_subsequent_new_idle_cycle_can_queue_one_new_stop(self):
        _seed_camera(self.db)
        _seed_customer_owner_permission(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 1))
        self.assertEqual(_idle_sweep_stop_commands(self.db), 1)

        session_id = start_live_view(request=object(), camera_id="cam-1")["session_id"]
        stop_live_view(request=object(), session_id=session_id)  # viewer leaves again -- this
        # explicit stop queues its own stop_live_relay too (existing,
        # unchanged live_view_sessions.py behavior); counted separately
        # below via created_by so it doesn't mask the idle sweep's own count.

        t1 = t0 + timedelta(seconds=GRACE + 61)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t1)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t1 + timedelta(seconds=GRACE + 1))

        self.assertEqual(_idle_sweep_stop_commands(self.db), 2)  # exactly one new stop, for the new cycle

    def test_active_viewer_recheck_prevents_a_stale_stop(self):
        _seed_camera(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)  # Phase A/B tracks from a snapshot that will go stale
        self.assertIsNotNone(_tracking_row(self.db))

        # A viewer starts watching in the gap before the grace-period
        # claim -- inserted directly (not via start_live_view()) to prove
        # Phase C's own re-check catches this even though nothing else
        # touched the tracking row in the meantime.
        _insert_session(self.db, session_id="s2", state="requested")

        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 1))

        self.assertEqual(len(_stop_commands(self.db)), 0)  # no stale stop queued
        self.assertIsNone(_tracking_row(self.db))  # stale tracking row cleaned up


# -- concurrency / idempotency with the explicit stop path -----------------

class ConcurrentExplicitStopTests(IdleSweepTestCase):
    def test_concurrent_explicit_stop_remains_harmless(self):
        _seed_camera(self.db)
        _seed_customer_owner_permission(self.db)
        session_id = start_live_view(request=object(), camera_id="cam-1")["session_id"]
        stop_live_view(request=object(), session_id=session_id)  # customer's own explicit stop

        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 1))

        # The explicit stop already queued its own stop_live_relay
        # command; the idle sweep's own stop is a second, harmless,
        # idempotent stop_live_relay for the same camera -- never an
        # error, and never more than one from the sweep itself.
        stop_commands = _stop_commands(self.db)
        self.assertEqual(len(stop_commands), 2)  # one from the explicit stop, one from the idle sweep
        for command in stop_commands:
            payload = json.loads(command["payload_json"])
            self.assertEqual(payload, {"camera_number": 5, "camera_id": "cam-1"})

        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 100))
        self.assertEqual(len(_stop_commands(self.db)), 2)  # re-running the sweep never adds a third


# -- observability / secret sanitization -----------------------------------

class ObservabilityTests(IdleSweepTestCase):
    def test_stop_command_payload_never_contains_forbidden_keys(self):
        _seed_camera(self.db)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 1))

        command = _stop_commands(self.db)[0]
        payload = json.loads(command["payload_json"])
        forbidden = {'username', 'password', 'camera_username', 'camera_password', 'rtsp_url', 'credentials', 'secret'}
        self.assertEqual(set(payload) & forbidden, set())
        self.assertEqual(command["created_by"], "live-relay-idle-sweep")

    def test_unassigned_camera_number_claims_but_queues_no_command(self):
        _seed_camera(self.db, camera_number=None)
        _insert_session(self.db, session_id="s1", state="stopped")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        live_relay_idle_sweep.run_idle_sweep_tick(now=t0)

        live_relay_idle_sweep.run_idle_sweep_tick(now=t0 + timedelta(seconds=GRACE + 1))

        self.assertEqual(len(_stop_commands(self.db)), 0)
        self.assertIsNotNone(_tracking_row(self.db)["stop_queued_at"])  # claim stands, never retried


if __name__ == "__main__":
    unittest.main()
