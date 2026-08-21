"""R5 (recording-pipeline roadmap): cloud-side recording retention
enforcement.

Deletes a recording (S3 object + `recordings` catalog row) once it is
older than the OWNING CUSTOMER'S OWN current plan's retention_days --
never a hardcoded value. "Current plan" uses the same "most recent
plans row for this customer_id" query already used elsewhere in this
codebase (see partner_workspace.py's customer_first_setup()) rather
than inventing a second convention for the same fact.

Hard delete, no archive tier -- explicit instruction, and consistent
with this project's stated preference for straightforward deletion
over a second storage-tier/retrieval-time conversation.

A recording belonging to a customer with NO plan row at all, or whose
retention_days is null, is deliberately left alone (skipped, not
deleted): this sweep must never delete something it cannot compute a
real retention policy for. Missing/ambiguous data means "cannot
conclude expired", never "expire immediately" -- the same fail-safe
direction (never fail-open into data loss) every other ambiguous-state
decision in this codebase already takes.

A recording is only removed from the catalog after its S3 delete call
actually reports success -- one that can't be confirmed deleted keeps
its row too, so the next tick retries it rather than the sweep losing
track of an object that may still exist in S3.

Deletion requires its own third IAM role, distinct from R1's
write-only upload role and R4's read-only role: see
docs/r5-recording-lifecycle-iam.md. Fails closed (deletes nothing)
whenever that role is unconfigured -- the same "code built, real AWS
role applied separately" pattern R1/R4 already established.

Gated by BOTH RUNTIME_ROLE (cloud/combined, the same cloud-side
placement live_relay_idle_sweep.py already uses) AND its own dedicated
ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED flag, defaulting off -- an
explicit second opt-in beyond every other worker in this codebase,
because this one is the first that permanently deletes customer data
rather than starting/stopping a relay or uploading a copy.
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta

from partner_db import connection

logger = logging.getLogger(__name__)

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
RETENTION_SWEEP_ENABLED = os.environ.get("ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED", "false").strip().lower() == "true"
RETENTION_SWEEP_INTERVAL_SECONDS = max(300, int(os.environ.get("ANYAICAM_RECORDING_RETENTION_SWEEP_INTERVAL_SECONDS", "3600")))

recording_retention_sweep_state = {"worker_status": "not_started", "last_tick_at": None, "last_error": None, "last_deleted_count": 0}


def _customer_retention_days(db, customer_id: str) -> int | None:
    """The owning customer's own current plan's retention_days -- None
    means "no real plan on file", never a default retention value."""
    plan = db.execute(
        "SELECT retention_days FROM plans WHERE customer_id=? ORDER BY created_at DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    if not plan or plan["retention_days"] is None:
        return None
    return int(plan["retention_days"])


def _expired_candidates(db, now: datetime) -> list[dict]:
    """Every available recording whose customer has a real, on-file
    retention_days AND whose own started_at is older than that many
    days. Computed per-row in Python -- retention_days varies per
    customer, not expressible as a single static SQL WHERE clause --
    rather than a per-plan-tier UNION query."""
    candidates = []
    for row in db.execute("SELECT id, customer_id, s3_key, started_at FROM recordings WHERE status='available'").fetchall():
        retention_days = _customer_retention_days(db, row["customer_id"])
        if retention_days is None:
            continue
        try:
            started_at = datetime.fromisoformat(row["started_at"])
        except (TypeError, ValueError):
            continue  # unparseable timestamp -- never treated as expired
        if now - started_at > timedelta(days=retention_days):
            candidates.append(dict(row))
    return candidates


def _delete_recording_object(s3_key: str) -> bool:
    """Deletes one S3 object via a dedicated, delete-only STS role.
    Fails closed (returns False, deletes nothing) whenever
    unconfigured -- see docs/r5-recording-lifecycle-iam.md."""
    role_arn = os.environ.get("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "").strip()
    bucket = os.environ.get("ANYAICAM_RECORDING_S3_BUCKET", "").strip()
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
    if not role_arn or not bucket or not region:
        return False
    try:
        import boto3
    except ImportError:
        return False
    try:
        sts = boto3.client("sts", region_name=region)
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"recording-lifecycle-{secrets.token_hex(6)}",
            DurationSeconds=900,
        )
        creds = assumed["Credentials"]
        s3 = boto3.client(
            "s3", region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        s3.delete_object(Bucket=bucket, Key=s3_key)
        return True
    except Exception:
        logger.exception("recording_retention.delete_failed")
        return False


def run_retention_sweep_tick(now: datetime | None = None) -> dict:
    """One full sweep pass -- synchronous and directly callable so it's
    fully testable without asyncio. Each candidate's delete + catalog
    removal is its own short transaction, matching this project's
    existing per-item transaction granularity (see
    live_relay_idle_sweep.py's own run_idle_sweep_tick())."""
    now = now or datetime.now()
    with connection() as db:
        candidates = _expired_candidates(db, now)

    deleted = 0
    for candidate in candidates:
        if not _delete_recording_object(candidate["s3_key"]):
            continue
        with connection() as db:
            db.execute("DELETE FROM recordings WHERE id=?", (candidate["id"],))
        deleted += 1
        logger.info(
            "recording_retention.deleted recording_id=%s customer_id=%s",
            candidate["id"], candidate["customer_id"],
        )
    recording_retention_sweep_state["last_deleted_count"] = deleted
    return {"checked": len(candidates), "deleted": deleted}


async def recording_retention_sweep_worker() -> None:
    if RUNTIME_ROLE not in {"cloud", "combined"} or not RETENTION_SWEEP_ENABLED:
        recording_retention_sweep_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    recording_retention_sweep_state["worker_status"] = "running"
    logger.info("recording_retention.worker_started")
    while True:
        try:
            await asyncio.to_thread(run_retention_sweep_tick)
            recording_retention_sweep_state["last_tick_at"] = datetime.now().isoformat()
            recording_retention_sweep_state["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            recording_retention_sweep_state["last_error"] = str(error)
            logger.warning("recording_retention.worker_iteration_failed error=%s", error)
        await asyncio.sleep(RETENTION_SWEEP_INTERVAL_SECONDS)
