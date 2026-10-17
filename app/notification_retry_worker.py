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
# A claim older than this is taken to be from a worker that stopped mid-send and
# may be taken again; a send takes seconds, so a live one is never retaken.
CLAIM_STALE_SECONDS = 300
PENDING_MEDIA_CLAIM_CHANNEL = "email:pending_media"

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


def _claim(delivery: dict, *, channel: str, attempt: int, now: datetime) -> bool:
    """Atomically takes the right to make this attempt (Green 2026-10-16).
    Two workers that both read the same failed (or held) row used to both
    send it. Only the worker whose claim row is created (or that retakes a
    stale one) sends -- and only if no newer delivery row for the pair
    exists by then, so a worker holding an old candidate list, or one
    retaking a stale claim whose send did finish, never sends a second
    copy. The claim is written (and committed) before the provider call."""
    stale_before = (now - timedelta(seconds=CLAIM_STALE_SECONDS)).isoformat()
    with connection() as db:
        claimed = db.execute(
            "INSERT INTO notification_delivery_claims(notification_id,channel,attempt,claimed_at) VALUES(?,?,?,?) "
            "ON CONFLICT(notification_id,channel,attempt) DO UPDATE SET claimed_at=excluded.claimed_at "
            "WHERE notification_delivery_claims.claimed_at<?",
            (delivery["notification_id"], channel, attempt, now.isoformat(), stale_before),
        ).rowcount
    if not claimed:
        return False
    with connection() as db:
        newer = db.execute(
            "SELECT 1 FROM notification_deliveries WHERE notification_id=? AND channel=? AND id!=? "
            "AND (attempt>? OR created_at>?) LIMIT 1",
            (delivery["notification_id"], delivery["channel"], delivery["id"], int(delivery["attempt"] or 0),
             delivery["created_at"]),
        ).fetchone()
    return newer is None


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
            notification = row("SELECT id,title,message,event_type FROM notifications WHERE id=?", (delivery["notification_id"],))
        if not notification:
            continue
        # The operator email allowlist applies to retries too, so narrowing
        # email alerts can never be undone by re-sending older failures.
        from notification_engine import email_alert_allowed, sms_alert_allowed
        if delivery["channel"] == "email" and not email_alert_allowed(str(notification["event_type"] or "")):
            continue
        # The same for the SMS allowlist: an older failed person/motion SMS
        # is never re-sent once SMS is narrowed to urgent types.
        if delivery["channel"] == "sms" and not sms_alert_allowed(str(notification["event_type"] or "")):
            continue
        notification = {key: notification[key] for key in ("id", "title", "message")}
        if not _claim(delivery, channel=delivery["channel"], attempt=int(delivery["attempt"] or 0) + 1, now=now):
            continue  # another worker is making (or already made) this attempt
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
                "INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at,"
                "provider_message_id,provider_error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    secrets.token_hex(12), delivery["notification_id"], delivery["channel"],
                    result["status"], result.get("provider"), result.get("error"),
                    delivery["recipient"], delivery["attempt"] + 1, now.isoformat(),
                    result.get("provider_message_id"), result.get("provider_error_code"),
                ),
            )
        logger.info(
            "notification_retry.attempted notification_id=%s channel=%s attempt=%s status=%s",
            delivery["notification_id"], delivery["channel"], delivery["attempt"] + 1, result.get("status"),
        )
    return {"candidates": len(candidates), "attempted": attempted, "succeeded": succeeded}


PENDING_MEDIA_SCAN_SECONDS = max(5.0, float(os.environ.get("ANYAICAM_ALERT_EMAIL_PENDING_SCAN_SECONDS", "15")))


def send_pending_media_emails(*, now: datetime | None = None) -> dict:
    """Sends detection-alert emails held by notification_engine until
    their thumbnail reached the cloud (2026-09-27), or once
    notification_email.MEDIA_WAIT_SECONDS has passed (link only). One
    new delivery row per send (attempt 1) -- a failure then follows the
    normal retry path above. The email allowlist is re-checked, so a
    narrowed allowlist also covers already-held emails."""
    import notification_email
    from notification_engine import (VOICE_CALL_ENTRANCE_GRACE_SECONDS, awaits_possible_voice_call,
                                     email_alert_allowed, voice_call_supersedes_email)
    now = now or datetime.now()
    sent = held = skipped = 0
    with connection() as db:
        pending = [dict(r) for r in db.execute(
            "SELECT nd.* FROM notification_deliveries nd WHERE nd.channel='email' AND nd.status='pending_media' "
            "AND NOT EXISTS (SELECT 1 FROM notification_deliveries later WHERE later.notification_id=nd.notification_id "
            "AND later.channel='email' AND later.rowid>nd.rowid) ORDER BY nd.rowid LIMIT 200").fetchall()]
    for delivery in pending:
        with connection() as db:
            context = notification_email.alert_context(db, delivery["notification_id"])
        if not context:
            continue
        try:
            age = (now - datetime.fromisoformat(delivery["created_at"])).total_seconds()
        except (TypeError, ValueError):
            age = notification_email.MEDIA_WAIT_SECONDS
        try:
            held_since = datetime.fromisoformat(delivery["created_at"])
        except (TypeError, ValueError):
            held_since = now
        with connection() as db:
            superseded = voice_call_supersedes_email(
                db, camera_id=context.get("camera_id"), event_type=str(context.get("event_type") or ""), at=held_since,
            )
            entrance = not superseded and age < VOICE_CALL_ENTRANCE_GRACE_SECONDS and awaits_possible_voice_call(
                db, camera_id=context.get("camera_id"), event_type=str(context.get("event_type") or ""),
            )
        if not email_alert_allowed(str(context.get("event_type") or "")):
            status, result = "skipped_allowlist", {"provider": "configured_email", "error": None}
        elif superseded:
            # The same visit already produced an AAC Voice Call email.
            status, result = "skipped_voice_call", {"provider": "configured_email", "error": None}
        elif entrance:
            # Entrance camera: give the Voice Call time to exist first.
            held += 1
            continue
        elif notification_email.media_ready(context) or age >= notification_email.MEDIA_WAIT_SECONDS:
            status, result = None, None
        else:
            held += 1
            continue
        # Resolving a held email (sent or skipped) is claimed like a retry: one worker only.
        if not _claim(delivery, channel=PENDING_MEDIA_CLAIM_CHANNEL, attempt=1, now=now):
            continue
        if status is None:
            try:
                result = CHANNELS["email"].send({"id": context["id"], "title": context["title"], "message": context["message"]}, delivery["recipient"])
            except Exception as error:
                result = {"status": "error", "provider": "configured", "error": str(error)}
            status = result.get("status")
            sent += status == "sent"
        else:
            skipped += 1
        import secrets
        with connection() as db:
            db.execute(
                "INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (secrets.token_hex(12), delivery["notification_id"], "email", status, result.get("provider"),
                 result.get("error"), delivery["recipient"], 1, now.isoformat()),
            )
    return {"pending": len(pending), "sent": sent, "held": held, "skipped": skipped}


async def notification_retry_worker() -> None:
    if RUNTIME_ROLE not in {"cloud", "combined"}:
        retry_worker_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    retry_worker_state["worker_status"] = "running"
    logger.info("notification_retry.worker_started")
    last_retry_scan = 0.0
    while True:
        try:
            # Held detection-alert emails go out on a fast cadence; the
            # (heavier) retry scan keeps its own SCAN_SECONDS cadence.
            retry_worker_state["last_pending_media"] = await asyncio.to_thread(send_pending_media_emails)
            loop_now = asyncio.get_running_loop().time()
            if loop_now - last_retry_scan >= SCAN_SECONDS:
                last_retry_scan = loop_now
                stats = await asyncio.to_thread(retry_failed_deliveries)
                retry_worker_state["last_scan_at"] = datetime.now().isoformat()
                retry_worker_state["last_stats"] = stats
            retry_worker_state["last_error"] = None
            await asyncio.sleep(PENDING_MEDIA_SCAN_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            retry_worker_state["last_error"] = str(error)
            logger.warning("notification_retry.worker_iteration_failed error=%s", error)
            await asyncio.sleep(PENDING_MEDIA_SCAN_SECONDS)
