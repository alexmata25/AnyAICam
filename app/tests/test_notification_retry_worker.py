"""notification_retry_worker.py (2026-09-17): regression coverage for the
periodic worker that retries failed external (email/sms) notification
deliveries -- notification_engine.py already records every attempt as
its own permanent row in notification_deliveries; this worker finds the
ones genuinely worth retrying and re-sends them through the same
CHANNELS dispatch the original send used.

Same import/isolation pattern as this suite's sibling,
test_notification_external_delivery.py.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_notification_retry_worker_import.db"):
    import notification_retry_worker as retry_worker
    from partner_db import connection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    with override_target(sqlite_path=tmp_path / "test_notification_retry_worker.db"):
        initialize_database()
        yield


class _FakeChannel:
    def __init__(self, status="sent", provider="fake", error=None, raises=False):
        self.status = status
        self.provider = provider
        self.error = error
        self.raises = raises
        self.calls = []

    def send(self, notification, recipient):
        self.calls.append((notification, recipient))
        if self.raises:
            raise RuntimeError("provider unreachable")
        return {"channel": "fake", "status": self.status, "provider": self.provider, "error": self.error}


@pytest.fixture()
def fake_channels(monkeypatch):
    channels = {"email": _FakeChannel(), "sms": _FakeChannel()}
    monkeypatch.setattr(retry_worker, "CHANNELS", channels)
    return channels


def _seed_notification(db, *, notification_id="notif-1"):
    now = "2026-09-16T00:00:00"
    partner_id = "partner-1"
    customer_id = "cust-1"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, "Test Customer", "cust-1@example.test", "active", "real", now))
    db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("user-1", partner_id, "user-1@example.test", "Owner", "customer_owner", "x", 1, customer_id, now))
    db.execute(
        "INSERT INTO notifications(id,user_id,customer_id,event_type,severity,title,message,timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (notification_id, "user-1", customer_id, "motion", "info", "Motion detected", "Camera 1 detected motion", now, now),
    )
    return notification_id


def _insert_delivery(db, *, notification_id, channel, status, attempt, created_at, recipient="user-1@example.test"):
    import secrets
    delivery_id = secrets.token_hex(12)
    db.execute(
        "INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (delivery_id, notification_id, channel, status, "configured_email", None, recipient, attempt, created_at),
    )
    return delivery_id


# --------------------------------------------------------------- _due_for_retry


def test_not_due_when_status_is_not_retryable():
    delivery = {"status": "sent", "attempt": 1, "created_at": "2026-09-01T00:00:00"}
    assert retry_worker._due_for_retry(delivery, now=datetime(2026, 9, 1, 1, 0, 0)) is False


def test_not_due_when_attempt_already_at_max():
    delivery = {"status": "error", "attempt": retry_worker.MAX_ATTEMPTS, "created_at": "2026-09-01T00:00:00"}
    assert retry_worker._due_for_retry(delivery, now=datetime(2027, 1, 1, 0, 0, 0)) is False


def test_not_due_before_the_backoff_window_elapses():
    delivery = {"status": "error", "attempt": 1, "created_at": "2026-09-01T00:00:00"}
    # attempt 1's own backoff is 2 minutes (_BACKOFF_MINUTES[0])
    assert retry_worker._due_for_retry(delivery, now=datetime(2026, 9, 1, 0, 1, 0)) is False


def test_due_once_the_backoff_window_elapses():
    delivery = {"status": "error", "attempt": 1, "created_at": "2026-09-01T00:00:00"}
    assert retry_worker._due_for_retry(delivery, now=datetime(2026, 9, 1, 0, 2, 0)) is True


def test_not_due_with_an_unparseable_created_at():
    delivery = {"status": "error", "attempt": 1, "created_at": "not-a-real-timestamp"}
    assert retry_worker._due_for_retry(delivery, now=datetime(2026, 9, 1, 1, 0, 0)) is False


def test_backoff_escalates_across_repeated_attempts():
    assert retry_worker._backoff_minutes(1) == 2
    assert retry_worker._backoff_minutes(2) == 10
    assert retry_worker._backoff_minutes(5) == 120
    # Beyond the table's own length, the last value repeats -- never
    # indexes out of range for a real, unbounded attempt count.
    assert retry_worker._backoff_minutes(99) == 120


# --------------------------------------------------------------- _latest_failed_deliveries


def test_latest_failed_deliveries_returns_only_the_newest_row_per_pair():
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=1, created_at="2026-09-01T00:00:00")
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=2, created_at="2026-09-01T00:10:00")
    with connection() as db:
        candidates = retry_worker._latest_failed_deliveries(db)
    assert len(candidates) == 1
    assert candidates[0]["attempt"] == 2


def test_latest_failed_deliveries_excludes_in_app_channel():
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="in_app", status="error", attempt=1, created_at="2026-09-01T00:00:00")
    with connection() as db:
        assert retry_worker._latest_failed_deliveries(db) == []


def test_latest_failed_deliveries_excludes_non_failure_statuses():
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="sent", attempt=1, created_at="2026-09-01T00:00:00")
        _insert_delivery(db, notification_id=notification_id, channel="sms", status="preview", attempt=1, created_at="2026-09-01T00:00:00")
    with connection() as db:
        assert retry_worker._latest_failed_deliveries(db) == []


def test_latest_failed_deliveries_stops_once_the_pair_finally_succeeds():
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=1, created_at="2026-09-01T00:00:00")
        _insert_delivery(db, notification_id=notification_id, channel="email", status="sent", attempt=2, created_at="2026-09-01T00:10:00")
    with connection() as db:
        assert retry_worker._latest_failed_deliveries(db) == []


# --------------------------------------------------------------- retry_failed_deliveries


def test_retry_failed_deliveries_resends_a_due_failure_and_records_a_new_row(fake_channels):
    old_enough = (datetime.now() - timedelta(hours=1)).isoformat()
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=1, created_at=old_enough)

    stats = retry_worker.retry_failed_deliveries()

    assert stats == {"candidates": 1, "attempted": 1, "succeeded": 1}
    assert len(fake_channels["email"].calls) == 1
    with connection() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM notification_deliveries WHERE notification_id=? ORDER BY attempt", (notification_id,)).fetchall()]
    assert len(rows) == 2
    assert rows[0]["attempt"] == 1 and rows[0]["status"] == "error"  # original row never mutated
    assert rows[1]["attempt"] == 2 and rows[1]["status"] == "sent"


def test_retry_failed_deliveries_skips_a_row_not_yet_due(fake_channels):
    just_failed = datetime.now().isoformat()
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=1, created_at=just_failed)

    stats = retry_worker.retry_failed_deliveries()

    assert stats == {"candidates": 1, "attempted": 0, "succeeded": 0}
    assert fake_channels["email"].calls == []


def test_retry_failed_deliveries_skips_a_row_with_no_recipient(fake_channels):
    old_enough = (datetime.now() - timedelta(hours=1)).isoformat()
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=1, created_at=old_enough, recipient="")

    stats = retry_worker.retry_failed_deliveries()

    assert stats == {"candidates": 1, "attempted": 0, "succeeded": 0}
    assert fake_channels["email"].calls == []


def test_retry_failed_deliveries_records_a_provider_exception_as_an_error_never_raises(fake_channels):
    fake_channels["sms"].raises = True
    old_enough = (datetime.now() - timedelta(hours=1)).isoformat()
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="sms", status="error", attempt=1, created_at=old_enough, recipient="+15555550100")

    stats = retry_worker.retry_failed_deliveries()  # must not raise

    assert stats == {"candidates": 1, "attempted": 1, "succeeded": 0}
    with connection() as db:
        latest = [dict(r) for r in db.execute("SELECT * FROM notification_deliveries WHERE notification_id=? ORDER BY attempt DESC LIMIT 1", (notification_id,)).fetchall()][0]
    assert latest["status"] == "error"
    assert latest["attempt"] == 2


def test_retry_failed_deliveries_never_exceeds_max_attempts(fake_channels):
    old_enough = (datetime.now() - timedelta(hours=3)).isoformat()
    with connection() as db:
        notification_id = _seed_notification(db)
        _insert_delivery(db, notification_id=notification_id, channel="email", status="error", attempt=retry_worker.MAX_ATTEMPTS, created_at=old_enough)

    stats = retry_worker.retry_failed_deliveries()

    # candidates counts every latest-failed (notification_id, channel)
    # row _latest_failed_deliveries() finds, before the due-for-retry
    # filter -- this row IS a real "latest failure" candidate, it's just
    # not attempted because it already hit MAX_ATTEMPTS.
    assert stats == {"candidates": 1, "attempted": 0, "succeeded": 0}
    assert fake_channels["email"].calls == []


# --------------------------------------------------------------- the safety-critical role gate


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_worker_never_runs_on_edge_role(monkeypatch):
    """Same P2P-equivalent safety-critical shape as this codebase's other
    opt-in workers: on edge role (no notifications/notification_
    deliveries rows of its own to retry), this worker must stay fully
    inert."""
    monkeypatch.setattr(retry_worker, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(retry_worker, "retry_worker_state", {"worker_status": "disabled", "last_scan_at": None, "last_error": None})
    task = asyncio.ensure_future(retry_worker.notification_retry_worker())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert retry_worker.retry_worker_state["worker_status"] == "disabled"
    assert retry_worker.retry_worker_state["last_scan_at"] is None


@pytest.mark.anyio
async def test_worker_runs_and_updates_state_on_cloud_role(monkeypatch, fake_channels):
    monkeypatch.setattr(retry_worker, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(retry_worker, "SCAN_SECONDS", 0.01)
    monkeypatch.setattr(retry_worker, "retry_worker_state", {"worker_status": "disabled", "last_scan_at": None, "last_error": None})
    task = asyncio.ensure_future(retry_worker.notification_retry_worker())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert retry_worker.retry_worker_state["worker_status"] == "running"
    assert retry_worker.retry_worker_state["last_scan_at"] is not None
    assert retry_worker.retry_worker_state["last_error"] is None
