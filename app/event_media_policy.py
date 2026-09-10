"""Authoritative limits for low-cost motion-event cloud recording.

The server applies this policy before it accepts an event-media catalog row;
the appliance preflights it before uploading bytes.  Values intentionally stay
small and explicit while pricing/entitlements are not yet wired to billing.
"""
from datetime import datetime, timedelta

MOTION_RETENTION_DAYS = frozenset({7, 14, 30})
DEFAULT_DAILY_SECONDS = 6 * 60 * 60


def _current_plan(db, customer_id):
    return db.execute(
        "SELECT recording_mode,retention_days FROM plans WHERE customer_id=? "
        "ORDER BY created_at DESC LIMIT 1", (customer_id,)
    ).fetchone()


def motion_event_policy(db, customer_id: str) -> dict | None:
    plan = _current_plan(db, customer_id)
    if not plan or str(plan["recording_mode"] or "").strip().lower() != "motion":
        return None
    try:
        retention_days = int(plan["retention_days"])
    except (TypeError, ValueError):
        return None
    if retention_days not in MOTION_RETENTION_DAYS:
        return None
    return {"retention_days": retention_days, "daily_seconds": DEFAULT_DAILY_SECONDS}


def daily_seconds_used(db, camera_id: str, event_at: datetime) -> float:
    day_start = event_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    total = 0.0
    rows = db.execute(
        "SELECT dem.duration_seconds,de.event_timestamp FROM detection_event_media dem "
        "JOIN detection_events de ON de.id=dem.detection_event_id WHERE dem.camera_id=?",
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


def allows_event_media(db, customer_id: str, camera_id: str, event_at: datetime, duration_seconds: float) -> tuple[bool, str]:
    policy = motion_event_policy(db, customer_id)
    if not policy:
        return False, "An active 7, 14, or 30-day motion recording plan is required."
    if duration_seconds <= 0 or duration_seconds > policy["daily_seconds"]:
        return False, "Invalid event-media duration."
    if daily_seconds_used(db, camera_id, event_at) + duration_seconds > policy["daily_seconds"]:
        return False, "Daily motion event recording allowance exceeded."
    return True, ""
