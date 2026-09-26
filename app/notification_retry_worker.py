"""Retries failed external (email/sms) notification deliveries -- the
"failed deliveries must be recorded and safely retryable" requirement.
notification_engine.py already records every attempt as its own
permanent row in notification_deliveries (id/notification_id/channel/
status/provider/error/recipient/attempt/created_at); this module is
the periodic worker that finds the ones genuinely worth retrying and
re-attempts them through the exact same notification_service.CHANNELS
dispatch the original send used, so a real fix (the mail server comes
back up, a rate limit clears) is picked up automatically with no
customer or operator action required.

RUNTIME_ROLE-gated to cloud/combined only, matching notification_
engine.py's own dependency (partner_db's multi-tenant connection) --
an edge appliance has no notifications/notification_deliveries rows of
its own to retry.

Only ever retries a delivery whose genuinely LATEST attempt for that
(notification_id, channel) pair is a real failure (status in
RETRYABLE_STATUSES) -- 'preview'/'unavailable'/'stored' are
configuration states, not failures, and are never retried (retrying a
Preview send forever would just write endless local preview files with
no real customer benefit). Bounded to MAX_ATTEMPTS total attempts per
(notification_id, channel), with escalating backoff between attempts
-- see _due_for_retry() -- so a genuinely down provider is retried a
few times over roughly the first two hours, then left alone rather
than hammered forever.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

from notification_service import CHANNELS
from partner_db import connection, row, rows

logger = logging.getLogger("anyaicam.notification_retry")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()

RETRYABLE_STATUSES = {"error", "failed"}
MAX_ATTEMPTS = max(1, int(os.environ.get("ANYAICAM_NOTIFICATION_RETRY_MAX_ATTEMPTS", "5")))
# Escalating backoff, indexed by (attempt number - 1): how long to wait
# after attempt N's failure before attempt N+1 is due. The last value
# repeats for any attempt count beyond the list's own length.
_BACKOFF_MINUTES = [2, 10, 30, 60, 120]
SCAN_SECONDS = max(30.0, float(os.environ.get("ANYAICAM_NOTIFICATION_RETRY_SCAN_SECONDS", "120.0")))

retry_worker_state: dict = {"worker_status": "disabled", "last_scan_at": None, "last_error": None}


def _backoff_minutes(attempt: int) -> int:
    index = min(max(attempt, 1), len(_BACKOFF_MINUTES)) - 1
    return _BACKOFF_MINUTES[index]


def _due_for_retry(delivery: dict, *, now: datetime) -> bool:
    if delivery["status"] not in RETRYABLE_STATUSES:
        return False
    if delivery["attempt"] >= MAX_ATTEMPTS:
        return False
    try:
        last_attempt_at = datetime.fromisoformat(delivery["created_at"])
    except (TypeError, ValueError):
        return False
    return now >= last_attempt_at + timedelta(minutes=_backoff_minutes(delivery["attempt"]))


def _latest_failed_deliveries(db) -> list[dict]:
    """One row per (notification_id, channel) pair -- the genuinely
    most recent delivery attempt for that pair, restricted to the
    external channels (in_app is never retried: it has no external
    provider to fail against, a failed INSERT would already have
    raised) and to a real failure status. NOT EXISTS a newer row for
    the same pair is the portable "latest per group" idiom already
    used elsewhere in this codebase's own migrations."""
    return [
        dict(item)
        for item in db.execute(
            "SELECT nd.* FROM notification_deliveries nd "
            "WHERE nd.channel IN ('email','sms') AND nd.status IN ('error','failed') "
            "AND NOT EXISTS (SELECT 1 FROM notification_deliveries nd2 "
            "WHERE nd2.notification_id = nd.notification_id AND nd2.channel = nd.channel "
            "AND nd2.created_at > nd.created_at)"
        ).fetchall()
    ]


def retry_failed_deliveries() -> dict:
    """One full pass: finds every (notification_id, channel) pair whose
    latest attempt is a due-for-retry failure, re-sends through the
    same CHANNELS dispatch, and records the outcome as a new row with
    attempt+1 -- never mutates the original failed row, preserving the
    complete audit/history trail. Returns a small stats dict for
    logging/tests."""
    now = datetime.now()
    attempted = 0
    succeeded = 0
    with connection() as db:
        candidates = _latest_failed_deliveries(db)
    for delivery in candidates:
        if not _due_for_retry(delivery, now=now):
            continue
        if not delivery.get("recipient"):
            # Pre-existing row from before the recipient column existed,
            # or a genuinely empty recipient -- nothing safe to retry to.
            continue
        with connection() as db:
            notification = row("SELECT id,title,message FROM notifications WHERE id=?", (delivery["notification_id"],))
        if not notification:
            continue
        attempted += 1
        try:
            result = CHANNELS[delivery["channel"]].send(dict(notification), delivery["recipient"])
        except Exception as error:
            result = {"status": "error", "provider": "configured", "error": str(error)}
        if result.get("status") == "sent":
            succeeded += 1
        import secrets
        with connection() as db:
            db.execute(
                "INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    secrets.token_hex(12), delivery["notification_id"], delivery["channel"],
                    result["status"], result.get("provider"), result.get("error"),
                    delivery["recipient"], delivery["attempt"] + 1, now.isoformat(),
                ),
            )
        logger.info(
            "notification_retry.attempted notification_id=%s channel=%s attempt=%s status=%s",
            delivery["notification_id"], delivery["channel"], delivery["attempt"] + 1, result.get("status"),
        )
    return {"candidates": len(candidates), "attempted": attempted, "succeeded": succeeded}


async def notification_retry_worker() -> None:
    if RUNTIME_ROLE not in {"cloud", "combined"}:
        retry_worker_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    retry_worker_state["worker_status"] = "running"
    logger.info("notification_retry.worker_started")
    while True:
        try:
            stats = await asyncio.to_thread(retry_failed_deliveries)
            retry_worker_state["last_scan_at"] = datetime.now().isoformat()
            retry_worker_state["last_error"] = None
            retry_worker_state["last_stats"] = stats
            await asyncio.sleep(SCAN_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            retry_worker_state["last_error"] = str(error)
            logger.warning("notification_retry.worker_iteration_failed error=%s", error)
            await asyncio.sleep(SCAN_SECONDS)
