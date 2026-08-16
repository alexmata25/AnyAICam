"""Phase 6c (docs/AI_HANDOFF.md Sec 8) tests for app/live_view_sessions.py:
POST /api/customer/cameras/{camera_id}/live/start and
POST /api/customer/live/sessions/{session_id}/stop.

Route-level tests use a real, lightweight in-memory SQLite database (no
foreign-key enforcement) covering cameras/partner_users/
customer_camera_permissions/appliance_commands/live_view_sessions --
following the exact same pattern already established in
test_live_playlist.py (itself following test_customer_camera_number_wiring.py).
partner_identity()/connection()/audit() are patched; no real partner_db,
no real network/AWS call anywhere.
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
    str(Path(tempfile.gettempdir()) / "anyaicam-live-view-sessions-wiring-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-view-sessions-manifest-import-guard.json"),
)

from fastapi import FastAPI, HTTPException  # noqa: E402

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
    return db


def _seed(db, *, can_live=1, camera_number=5, customer_id="cust-1", camera_id="cam-1", appliance_id="app-1"):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number) VALUES(?,?,?,?,?)",
        (camera_id, customer_id, "site-1", appliance_id, camera_number),
    )
    db.execute("INSERT OR IGNORE INTO partner_users(id,email) VALUES(?,?)", ("user-1", "owner@example.com"))
    if can_live is not None:
        db.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES(?,?,?)",
            ("user-1", camera_id, can_live),
        )


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


IDENTITY = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"}


class LiveViewSessionsTestCase(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self._patches = [
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

    def _sessions(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM live_view_sessions").fetchall()]

    def _commands(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM appliance_commands").fetchall()]


# -- start_live_view -----------------------------------------------------

class StartLiveViewTests(LiveViewSessionsTestCase):
    def test_non_customer_owner_role_returns_403(self):
        with patch.object(live_view_sessions, "partner_identity", return_value={**IDENTITY, "role": "customer_viewer"}):
            with self.assertRaises(HTTPException) as ctx:
                start_live_view(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_camera_not_found_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            start_live_view(request=object(), camera_id="cam-missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_camera_belonging_to_another_customer_returns_404(self):
        _seed(self.db, customer_id="cust-OTHER")
        with self.assertRaises(HTTPException) as ctx:
            start_live_view(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_can_live_row_returns_403(self):
        _seed(self.db, can_live=None)
        with self.assertRaises(HTTPException) as ctx:
            start_live_view(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_can_live_zero_returns_403(self):
        _seed(self.db, can_live=0)
        with self.assertRaises(HTTPException) as ctx:
            start_live_view(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_unassigned_camera_number_returns_409(self):
        _seed(self.db, camera_number=None)
        with self.assertRaises(HTTPException) as ctx:
            start_live_view(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self._sessions(), [])
        self.assertEqual(self._commands(), [])

    def test_successful_start_creates_a_requested_session_row(self):
        _seed(self.db)
        result = start_live_view(request=object(), camera_id="cam-1")

        self.assertEqual(result["status"], "requested")
        sessions = self._sessions()
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["id"], result["session_id"])
        self.assertEqual(session["customer_id"], "cust-1")
        self.assertEqual(session["site_id"], "site-1")
        self.assertEqual(session["camera_id"], "cam-1")
        self.assertEqual(session["user_id"], "user-1")
        self.assertEqual(session["requested_by"], "owner@example.com")
        self.assertEqual(session["role"], "customer_owner")
        self.assertEqual(session["state"], "requested")
        self.assertIsNone(session["ready_at"])
        self.assertIsNone(session["failed_at"])
        self.assertEqual(session["expires_at"], result["expires_at"])

    def test_successful_start_queues_the_real_start_live_relay_command(self):
        _seed(self.db)
        start_live_view(request=object(), camera_id="cam-1")

        commands = self._commands()
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command["appliance_id"], "app-1")
        self.assertEqual(command["command"], "start_live_relay")
        self.assertEqual(command["status"], "pending")
        payload = json.loads(command["payload_json"])
        self.assertEqual(payload, {"camera_number": 5, "camera_id": "cam-1"})

    def test_session_expiry_is_in_the_future_by_the_configured_duration(self):
        _seed(self.db)
        before = datetime.now()
        result = start_live_view(request=object(), camera_id="cam-1")
        expires_at = datetime.fromisoformat(result["expires_at"])
        delta = (expires_at - before).total_seconds()
        self.assertAlmostEqual(delta, live_view_sessions.SESSION_DURATION_SECONDS, delta=5)


# -- stop_live_view -----------------------------------------------------

class StopLiveViewTests(LiveViewSessionsTestCase):
    def _start(self):
        _seed(self.db)
        return start_live_view(request=object(), camera_id="cam-1")["session_id"]

    def test_non_customer_owner_role_returns_403(self):
        session_id = self._start()
        with patch.object(live_view_sessions, "partner_identity", return_value={**IDENTITY, "role": "customer_viewer"}):
            with self.assertRaises(HTTPException) as ctx:
                stop_live_view(request=object(), session_id=session_id)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_missing_session_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            stop_live_view(request=object(), session_id="missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_session_belonging_to_another_customer_returns_404(self):
        session_id = self._start()
        with patch.object(live_view_sessions, "partner_identity", return_value={**IDENTITY, "customer_id": "cust-OTHER"}):
            with self.assertRaises(HTTPException) as ctx:
                stop_live_view(request=object(), session_id=session_id)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_successful_stop_updates_session_state(self):
        session_id = self._start()
        result = stop_live_view(request=object(), session_id=session_id)

        self.assertEqual(result, {"session_id": session_id, "status": "stopped"})
        session = self.db.execute("SELECT state FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
        self.assertEqual(session["state"], "stopped")

    def test_successful_stop_queues_the_real_stop_live_relay_command(self):
        session_id = self._start()
        stop_live_view(request=object(), session_id=session_id)

        commands = self._commands()
        self.assertEqual(len(commands), 2)  # start, then stop
        stop_command = commands[1]
        self.assertEqual(stop_command["command"], "stop_live_relay")
        self.assertEqual(stop_command["appliance_id"], "app-1")
        payload = json.loads(stop_command["payload_json"])
        self.assertEqual(payload, {"camera_number": 5, "camera_id": "cam-1"})

    def test_stopping_an_already_stopped_session_is_idempotent(self):
        session_id = self._start()
        stop_live_view(request=object(), session_id=session_id)
        commands_after_first_stop = len(self._commands())

        result = stop_live_view(request=object(), session_id=session_id)

        self.assertEqual(result, {"session_id": session_id, "status": "stopped"})
        self.assertEqual(len(self._commands()), commands_after_first_stop)  # no second stop command queued

    def test_stopping_a_session_whose_camera_is_now_unassigned_still_stops_the_session(self):
        session_id = self._start()
        self.db.execute("UPDATE cameras SET camera_number=NULL WHERE id='cam-1'")
        commands_before = len(self._commands())

        result = stop_live_view(request=object(), session_id=session_id)

        self.assertEqual(result, {"session_id": session_id, "status": "stopped"})
        session = self.db.execute("SELECT state FROM live_view_sessions WHERE id=?", (session_id,)).fetchone()
        self.assertEqual(session["state"], "stopped")
        self.assertEqual(len(self._commands()), commands_before)  # no stop command queued -- no valid camera_number


# -- lazy expiry sweep ----------------------------------------------------

class LazyExpirySweepTests(LiveViewSessionsTestCase):
    def test_a_stale_requested_session_is_swept_to_expired_on_the_next_start_call(self):
        _seed(self.db, camera_id="cam-1", customer_id="cust-1")
        past = (datetime.now() - timedelta(seconds=1)).isoformat()
        self.db.execute(
            "INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
            "transport,requested_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("stale-session", "cust-1", "site-1", "cam-1", "user-1", "owner@example.com", "customer_owner",
             "requested", "not_configured", past, past),
        )

        _seed(self.db, camera_id="cam-2", customer_id="cust-1", appliance_id="app-2")
        start_live_view(request=object(), camera_id="cam-2")

        stale_row = self.db.execute("SELECT state FROM live_view_sessions WHERE id='stale-session'").fetchone()
        self.assertEqual(stale_row["state"], "expired")

    def test_a_stale_requested_session_is_swept_to_expired_on_the_next_stop_call(self):
        _seed(self.db)
        session_id = start_live_view(request=object(), camera_id="cam-1")["session_id"]
        past = (datetime.now() - timedelta(seconds=1)).isoformat()
        self.db.execute(
            "INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
            "transport,requested_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("stale-session", "cust-1", "site-1", "cam-1", "user-1", "owner@example.com", "customer_owner",
             "requested", "not_configured", past, past),
        )

        stop_live_view(request=object(), session_id=session_id)

        stale_row = self.db.execute("SELECT state FROM live_view_sessions WHERE id='stale-session'").fetchone()
        self.assertEqual(stale_row["state"], "expired")

    def test_sweep_never_overwrites_an_already_stopped_session(self):
        _seed(self.db)
        past = (datetime.now() - timedelta(seconds=1)).isoformat()
        self.db.execute(
            "INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
            "transport,requested_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("already-stopped", "cust-1", "site-1", "cam-1", "user-1", "owner@example.com", "customer_owner",
             "stopped", "not_configured", past, past),
        )

        _seed(self.db, camera_id="cam-2", appliance_id="app-2")
        start_live_view(request=object(), camera_id="cam-2")

        row = self.db.execute("SELECT state FROM live_view_sessions WHERE id='already-stopped'").fetchone()
        self.assertEqual(row["state"], "stopped")  # unchanged, never reverted to 'expired'


if __name__ == "__main__":
    unittest.main()
