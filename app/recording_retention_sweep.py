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


def _event_media_retention_days(db, customer_id: str) -> int | None:
    """Cloud EVENT clips/thumbnails (2026-10-05, after Codex review of
    453bd1c): the retention the customer's cloud-event entitlement
    promises. A billing v2 Hybrid account has no legacy plans row, so the
    legacy lookup alone never expired its media. Order: an explicit RDM
    override (7/14/30, event_media_policy.cloud_policy_for_customer), else
    the v2 entitlement's cloud_event_retention_days (kept after the plan
    ends, so media of a cancelled Hybrid plan still expires), else the
    legacy plan exactly as before."""
    try:
        import event_media_policy
        override = event_media_policy.cloud_policy_for_customer(db, customer_id).get("retention_days")
        if override in event_media_policy.MOTION_RETENTION_DAYS:
            return int(override)
        import per_camera_billing
        v2 = per_camera_billing.entitlement_for_customer(customer_id)
        if v2 and v2.get("cloud_event_retention_days"):
            return int(v2["cloud_event_retention_days"])
    except Exception:
        logger.exception("recording_retention.event_media_policy_unavailable customer_id=%s", customer_id)
    return _customer_retention_days(db, customer_id)


def event_media_retention_days(db, customer_id: str) -> int | None:
    """Public form of the sweep's own event-media retention, so an upload is
    authorized only for media this sweep will later expire."""
    return _event_media_retention_days(db, customer_id)


FULL_VMS_POLICY = "full_vms_plan"
CLOUD_GATEWAY_POLICY = "cloud_gateway"
GATEWAY_RETENTION_OPTIONS = (7, 14, 30)
# Consecutive ticks whose Cloud Gateway binding lookup failed before the
# sweep raises its log level to CRITICAL (historical rows are held).
LOOKUP_FAILURE_ALERT_TICKS = 3


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _first_gateway_binding_times(db) -> dict:
    """{camera_id: earliest Cloud Gateway binding created_at} over every
    binding row, any status. Raises if the lookup is unavailable."""
    rows = db.execute("SELECT camera_id, MIN(created_at) AS first_at FROM cloud_gateway_camera_entitlements GROUP BY camera_id").fetchall()
    return {row["camera_id"]: row["first_at"] for row in rows}


def _new_diagnostics() -> dict:
    return {
        "attributed_full_vms": 0, "attributed_cloud_gateway": 0,
        "historical_full_vms": 0,
        # Held = never deleted this tick, with the reason:
        "held_historical_ambiguous": 0,  # cataloged on/after the camera's first Gateway binding
        "held_historical_lookup_unavailable": 0,  # binding lookup failed
        "held_invalid_attribution": 0,  # inconsistent or out-of-range stored policy
        "held_no_retention": 0,  # Full VMS customer with no retention on file
        "held_bad_timestamp": 0,
        "gateway_lookup_error": None,
    }


def _media_retention_days(db, row, first_binding, full_vms_cache, diagnostics):
    """The retention that governs one ROOT event-media row, or None to keep
    it. Decided by the policy recorded on the row when it was cataloged
    (capture time) -- never by the camera's current binding, edition or
    subscription, and never by a later Gateway retention change:
    - 'cloud_gateway': the 7/14/30 days frozen on the row;
    - 'full_vms_plan': the existing Full VMS chain, exactly as before;
    - NULL (cataloged before attribution existed): Full VMS only when the
      server recorded it strictly before the camera's first Cloud Gateway
      binding (or the camera never had one). Anything else -- on/after a
      binding, unparseable times, or the lookup unavailable -- is held.
    """
    policy, stored_days = row["retention_policy"], row["retention_days"]
    if policy == CLOUD_GATEWAY_POLICY:
        if isinstance(stored_days, bool) or not isinstance(stored_days, int) or stored_days not in GATEWAY_RETENTION_OPTIONS:
            diagnostics["held_invalid_attribution"] += 1
            return None
        diagnostics["attributed_cloud_gateway"] += 1
        return stored_days
    if policy == FULL_VMS_POLICY:
        if stored_days is not None:
            diagnostics["held_invalid_attribution"] += 1
            return None
        diagnostics["attributed_full_vms"] += 1
    elif policy is None and stored_days is None:
        if first_binding is None:
            diagnostics["held_historical_lookup_unavailable"] += 1
            return None
        first_at = first_binding.get(row["camera_id"])
        if first_at is not None:
            cataloged, bound = _parse_time(row["created_at"]), _parse_time(first_at)
            if cataloged is None or bound is None or not cataloged < bound:
                diagnostics["held_historical_ambiguous"] += 1
                return None
        diagnostics["historical_full_vms"] += 1
    else:
        diagnostics["held_invalid_attribution"] += 1
        return None
    customer_id = row["customer_id"]
    if customer_id not in full_vms_cache:
        full_vms_cache[customer_id] = _event_media_retention_days(db, customer_id)
    if full_vms_cache[customer_id] is None:
        diagnostics["held_no_retention"] += 1
    return full_vms_cache[customer_id]


def _expired_candidates(db, now: datetime, diagnostics: dict | None = None) -> list[dict]:
    """Every available recording whose customer has a real, on-file
    retention_days AND whose own started_at is older than that many
    days. Computed per-row in Python -- retention_days varies per
    customer, not expressible as a single static SQL WHERE clause --
    rather than a per-plan-tier UNION query.

    event_media rows follow the policy recorded at capture time (see
    _media_retention_days). The Cloud Gateway binding lookup is needed
    only for historical, unattributed rows: if it fails, only those rows
    are held; attributed Full VMS and Cloud Gateway rows keep expiring.

    event_media candidates are restricted to ROOT rows only
    (source_media_id IS NULL) -- see db_migrations.py's own comment on
    that column. A shared row (a correlated Smart Motion event
    referencing its base Motion event's own uploaded clip) is a
    reference, never an owner, of its S3 object: it must never
    independently select itself for deletion, and must never trigger
    its own S3 delete_object call. Its lifetime is tied to its root's
    -- see run_retention_sweep_tick() below, which deletes every row
    sharing a deleted root's own id in the SAME pass, only after that
    root's own S3 object is confirmed deleted."""
    diagnostics = _new_diagnostics() if diagnostics is None else diagnostics
    candidates = []
    recordings = db.execute("SELECT id, customer_id, s3_key, started_at, 'recording' AS kind FROM recordings WHERE status='available'").fetchall()
    media = db.execute("SELECT id, customer_id, camera_id, s3_key, started_at, created_at, retention_policy, retention_days, "
                       "'event_media' AS kind FROM detection_event_media WHERE source_media_id IS NULL").fetchall()
    first_binding = None
    if any(row["retention_policy"] is None for row in media):
        try:
            first_binding = _first_gateway_binding_times(db)
        except Exception as error:
            diagnostics["gateway_lookup_error"] = f"{type(error).__name__}: {error}"[:300]
            logger.exception("recording_retention.gateway_binding_lookup_failed")
    full_vms_cache: dict = {}
    for row in list(recordings) + list(media):
        if row["kind"] == "event_media":
            retention_days = _media_retention_days(db, row, first_binding, full_vms_cache, diagnostics)
        else:  # continuous recordings: the legacy plan, unchanged
            retention_days = _customer_retention_days(db, row["customer_id"])
        if retention_days is None:
            continue
        try:
            started_at = datetime.fromisoformat(row["started_at"])
        except (TypeError, ValueError):
            diagnostics["held_bad_timestamp"] += 1
            continue  # unparseable timestamp -- never treated as expired
        if now - started_at > timedelta(days=retention_days):
            candidates.append({key: row[key] for key in ("id", "customer_id", "s3_key", "started_at", "kind")})
    return candidates


def _lifecycle_s3():
    """(s3 client, bucket) through the dedicated delete-only STS role, or
    None when unconfigured -- see docs/r5-recording-lifecycle-iam.md."""
    role_arn = os.environ.get("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "").strip()
    bucket = os.environ.get("ANYAICAM_RECORDING_S3_BUCKET", "").strip()
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
    if not role_arn or not bucket or not region:
        return None
    try:
        import boto3
    except ImportError:
        return None
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
    return s3, bucket


def _delete_recording_object(s3_key: str) -> bool:
    """Deletes one S3 object via a dedicated, delete-only STS role.
    Fails closed (returns False, deletes nothing) whenever
    unconfigured -- see docs/r5-recording-lifecycle-iam.md."""
    try:
        client = _lifecycle_s3()
        if client is None:
            return False
        s3, bucket = client
        s3.delete_object(Bucket=bucket, Key=s3_key)
        return True
    except Exception:
        logger.exception("recording_retention.delete_failed")
        return False


# ---------------------------------------------------------------- health
# A configuration flag is not evidence that deletion works (Codex review
# of 9ba528b). The worker records, in the database, a deletion probe that
# really assumed the lifecycle role and called DeleteObject, and each
# successful tick. Upload guards read that record, so every web process
# sees the same evidence.
HEALTH_TABLE = "recording_retention_sweep_health"
PROBE_PREFIX = "recordings/_retention_probe/"
PROBE_MAX_AGE = timedelta(hours=26)
PROBE_INTERVAL = timedelta(hours=24)


def worker_id() -> str:
    import re
    import socket
    return re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())[:64] or "unknown-host"


def probe_key(worker: str) -> str:
    """A key under the recordings/ prefix the delete-only policy covers.
    Nothing is ever uploaded there, so the probe deletes no customer data;
    S3 answers DeleteObject on an absent key with success only when the
    role, bucket, region and permission all work."""
    return f"{PROBE_PREFIX}{worker}.probe"


def run_deletion_probe(worker: str) -> tuple:
    try:
        client = _lifecycle_s3()
        if client is None:
            return False, "lifecycle role, bucket or region not configured"
        s3, bucket = client
        s3.delete_object(Bucket=bucket, Key=probe_key(worker))
        return True, None
    except Exception as error:
        logger.exception("recording_retention.deletion_probe_failed")
        return False, f"deletion probe failed: {type(error).__name__}"


def tick_max_age() -> timedelta:
    return timedelta(seconds=2 * RETENTION_SWEEP_INTERVAL_SECONDS + 600)


def record_health(worker: str, *, now: datetime | None = None, started: bool = False, probe_ok: bool | None = None,
                  tick_ok: bool | None = None, error: str | None = None, diagnostics: dict | None = None) -> None:
    """Best effort: a failure here is logged, never raised into the worker."""
    import json
    stamp = (now or datetime.now()).isoformat()
    try:
        from partner_db import transaction
        with transaction() as db:
            row = db.execute(f"SELECT worker_id FROM {HEALTH_TABLE} WHERE worker_id=?", (worker,)).fetchone()
            if row is None or started:
                db.execute(f"DELETE FROM {HEALTH_TABLE} WHERE worker_id=?", (worker,))
                db.execute(f"INSERT INTO {HEALTH_TABLE}(worker_id,started_at,updated_at) VALUES(?,?,?)", (worker, stamp, stamp))
            if probe_ok is True:
                db.execute(f"UPDATE {HEALTH_TABLE} SET last_probe_ok_at=? WHERE worker_id=?", (stamp, worker))
            elif probe_ok is False:
                db.execute(f"UPDATE {HEALTH_TABLE} SET last_probe_ok_at=NULL WHERE worker_id=?", (worker,))
            if tick_ok is True:
                db.execute(f"UPDATE {HEALTH_TABLE} SET last_tick_ok_at=? WHERE worker_id=?", (stamp, worker))
            if error is not None:
                db.execute(f"UPDATE {HEALTH_TABLE} SET last_error=?,last_error_at=? WHERE worker_id=?", (error[:300], stamp, worker))
            if diagnostics is not None:
                db.execute(f"UPDATE {HEALTH_TABLE} SET diagnostics_json=? WHERE worker_id=?",
                           (json.dumps(diagnostics, sort_keys=True, default=str), worker))
            db.execute(f"UPDATE {HEALTH_TABLE} SET updated_at=? WHERE worker_id=?", (stamp, worker))
    except Exception:
        logger.exception("recording_retention.health_record_failed")


def operational_readiness(db, now: datetime | None = None) -> dict:
    """Configuration (readiness()) AND evidence that a worker is really
    deleting: some worker with a successful deletion probe within
    PROBE_MAX_AGE and a successful tick within tick_max_age(). Read only;
    no AWS call. Any error means not ready."""
    now = now or datetime.now()
    result = readiness()
    missing = list(result["missing"])
    try:
        rows = db.execute(f"SELECT worker_id,last_probe_ok_at,last_tick_ok_at FROM {HEALTH_TABLE}").fetchall()
    except Exception:
        logger.exception("recording_retention.health_unavailable")
        rows = None
    healthy = None
    if rows is None:
        missing.append("retention worker health record unavailable")
    else:
        for row in rows:
            probe, tick = _parse_time(row["last_probe_ok_at"]), _parse_time(row["last_tick_ok_at"])
            if probe and tick and timedelta(0) <= now - probe <= PROBE_MAX_AGE and timedelta(0) <= now - tick <= tick_max_age():
                healthy = row["worker_id"]
                break
        if healthy is None:
            missing.append("a retention worker with a successful deletion probe (last 26 h) and sweep tick "
                           f"(last {int(tick_max_age().total_seconds() // 60)} min)")
    return {"ready": not missing, "missing": missing, "worker_id": healthy}


def _report(diagnostics: dict) -> None:
    """Monitoring: held rows and lookup failures are never silent."""
    state = recording_retention_sweep_state
    if diagnostics.get("gateway_lookup_error"):
        state["gateway_lookup_failures_consecutive"] = state.get("gateway_lookup_failures_consecutive", 0) + 1
    else:
        state["gateway_lookup_failures_consecutive"] = 0
    state["last_diagnostics"] = diagnostics
    held = {key: value for key, value in diagnostics.items() if key.startswith("held_") and value}
    failures = state["gateway_lookup_failures_consecutive"]
    if failures >= LOOKUP_FAILURE_ALERT_TICKS:
        logger.critical("recording_retention.historical_media_expiry_blocked consecutive_lookup_failures=%s held=%s error=%s",
                        failures, held, diagnostics.get("gateway_lookup_error"))
    elif held:
        logger.warning("recording_retention.media_held held=%s lookup_error=%s", held, diagnostics.get("gateway_lookup_error"))


def run_retention_sweep_tick(now: datetime | None = None) -> dict:
    """One full sweep pass -- synchronous and directly callable so it's
    fully testable without asyncio. Each candidate's delete + catalog
    removal is its own short transaction, matching this project's
    existing per-item transaction granularity (see
    live_relay_idle_sweep.py's own run_idle_sweep_tick())."""
    now = now or datetime.now()
    diagnostics = _new_diagnostics()
    with connection() as db:
        candidates = _expired_candidates(db, now, diagnostics)

    deleted = 0
    delete_failures = 0
    for candidate in candidates:
        if not _delete_recording_object(candidate["s3_key"]):
            # S3 deletion not confirmed -- leave every DB record (this
            # root row, and any shared rows referencing it below)
            # completely untouched. The next tick retries from the same
            # state; a database record must never be removed ahead of
            # its own confirmed physical deletion.
            delete_failures += 1
            continue
        with connection() as db:
            table = "recordings" if candidate["kind"] == "recording" else "detection_event_media"
            if table == "detection_event_media":
                # This candidate is always a ROOT row (source_media_id
                # IS NULL, enforced by _expired_candidates()'s own
                # query) -- its S3 object is now confirmed gone, so
                # every row that shares it (a correlated Smart Motion
                # event's own reference) is deleted in this SAME
                # transaction. A shared row never gets its own
                # _delete_recording_object() call: it owns no S3 object
                # of its own to delete, only a reference to this one.
                db.execute("DELETE FROM detection_event_media WHERE source_media_id=?", (candidate["id"],))
            db.execute(f"DELETE FROM {table} WHERE id=?", (candidate["id"],))
        deleted += 1
        logger.info(
            "recording_retention.deleted recording_id=%s customer_id=%s",
            candidate["id"], candidate["customer_id"],
        )
    diagnostics["delete_failures"] = delete_failures
    recording_retention_sweep_state["last_deleted_count"] = deleted
    _report(diagnostics)
    return {"checked": len(candidates), "deleted": deleted}  # diagnostics: state["last_diagnostics"]


def readiness() -> dict:
    """Whether this host is CONFIGURED to enforce cloud retention (Hybrid's
    14-day event retention included) -- configuration only, no AWS call.
    Configuration alone does not prove deletion works; Cloud Gateway
    uploads require operational_readiness(). Everything listed in
    "missing" must be set on the cloud host; see
    docs/hybrid-event-retention-staging-checklist.md."""
    import re
    missing = []
    if RUNTIME_ROLE not in {"cloud", "combined"}:
        missing.append("ANYAICAM_RUNTIME_ROLE=cloud (or combined)")
    if not RETENTION_SWEEP_ENABLED:
        missing.append("ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=true")
    role_arn = os.environ.get("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "").strip()
    if not re.fullmatch(r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]+", role_arn):
        missing.append("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN (the delete-only IAM role)")
    if not os.environ.get("ANYAICAM_RECORDING_S3_BUCKET", "").strip():
        missing.append("ANYAICAM_RECORDING_S3_BUCKET")
    if not os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip():
        missing.append("AWS_REGION (or AWS_DEFAULT_REGION)")
    try:
        import boto3  # noqa: F401
    except ImportError:
        missing.append("boto3 installed in the image")
    return {"ready": not missing, "missing": missing}


def worker_iteration(worker: str, last_probe_at: datetime | None, now: datetime | None = None) -> datetime | None:
    """One worker step (synchronous, testable): re-probe when due, then
    sweep only after a successful probe. Returns the last probe time."""
    now = now or datetime.now()
    if last_probe_at is None or now - last_probe_at >= PROBE_INTERVAL:
        ok, error = run_deletion_probe(worker)
        record_health(worker, now=now, probe_ok=ok, error=error)
        if not ok:
            recording_retention_sweep_state["last_error"] = error
            logger.error("recording_retention.not_enforced reason=%s", error)
            return None
        last_probe_at = now
    try:
        run_retention_sweep_tick(now)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"[:300]
        recording_retention_sweep_state["last_error"] = message
        record_health(worker, now=now, error=message)
        logger.warning("recording_retention.worker_iteration_failed error=%s", message)
        return last_probe_at
    recording_retention_sweep_state["last_tick_at"] = now.isoformat()
    recording_retention_sweep_state["last_error"] = None
    record_health(worker, now=now, tick_ok=True, diagnostics=recording_retention_sweep_state.get("last_diagnostics"))
    return last_probe_at


async def recording_retention_sweep_worker() -> None:
    ready = readiness()
    recording_retention_sweep_state["readiness"] = ready
    if RUNTIME_ROLE in {"cloud", "combined"} and not ready["ready"]:
        # Retention is a customer promise (Hybrid: 14-day cloud events): say
        # loudly that this host is not enforcing it, and why.
        logger.warning("recording_retention.not_enforced missing=%s", "; ".join(ready["missing"]))
    if RUNTIME_ROLE not in {"cloud", "combined"} or not RETENTION_SWEEP_ENABLED:
        recording_retention_sweep_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    worker = worker_id()
    await asyncio.to_thread(record_health, worker, started=True)
    recording_retention_sweep_state["worker_status"] = "running"
    logger.info("recording_retention.worker_started worker_id=%s", worker)
    last_probe_at = None
    while True:
        try:
            last_probe_at = await asyncio.to_thread(worker_iteration, worker, last_probe_at)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            recording_retention_sweep_state["last_error"] = str(error)
            logger.warning("recording_retention.worker_iteration_failed error=%s", error)
        await asyncio.sleep(RETENTION_SWEEP_INTERVAL_SECONDS)


def diagnostics_report(now: datetime | None = None) -> dict:
    """Read-only operator view: configuration, worker health records and
    what the next sweep would hold (no deletion, no AWS call)."""
    import json
    now = now or datetime.now()
    with connection() as db:
        report = {"operational": operational_readiness(db, now)}
        try:
            report["workers"] = [dict(row) for row in db.execute(f"SELECT * FROM {HEALTH_TABLE}").fetchall()]
            for worker in report["workers"]:
                raw = worker.pop("diagnostics_json", None)
                if raw:
                    worker["diagnostics"] = json.loads(raw)
        except Exception as error:
            report["workers"] = f"unavailable: {type(error).__name__}"
        diagnostics = _new_diagnostics()
        report["would_delete"] = len(_expired_candidates(db, now, diagnostics))
        report["classification"] = diagnostics
    return report


if __name__ == "__main__":
    # Staging/production checks, no AWS call:
    #   python -m recording_retention_sweep --check        configuration only
    #   python -m recording_retention_sweep --diagnostics  health + held rows
    import json
    import sys
    if sys.argv[1:] == ["--check"]:
        result = readiness()
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["ready"] else 1)
    if sys.argv[1:] == ["--diagnostics"]:
        result = diagnostics_report()
        print(json.dumps(result, indent=2, default=str))
        raise SystemExit(0 if result["operational"]["ready"] else 1)
    print("usage: python -m recording_retention_sweep --check | --diagnostics")
    raise SystemExit(2)
