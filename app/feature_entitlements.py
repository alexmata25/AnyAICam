"""Runtime enforcement of paid customer features (2026-10-05, Codex review of
41d2af4: billing distinguished Talk Down and AAC Voice Call, but the routes
and triggers that run them did not check either).

customer_entitled() is the one answer, from the same sources billing writes:
  * ordinary Talk Down -- included with an active AI Local / Hybrid plan, or a
    held Talk Down add-on (Basic Local, grandfathered legacy plans);
  * AAC Voice Call -- never from a plan; only a held package that grants it
    (today the Talk Down add-on) or an analytics grant recorded directly for
    the account (admin/partner provisioned, older purchases).
A cancelled, refunded or suspended plan or add-on no longer counts, because
billing marks it so. Any error reading entitlements denies (fail closed).

Where it is decided (allowed()), by runtime role:
  * cloud / combined -- from billing, as above;
  * edge (an appliance) -- from the signed entitlement snapshot the cloud
    sends with each configuration sync (appliance_entitlements.py); no
    valid snapshot means no paid feature;
  * anything else (an unrecognized role, or one that cannot be read) --
    denied.
The cloud still only sends Voice Call entrance cameras and the automatic
alarm talk-down switch to an entitled account (appliance_cloud.py), as a
second layer.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("anyaicam.feature_entitlements")

TALK_DOWN = "talk_down"
VOICE_CALL = "voice_call"
NOT_ENTITLED_DETAIL = {
    TALK_DOWN: "Talk Down isn't part of this account's plan. It's included with AI Local and Hybrid, "
               "or can be added to Basic Local on My subscription.",
    VOICE_CALL: "AAC Voice Call isn't enabled for this account.",
}


def runtime_role() -> str:
    """The configured role, or "" when it cannot be read (denies)."""
    role = os.environ.get("ANYAICAM_RUNTIME_ROLE")
    if role is None:
        try:
            from cloud_config import settings
            role = settings.runtime_role
        except Exception:
            return ""
    return str(role or "").strip().lower()


def enforcement_applies() -> bool:
    """True where billing is read directly (cloud / combined)."""
    return runtime_role() in ("cloud", "combined")


def customer_entitled(customer_id: str | None, feature: str) -> bool:
    if not customer_id:
        return False
    try:
        import analytics_entitlements
        from partner_db import connection
        with connection() as db:
            if analytics_entitlements.account_wide_feature_active(db, customer_id, feature):
                return True
        return feature in set(analytics_entitlements.get_active_analytics_for_customer(customer_id))
    except Exception:
        logger.exception("feature_entitlements.check_failed customer_id=%s feature=%s", customer_id, feature)
        return False


def allowed(customer_id: str | None, feature: str) -> bool:
    role = runtime_role()
    if role in ("cloud", "combined"):
        return customer_entitled(customer_id, feature)
    if role == "edge":
        import appliance_entitlements
        return appliance_entitlements.feature_allowed(feature, customer_id)
    logger.warning("feature_entitlements.unknown_runtime_role role=%r feature=%s", role, feature)
    return False


def require(customer_id: str | None, feature: str) -> None:
    """HTTP 403 when this runtime may not run the feature for the account."""
    if not allowed(customer_id, feature):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=NOT_ENTITLED_DETAIL[feature])


DEFAULT_SNAPSHOT_TTL_HOURS = 24


def snapshot_ttl() -> timedelta:
    """How long an appliance may keep using a snapshot without a fresh one
    (cloud outage): ANYAICAM_ENTITLEMENT_SNAPSHOT_TTL_HOURS, 1-168, default 24."""
    try:
        hours = int(os.environ.get("ANYAICAM_ENTITLEMENT_SNAPSHOT_TTL_HOURS", DEFAULT_SNAPSHOT_TTL_HOURS))
    except ValueError:
        hours = DEFAULT_SNAPSHOT_TTL_HOURS
    return timedelta(hours=min(168, max(1, hours)))


def appliance_snapshot(appliance: dict, *, now: datetime | None = None) -> dict:
    """The signed entitlement snapshot for one appliance (cloud side; sent
    with its configuration -- see appliance_entitlements.py). Only the paid
    runtime features the appliance runs, each decided from billing for the
    appliance's own customer."""
    import appliance_identity
    from partner_db import connection
    customer_id = appliance.get("customer_id")
    now = now or datetime.now(timezone.utc)
    body = {
        "type": "anyaicam.feature_entitlements", "version": 1,
        "appliance_id": str(appliance.get("id") or ""), "cloud_id": str(appliance.get("cloud_id") or ""),
        "customer_id": str(customer_id or ""),
        "features": {TALK_DOWN: customer_entitled(customer_id, TALK_DOWN),
                     VOICE_CALL: customer_entitled(customer_id, VOICE_CALL)},
        "issued_at": now.isoformat(), "expires_at": (now + snapshot_ttl()).isoformat(),
    }
    with connection() as db:
        key = appliance_identity.ensure_signing_key(db)
    return {**body, "signature": appliance_identity.sign_body(body, key_id=key["key_id"], private_key_b64=key["private_key_b64"])}
