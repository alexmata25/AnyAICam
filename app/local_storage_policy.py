"""Local recording storage management: RDM-configurable thresholds and
their system defaults, mirroring event_media_policy.py's cloud_policy_for_
customer() pattern exactly (a customer_id-keyed override table, NULL
meaning "no override, use the system default", never a second silently-
applied default hiding somewhere else).

Two distinct percentages, both measured as "percent of the recordings
filesystem that is FREE" (not used):
  - reserved_free_percent: the floor. Once free space drops to or below
    this, automatic cleanup deletes the oldest eligible local recordings
    until free space is restored above it again. Default 10%, per the
    explicit product decision ("a safe default such as 10% free space").
  - warning_free_percent: the earlier, non-destructive line. Once free
    space drops to or below this (but is still above reserved_free_percent),
    the appliance's storage_state is 'warning' -- surfaced in RDM and
    eligible for a low-disk notification -- with no deletion yet. Default
    20%, twice the reserved floor, giving a real early-warning window
    before cleanup would ever need to run under normal recording rates.

Kept in its own small module, separate from local_storage_manager.py's
actual disk-scanning/deletion logic, the same separation of concerns
event_media_policy.py (defaults/resolution) has from event_media_uploader.py
(the actual upload worker)."""

DEFAULT_RESERVED_FREE_PERCENT = 10
DEFAULT_WARNING_FREE_PERCENT = 20


def local_storage_policy_for_customer(db, customer_id: str) -> dict:
    """The real, effective local-storage policy for this customer: an
    RDM-set local_storage_policy row's own values where explicitly set
    (NULL means "no override"), otherwise the system default -- unlike
    cloud_policy_for_customer(), these two values always resolve to a
    real, usable number (never None), since "how full is too full" must
    always have an answer for the cleanup worker to act on."""
    row = db.execute(
        "SELECT reserved_free_percent,warning_free_percent FROM local_storage_policy WHERE customer_id=?",
        (customer_id,),
    ).fetchone()
    reserved_free_percent = DEFAULT_RESERVED_FREE_PERCENT
    warning_free_percent = DEFAULT_WARNING_FREE_PERCENT
    if row:
        if row["reserved_free_percent"] is not None:
            reserved_free_percent = int(row["reserved_free_percent"])
        if row["warning_free_percent"] is not None:
            warning_free_percent = int(row["warning_free_percent"])
    return {"reserved_free_percent": reserved_free_percent, "warning_free_percent": warning_free_percent}
