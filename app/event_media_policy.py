"""Authoritative limits for low-cost motion-event cloud recording, and
the RDM-controlled cloud cost-control policy (customer_cloud_policy) --
see cloud_policy_for_customer(), the real link: an RDM administrator's
own explicit override, set via POST /api/admin/customers/{id}/cloud-
policy in appliance_cloud.py, is the one authoritative source for both
real cost-control levers, retention_days (7/14/30, the real S3
lifecycle-expiration window, applies to every customer's cloud media
regardless of recording mode) and daily_cloud_seconds. Generic across
every customer_id -- nothing here is specific to any one appliance,
customer, or test account.

Product-architecture decision (2026-09-16): Hybrid ('motion') cloud
recording is real, intelligent EVENT CLIPS + thumbnails only -- see
event_media_uploader.py's own cloud_recording_mode=='motion' gate and
its environmental-motion filtering. It never uploads continuous
recording segments to cloud at all, so daily_cloud_seconds (a ceiling
on recording_uploader.py's own continuous-segment upload volume) has
NO active role for Hybrid -- there is nothing for it to cap. It
remains available, opt-in only (no default cap applied to anyone), as
a possible future ceiling for the Continuous/Cloud tier, whose own
product purpose is unlimited (customer-paid-for) continuous cloud
recording -- defaulting that tier to a 6-hour cap would defeat its own
value proposition, so the system default is None (no cap) unless an
administrator explicitly sets one for a specific customer.

The server applies this policy before it accepts an event-media catalog row;
the appliance preflights it before uploading bytes.
"""
from datetime import datetime, timedelta

MOTION_RETENTION_DAYS = frozenset({7, 14, 30})
# Retained only as the historical/previous default value -- no longer
# applied anywhere automatically; see the module docstring above.
DEFAULT_DAILY_SECONDS = 6 * 60 * 60


def cloud_policy_for_customer(db, customer_id: str) -> dict:
    """The real, effective cloud-cost policy for this customer: an
    RDM-set customer_cloud_policy row's own values where explicitly
    set (NULL means "no override"), otherwise no cap/no retention
    override at all (None) -- never a silently-applied default. Never
    special-cased per customer_id -- the exact same lookup for every
    customer, real or test."""
    row = db.execute(
        "SELECT daily_cloud_seconds,retention_days FROM customer_cloud_policy WHERE customer_id=?",
        (customer_id,),
    ).fetchone()
    daily_cloud_seconds = None
    retention_days = None
    if row:
        if row["daily_cloud_seconds"] is not None:
            daily_cloud_seconds = int(row["daily_cloud_seconds"])
        if row["retention_days"] is not None:
            retention_days = int(row["retention_days"])
    return {"daily_cloud_seconds": daily_cloud_seconds, "retention_days": retention_days}


def _current_plan(db, customer_id):
    return db.execute(
        "SELECT recording_mode,retention_days FROM plans WHERE customer_id=? "
        "ORDER BY created_at DESC LIMIT 1", (customer_id,)
    ).fetchone()


def motion_event_policy(db, customer_id: str) -> dict | None:
    """Whether this customer is authorized to have event-media (short
    motion clips + thumbnails) accepted into the cloud catalog at all,
    and their retention_days. cloud_policy_for_customer()'s own RDM
    override takes precedence for retention_days when set; the legacy
    plans table (the pre-RDM, partner-quoted source) remains the
    fallback for a customer RDM has never explicitly configured, so an
    already-working real customer's existing behavior is unaffected
    until an administrator explicitly sets a customer_cloud_policy row
    for them.

    2026-09-16: no longer returns/enforces a daily_seconds ceiling here
    -- event clips and thumbnails are explicitly exempt from the 6-hour
    Hybrid cloud-upload allowance (that allowance applies only to the
    larger continuous/motion-correlated recording segments, enforced
    separately in recording_uploader.py on the appliance) and must keep
    working normally even after that allowance is reached."""
    override = cloud_policy_for_customer(db, customer_id)
    if override["retention_days"] is not None:
        if override["retention_days"] not in MOTION_RETENTION_DAYS:
            return None
        return {"retention_days": override["retention_days"]}
    plan = _current_plan(db, customer_id)
    if not plan or str(plan["recording_mode"] or "").strip().lower() != "motion":
        return None
    try:
        retention_days = int(plan["retention_days"])
    except (TypeError, ValueError):
        return None
    if retention_days not in MOTION_RETENTION_DAYS:
        return None
    return {"retention_days": retention_days}


def daily_seconds_used(db, camera_id: str, event_at: datetime) -> float:
    day_start = event_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    total = 0.0
    # source_media_id IS NULL: count only ROOT media rows -- a shared
    # row (a correlated Smart Motion event referencing its base Motion
    # event's own already-uploaded clip, see appliance_cloud.py's
    # analytics_event_media_shared()) references physical footage
    # already counted once via its root, and must never be charged
    # again for bytes that were never re-recorded or re-uploaded.
    rows = db.execute(
        "SELECT dem.duration_seconds,de.event_timestamp FROM detection_event_media dem "
        "JOIN detection_events de ON de.id=dem.detection_event_id "
        "WHERE dem.camera_id=? AND dem.source_media_id IS NULL",
        (camera_id,),
    ).fetchall()
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row["event_timestamp"]).replace("Z", "+00:00")).replace(tzinfo=None)
            if day_start <= timestamp < day_end:
                total += max(0.0, float(row["duration_seconds"] or 0))
        except (TypeError, ValueError):
            continue
    return total


# A single event clip's own sanity ceiling -- catches corrupt/bogus
# duration data (never a real short motion clip), completely unrelated
# to the Hybrid 6-hour continuous-segment allowance. Generous on
# purpose: event clips/thumbnails are explicitly exempt from any daily
# cloud-upload allowance (see motion_event_policy()'s own docstring)
# and must keep being accepted no matter how many a busy day produces.
_MAX_SINGLE_CLIP_SECONDS = 3600


def allows_event_media(db, customer_id: str, camera_id: str, event_at: datetime, duration_seconds: float) -> tuple[bool, str]:
    policy = motion_event_policy(db, customer_id)
    if not policy:
        return False, "An active 7, 14, or 30-day motion recording plan is required."
    if duration_seconds <= 0 or duration_seconds > _MAX_SINGLE_CLIP_SECONDS:
        return False, "Invalid event-media duration."
    return True, ""
