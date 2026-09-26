"""Staging Live View transport (2026-09-13): regression coverage for
live_relay_idle_sweep.py -- the cloud-side worker that auto-stops a
camera's relay once its last viewer disconnects. Had no prior test
coverage (like the rest of this feature area before this pass); confirms
the exact grace-period/claim-and-recheck behavior the module's own
docstring documents.

No FastAPI app needed here -- run_idle_sweep_tick() only touches the
database directly.
"""

import json
from datetime import datetime, timedelta

import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_live_relay_idle_sweep.db"):
    import live_relay_idle_sweep as sweep
    from partner_db import connection


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_relay_idle_sweep.db"


def _seed(db):
    now = "2026-09-13T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active',?)", (now,))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main',?)", (now,))
    db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
    db.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-1','cust-1','site-1','appl-1',1,'urn:uuid:fake','configured','Camera 1',?)", (now,)
    )


def _insert_session(db, *, session_id, camera_id, state, now):
    db.execute(
        "INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,"
        "transport,requested_at,ready_at,failed_at,expires_at,error,relay_reference) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, "cust-1", "site-1", camera_id, "user-1", "owner@example.test", "customer_owner", state,
         "not_configured", now.isoformat(), None, None, (now + timedelta(seconds=1800)).isoformat(), None, None),
    )


def _latest_stop_command(db):
    return db.execute(
        "SELECT * FROM appliance_commands WHERE command='stop_live_relay' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


# ------------------------------------------------------------- idle -> stop


def test_camera_idle_past_grace_period_queues_exactly_one_stop_command(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
            now = datetime(2026, 9, 13, 12, 0, 0)
            _insert_session(db, session_id="sess-1", camera_id="cam-1", state="stopped", now=now)

        # First tick: discovers the idle camera, starts tracking it -- not
        # yet due (grace period is 30s).
        sweep.run_idle_sweep_tick(now)
        with connection() as db:
            tracked = db.execute("SELECT * FROM live_relay_idle_tracking WHERE camera_id='cam-1'").fetchone()
            assert tracked is not None
            assert tracked["stop_queued_at"] is None
            assert _latest_stop_command(db) is None

        # Second tick, after the grace period: due, and still idle -> stop.
        later = now + timedelta(seconds=sweep.IDLE_GRACE_PERIOD_SECONDS + 1)
        sweep.run_idle_sweep_tick(later)
        with connection() as db:
            command = _latest_stop_command(db)
            assert command is not None
            payload = json.loads(command["payload_json"])
            assert payload["camera_id"] == "cam-1"
            assert payload["camera_number"] == 1
            assert command["appliance_id"] == "appl-1"

        # A third tick must never queue a second stop for the same idle cycle.
        even_later = later + timedelta(seconds=sweep.IDLE_SWEEP_INTERVAL_SECONDS)
        sweep.run_idle_sweep_tick(even_later)
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliance_commands WHERE command='stop_live_relay'").fetchone()["n"]
            assert count == 1


# ------------------------------------------------------- active viewer prevents stop


def test_a_new_viewer_before_the_grace_period_elapses_prevents_the_stop(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
            now = datetime(2026, 9, 13, 12, 0, 0)
            _insert_session(db, session_id="sess-1", camera_id="cam-1", state="stopped", now=now)

        sweep.run_idle_sweep_tick(now)  # starts tracking as idle

        # A new viewer starts before the grace period elapses -- exactly
        # what live_view_sessions.start_live_view() does in the same
        # transaction as a real session start (DELETE FROM
        # live_relay_idle_tracking WHERE camera_id=?).
        with connection() as db:
            db.execute("DELETE FROM live_relay_idle_tracking WHERE camera_id='cam-1'")
            _insert_session(db, session_id="sess-2", camera_id="cam-1", state="requested", now=now)

        later = now + timedelta(seconds=sweep.IDLE_GRACE_PERIOD_SECONDS + 1)
        sweep.run_idle_sweep_tick(later)
        with connection() as db:
            assert _latest_stop_command(db) is None
            # No stale tracking row left behind to fire a stray stop later.
            assert db.execute("SELECT * FROM live_relay_idle_tracking WHERE camera_id='cam-1'").fetchone() is None


def test_a_viewer_that_starts_after_tracking_but_before_the_claim_tick_cancels_the_stop(db_path):
    """The race Phase C's own re-check exists for: a camera was tracked
    idle, then gained a viewer, but nothing deleted the tracking row in
    between (e.g. a session inserted directly, bypassing start_live_view's
    own DELETE) -- the claim's fresh re-read of live_view_sessions must
    still catch it and queue no stop."""
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
            now = datetime(2026, 9, 13, 12, 0, 0)
            _insert_session(db, session_id="sess-1", camera_id="cam-1", state="stopped", now=now)

        sweep.run_idle_sweep_tick(now)

        with connection() as db:
            _insert_session(db, session_id="sess-2", camera_id="cam-1", state="requested", now=now)

        later = now + timedelta(seconds=sweep.IDLE_GRACE_PERIOD_SECONDS + 1)
        sweep.run_idle_sweep_tick(later)
        with connection() as db:
            assert _latest_stop_command(db) is None
            assert db.execute("SELECT * FROM live_relay_idle_tracking WHERE camera_id='cam-1'").fetchone() is None
