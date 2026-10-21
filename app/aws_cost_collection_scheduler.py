"""Daily, cloud-only scheduling of the read-only AWS cost snapshot collector.

The scheduler reuses the collector functions in ``aws_cost_snapshot_collector``
(no subprocess). It runs only when ALL of these hold:

* the process runtime role is ``cloud`` (never ``edge`` or ``combined``
  appliances, which must not hold AWS billing permissions);
* ``ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED=true``; and
* ``ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED=true``.

One collection per UTC day is claimed through the ``aws_cost_collection_runs``
table (primary key = run date), so several app workers or containers sharing
one database never collect twice the same day. Each run's outcome is stored in
that table and shown on the owner-only dashboard. Failures are classified; an
AWS permission failure is recorded as a permission problem, never as a zero
cost (the collector only persists rows AWS actually returned).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from database_backend import connect

logger = logging.getLogger("anyaicam.aws_cost_collection")

DEFAULT_SCHEDULE_HOUR_UTC = 6
DEFAULT_STARTUP_DELAY_SECONDS = 300
# A run still marked 'running' after this long belongs to a worker that died
# mid-collection; another worker may then reclaim the day.
STALE_RUN_SECONDS = 3 * 3600

PERMISSION_CODES = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException",
    "AuthorizationError", "Forbidden", "403",
}
CREDENTIAL_CODES = {
    "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "UnrecognizedClientException",
    "InvalidAccessKeyId", "SignatureDoesNotMatch",
}
CREDENTIAL_EXCEPTIONS = {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError", "NoRegionError"}

PERMISSION_GUIDANCE = {
    "cost_explorer": "ask an AWS administrator to grant ce:GetCostAndUsage to the cloud workload role",
    "lifecycle": "ask an AWS administrator to grant s3:GetLifecycleConfiguration on the recordings bucket to the cloud workload role",
    "inventory": (
        "ask an AWS administrator to grant s3:GetObject on the S3 Inventory destination prefix; "
        "S3 also answers AccessDenied when that day's manifest does not exist, because the role intentionally has no s3:ListBucket"
    ),
}
CREDENTIAL_GUIDANCE = (
    "the cloud container has no usable AWS credentials; attach the approved read-only instance role "
    "(for Docker on EC2 set the IMDSv2 hop limit to 2)"
)


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def current_runtime_role() -> str:
    # Same source and default as main.RUNTIME_ROLE.
    return os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()


def scheduler_gate(runtime_role: str | None = None) -> tuple[bool, str]:
    """(enabled, reason). The reason is safe to show to the owner."""
    role = (runtime_role if runtime_role is not None else current_runtime_role()).strip().lower()
    if role != "cloud":
        return False, f"runtime role is '{role or 'unset'}'; AWS cost collection runs only on the cloud runtime"
    missing = [name for name in ("ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED", "ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED") if not _flag(name)]
    if missing:
        return False, " and ".join(missing) + (" is" if len(missing) == 1 else " are") + " not set to true"
    return True, "enabled"


def schedule_hour_utc() -> int:
    raw = os.getenv("ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC", "").strip()
    if not raw:
        return DEFAULT_SCHEDULE_HOUR_UTC
    try:
        hour = int(raw)
    except ValueError:
        logger.warning("Invalid ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC=%r; using %s", raw, DEFAULT_SCHEDULE_HOUR_UTC)
        return DEFAULT_SCHEDULE_HOUR_UTC
    if not 0 <= hour <= 23:
        logger.warning("ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC=%s is outside 0-23; using %s", hour, DEFAULT_SCHEDULE_HOUR_UTC)
        return DEFAULT_SCHEDULE_HOUR_UTC
    return hour


def startup_delay_seconds() -> int:
    raw = os.getenv("ANYAICAM_COST_SNAPSHOT_STARTUP_DELAY_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_STARTUP_DELAY_SECONDS
    except ValueError:
        value = DEFAULT_STARTUP_DELAY_SECONDS
    return max(0, min(value, 24 * 3600))


def next_run_at(now: datetime, hour: int) -> datetime:
    now = now.astimezone(timezone.utc)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def classify_error(error: BaseException) -> tuple[str, str]:
    """(category, code) without request internals, ARNs, or credentials."""
    name = type(error).__name__
    code = ""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    text = str(error)
    if code in PERMISSION_CODES or isinstance(error, PermissionError) or "AccessDenied" in text:
        return "permission", code or "AccessDenied"
    if code in CREDENTIAL_CODES or name in CREDENTIAL_EXCEPTIONS:
        return "credentials", code or name
    if code in {"NoSuchKey", "NoSuchBucket", "404", "NotFound"}:
        return "not_found", code
    if isinstance(error, ImportError):
        return "configuration", "Boto3Unavailable"
    if code:
        return "aws_error", code
    if isinstance(error, (ValueError, RuntimeError)):
        return "validation", name
    return "internal", name


def _guidance(step: str, category: str, code: str) -> str:
    if category == "permission":
        return f"{code} — {PERMISSION_GUIDANCE.get(step, 'ask an AWS administrator to review the read-only cost role')}"
    if category == "credentials":
        return f"{code} — {CREDENTIAL_GUIDANCE}"
    if category == "not_found":
        return f"{code} — the requested AWS report or bucket does not exist"
    if category == "configuration":
        return f"{code} — boto3 is not installed in the cloud image"
    if category == "aws_error":
        return f"{code} — AWS rejected the read-only request"
    if category == "validation":
        return f"{code} — the collected AWS data failed validation and was not stored"
    return f"{code} — internal collector error"


def _inventory_manifest_candidates(run_date: date) -> list[tuple[str, str]]:
    """Exact s3 manifest locations for this run. ``{date}`` in the configured
    template is replaced by the run date and then the previous day, so a fixed
    Inventory delivery folder (e.g. ``{date}T01-00Z``) is read without LIST."""
    template = os.getenv("ANYAICAM_COST_INVENTORY_MANIFEST_S3", "").strip()
    if not template:
        return []
    if not template.startswith("s3://") or "/" not in template[5:]:
        raise ValueError("ANYAICAM_COST_INVENTORY_MANIFEST_S3 must be s3://bucket/key")
    dates = [run_date, run_date - timedelta(days=1)] if "{date}" in template else [run_date]
    candidates = []
    for value in dates:
        location = template.replace("{date}", value.isoformat())
        bucket, key = location[5:].split("/", 1)
        candidates.append((bucket, key))
    return candidates


def _collect(run_date: date) -> dict[str, dict]:
    """Run every configured read-only source independently."""
    import aws_cost_snapshot_collector as collector

    steps: dict[str, dict] = {}

    def attempt(step: str, action: Callable[[], int], field: str) -> None:
        try:
            count = int(action())
            steps[step] = {"status": "succeeded", field: count}
        except Exception as error:
            category, code = classify_error(error)
            steps[step] = {"status": "failed", "error_category": category, "error_code": code, "message": _guidance(step, category, code)}

    attempt("cost_explorer", lambda: collector.refresh_cost_explorer(run_date), "rows")

    bucket = os.getenv("ANYAICAM_RECORDING_S3_BUCKET", "").strip()
    if bucket:
        attempt("lifecycle", lambda: collector.refresh_lifecycle(bucket), "rules")
    else:
        steps["lifecycle"] = {"status": "skipped", "message": "ANYAICAM_RECORDING_S3_BUCKET is not set"}

    try:
        candidates = _inventory_manifest_candidates(run_date)
    except ValueError as error:
        steps["inventory"] = {"status": "failed", "error_category": "configuration", "error_code": "InvalidManifestLocation", "message": str(error)}
        candidates = []
    if candidates:
        def import_inventory() -> int:
            if not bucket:
                raise RuntimeError("ANYAICAM_RECORDING_S3_BUCKET must be set before importing S3 Inventory")
            import boto3
            client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
            first_error: BaseException | None = None
            for source_bucket, manifest_key in candidates:
                try:
                    return len(collector.import_inventory_manifest_s3(client, source_bucket, manifest_key))
                except Exception as error:
                    category, _code = classify_error(error)
                    # Only "this day's manifest is absent" falls through to the
                    # previous day; integrity/validation errors stop the step.
                    if category not in {"permission", "not_found"}:
                        raise
                    first_error = first_error or error
            raise first_error  # type: ignore[misc]
        attempt("inventory", import_inventory, "camera_storage_aggregates")
    elif "inventory" not in steps:
        steps["inventory"] = {"status": "skipped", "message": "ANYAICAM_COST_INVENTORY_MANIFEST_S3 is not set (no camera-level storage)"}
    return steps


def _overall(steps: dict[str, dict]) -> tuple[str, dict | None]:
    attempted = [item for item in steps.values() if item["status"] != "skipped"]
    failed = [(name, item) for name, item in steps.items() if item["status"] == "failed"]
    if not failed:
        return "succeeded", None
    first = failed[0][1]
    return ("failed" if len(failed) == len(attempted) else "partial"), first


def claim_daily_run(run_date: date, *, trigger: str, now: datetime | None = None, retry_failed: bool = False) -> str | None:
    """Atomically claim ``run_date``; returns the claim token or None if
    another worker already owns/finished it."""
    now = now or _utcnow()
    token = uuid.uuid4().hex
    day = run_date.isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO aws_cost_collection_runs(run_date,claim_token,status,trigger_source,started_at,steps_json) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(run_date) DO NOTHING",
            (day, token, "running", trigger, _iso(now), "{}"),
        )
        row = db.execute("SELECT claim_token,status,started_at FROM aws_cost_collection_runs WHERE run_date=?", (day,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        if row["claim_token"] == token:
            return token
        reclaim = False
        if row["status"] == "running":
            try:
                started = datetime.fromisoformat(str(row["started_at"]))
                reclaim = (now - started).total_seconds() > STALE_RUN_SECONDS
            except ValueError:
                reclaim = True
        elif retry_failed and row["status"] in {"failed", "partial"}:
            reclaim = True
        if not reclaim:
            return None
        db.execute(
            "UPDATE aws_cost_collection_runs SET claim_token=?,status='running',trigger_source=?,started_at=?,finished_at=NULL,"
            "error_category=NULL,error_code=NULL,message=NULL,steps_json='{}' WHERE run_date=? AND claim_token=?",
            (token, trigger, _iso(now), day, row["claim_token"]),
        )
        current = db.execute("SELECT claim_token FROM aws_cost_collection_runs WHERE run_date=?", (day,)).fetchone()
        return token if current is not None and dict(current)["claim_token"] == token else None


def _finish(run_date: date, token: str, status: str, steps: dict, first_failure: dict | None) -> None:
    with connect() as db:
        db.execute(
            "UPDATE aws_cost_collection_runs SET status=?,finished_at=?,steps_json=?,error_category=?,error_code=?,message=? "
            "WHERE run_date=? AND claim_token=?",
            (
                status, _iso(_utcnow()), json.dumps(steps, sort_keys=True),
                (first_failure or {}).get("error_category"), (first_failure or {}).get("error_code"), (first_failure or {}).get("message"),
                run_date.isoformat(), token,
            ),
        )


def run_daily_collection(*, trigger: str = "schedule", runtime_role: str | None = None, now: datetime | None = None, retry_failed: bool = False) -> dict:
    """Collect once for the current UTC day. Never raises."""
    now = now or _utcnow()
    run_date = now.astimezone(timezone.utc).date()
    enabled, reason = scheduler_gate(runtime_role)
    if not enabled:
        return {"status": "disabled", "reason": reason, "run_date": run_date.isoformat()}
    try:
        token = claim_daily_run(run_date, trigger=trigger, now=now, retry_failed=retry_failed)
    except Exception as error:
        return {"status": "error", "stage": "claim", "error": type(error).__name__, "run_date": run_date.isoformat()}
    if token is None:
        return {"status": "already_claimed", "run_date": run_date.isoformat()}
    try:
        steps = _collect(run_date)
        status, first_failure = _overall(steps)
    except Exception as error:  # defensive: _collect isolates each step
        category, code = classify_error(error)
        steps = {"collector": {"status": "failed", "error_category": category, "error_code": code, "message": _guidance("collector", category, code)}}
        status, first_failure = "failed", steps["collector"]
    try:
        _finish(run_date, token, status, steps, first_failure)
    except Exception as error:
        return {"status": "error", "stage": "record", "error": type(error).__name__, "run_date": run_date.isoformat(), "steps": steps}
    return {"status": status, "run_date": run_date.isoformat(), "steps": steps}


def _default_log(event: str, level: str = "info", **fields) -> None:
    getattr(logger, level if level in {"debug", "info", "warning", "error"} else "info")(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def _log_result(log, result: dict) -> None:
    status = result.get("status")
    if status in {"failed", "partial", "error"}:
        for step, item in (result.get("steps") or {}).items():
            if item.get("status") == "failed":
                event = "aws_cost.collection_permission_denied" if item.get("error_category") == "permission" else "aws_cost.collection_step_failed"
                log(event, level="error", step=step, error_category=item.get("error_category"), error_code=item.get("error_code"), message=item.get("message"))
        log("aws_cost.collection_finished", level="error" if status != "partial" else "warning", status=status, run_date=result.get("run_date"), stage=result.get("stage"), error=result.get("error"))
    else:
        log("aws_cost.collection_finished", status=status, run_date=result.get("run_date"), reason=result.get("reason"))


async def aws_cost_collection_worker(runtime_role: str, log=None, *, sleep=asyncio.sleep, clock: Callable[[], datetime] = _utcnow, max_iterations: int | None = None) -> None:
    """Background task: startup delay, one run, then daily at the configured
    UTC hour. Exits immediately (task done) when gated off."""
    log = log or _default_log
    enabled, reason = scheduler_gate(runtime_role)
    if not enabled:
        log("aws_cost.collection_scheduler_disabled", reason=reason)
        return
    hour = schedule_hour_utc()
    delay = startup_delay_seconds()
    log("aws_cost.collection_scheduler_started", schedule_hour_utc=hour, startup_delay_seconds=delay)
    await sleep(delay)
    iterations = 0
    while True:
        try:
            result = await asyncio.to_thread(run_daily_collection, trigger="schedule", runtime_role=runtime_role, now=clock())
            _log_result(log, result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log("aws_cost.collection_worker_error", level="error", error=type(error).__name__)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        now = clock()
        await sleep(max(60.0, (next_run_at(now, hour) - now).total_seconds()))


def _latest_runs(db) -> tuple[dict | None, str | None]:
    row = db.execute(
        "SELECT run_date,status,trigger_source,started_at,finished_at,error_category,error_code,message,steps_json "
        "FROM aws_cost_collection_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    success = db.execute(
        "SELECT finished_at FROM aws_cost_collection_runs WHERE status='succeeded' ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    last = None
    if row is not None:
        last = dict(row)
        try:
            last["steps"] = json.loads(last.pop("steps_json") or "{}")
        except ValueError:
            last["steps"] = {}
    return last, (dict(success)["finished_at"] if success is not None else None)


def collection_overview(now: datetime | None = None) -> dict:
    """Owner-facing collection state plus an explicit empty-state message.
    Never fabricates figures; reports what is and is not stored."""
    now = now or _utcnow()
    role = current_runtime_role()
    enabled, reason = scheduler_gate(role)
    hour = schedule_hour_utc()
    overview: dict = {
        "runtime_role": role,
        "scheduler_enabled": enabled,
        "disabled_reason": None if enabled else reason,
        "schedule_hour_utc": hour,
        "next_run_at": _iso(next_run_at(now, hour)) if enabled else None,
        "last_run": None,
        "last_success_at": None,
        "status_available": True,
        "snapshot_counts": None,
    }
    try:
        with connect() as db:
            last, last_success = _latest_runs(db)
            counts = {
                "cost_explorer_rows": dict(db.execute("SELECT COUNT(*) AS n FROM aws_cost_monthly_snapshots").fetchone())["n"],
                "camera_usage_rows": dict(db.execute("SELECT COUNT(*) AS n FROM aws_camera_usage_snapshots").fetchone())["n"],
                "lifecycle_rule_rows": dict(db.execute("SELECT COUNT(*) AS n FROM aws_lifecycle_rule_snapshots").fetchone())["n"],
            }
        overview.update(last_run=last, last_success_at=last_success, snapshot_counts=counts)
    except Exception as error:
        logger.warning("AWS cost collection status unavailable: %s", type(error).__name__)
        overview["status_available"] = False

    last = overview["last_run"]
    if not overview["status_available"]:
        summary = "Collection status is unavailable (the status table could not be read)."
    elif last is None:
        summary = "Last collection: never run."
    elif last["status"] == "running":
        summary = f"Last collection: started {last['started_at']} — in progress."
    elif last["status"] == "succeeded":
        summary = f"Last collection: {last['finished_at']} OK."
    else:
        label = "FAILED" if last["status"] == "failed" else "PARTIAL"
        summary = f"Last collection: {last['finished_at']} {label}: {last.get('message') or last.get('error_code') or 'unknown error'}."
    if enabled:
        summary += f" Automatic daily collection at {hour:02d}:00 UTC; next run {overview['next_run_at']}."
    else:
        summary += f" Automatic collection is disabled: {reason}."
    overview["summary_text"] = summary

    counts = overview["snapshot_counts"]
    empty = None
    if counts is not None and not any(counts.values()):
        prefix = "No AWS cost data is stored yet, so no figures are shown."
        if last is not None and last["status"] in {"failed", "partial"}:
            empty = f"{prefix} The last collection ({last['finished_at']}) failed: {last.get('message') or last.get('error_code')}."
        elif not enabled:
            empty = f"{prefix} Collection is disabled on this server ({reason}). See docs/aws-cost-dashboard.md for the owner enablement procedure."
        elif last is None:
            empty = f"{prefix} Collection is enabled but has not run yet; the next run is {overview['next_run_at']}."
        elif last["status"] == "running":
            empty = f"{prefix} A collection is in progress (started {last['started_at']})."
        else:
            empty = f"{prefix} The last collection ({last['finished_at']}) succeeded but AWS returned no rows for the requested period."
    overview["empty_state"] = empty
    return overview
