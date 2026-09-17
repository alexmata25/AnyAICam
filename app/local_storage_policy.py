"""Local recording storage management: RDM-configurable thresholds and
their system defaults, mirroring event_media_policy.py's cloud_policy_for_
customer() pattern exactly (a customer_id-keyed override table, NULL
meaning "no override, use the system default", never a second silently-
applied default hiding somewhere else).

Three independent knobs, all resolved the same customer_id-keyed-
override-else-system-default way:
  - reserved_free_percent: the free-space floor. Once free space drops to
    or below this, automatic cleanup deletes the oldest eligible local
    recordings until free space is restored above it again. Default 10%,
    per the explicit product decision ("a safe default such as 10% free
    space").
  - warning_free_percent: the earlier, non-destructive line. Once free
    space drops to or below this (but is still above reserved_free_percent),
    the appliance's storage_state is 'warning' -- surfaced in RDM and
    eligible for a low-disk notification -- with no deletion yet. Default
    20%, twice the reserved floor, giving a real early-warning window
    before cleanup would ever need to run under normal recording rates.
  - local_retention_days (2026-09-17): a SEPARATE, independent trigger
    from either percentage above -- once a local recording is older than
    this many days, it becomes eligible for deletion regardless of how
    much free space remains. Explicitly NOT the same knob as the
    existing Hybrid AWS/S3 retention entitlement (customer_cloud_policy.
    retention_days / event_media_policy.MOTION_RETENTION_DAYS, which
    governs how long an uploaded CLOUD event clip/thumbnail is kept) --
    this one only ever touches local disk. Default None (no age-based
    limit at all -- only the free-space reserve trigger applies) since,
    unlike the two percentages above, "how many days of local footage a
    customer wants kept" has no single safe system-wide number; it is
    set per customer/appliance via RDM (e.g. 7 for a disk-constrained lab
    appliance) based on real disk capacity and customer preference.
    local_storage_manager.py's run_cleanup_pass() treats the two
    triggers as independent and additive: whichever one first requires
    deleting a given candidate (oldest-first) is the one that does, and
    a recording already past local_retention_days is deleted even while
    free space is otherwise healthy.

Kept in its own small module, separate from local_storage_manager.py's
actual disk-scanning/deletion logic, the same separation of concerns
event_media_policy.py (defaults/resolution) has from event_media_uploader.py
(the actual upload worker)."""

DEFAULT_RESERVED_FREE_PERCENT = 10
DEFAULT_WARNING_FREE_PERCENT = 20
DEFAULT_LOCAL_RETENTION_DAYS = None


def local_storage_policy_for_customer(db, customer_id: str) -> dict:
    """The real, effective local-storage policy for this customer: an
    RDM-set local_storage_policy row's own values where explicitly set
    (NULL means "no override"), otherwise the system default.
    reserved_free_percent/warning_free_percent always resolve to a real,
    usable number (never None) -- unlike cloud_policy_for_customer(),
    "how full is too full" must always have an answer. local_retention_days
    stays None unless an RDM override explicitly sets it -- "no age-based
    limit" is a real, valid, common state (most customers), not an
    unresolved default."""
    row = db.execute(
        "SELECT reserved_free_percent,warning_free_percent,local_retention_days FROM local_storage_policy WHERE customer_id=?",
        (customer_id,),
    ).fetchone()
    reserved_free_percent = DEFAULT_RESERVED_FREE_PERCENT
    warning_free_percent = DEFAULT_WARNING_FREE_PERCENT
    local_retention_days = DEFAULT_LOCAL_RETENTION_DAYS
    if row:
        if row["reserved_free_percent"] is not None:
            reserved_free_percent = int(row["reserved_free_percent"])
        if row["warning_free_percent"] is not None:
            warning_free_percent = int(row["warning_free_percent"])
        if row["local_retention_days"] is not None:
            local_retention_days = int(row["local_retention_days"])
    return {"reserved_free_percent": reserved_free_percent, "warning_free_percent": warning_free_percent, "local_retention_days": local_retention_days}
