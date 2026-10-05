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

Enforced where billing data lives: the cloud (and the combined single-host
runtime). An appliance holds no billing tables; the cloud controls what it is
sent instead -- Voice Call entrance cameras and the automatic alarm
talk-down switch are only sent for an entitled account (appliance_cloud.py).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("anyaicam.feature_entitlements")

TALK_DOWN = "talk_down"
VOICE_CALL = "voice_call"
NOT_ENTITLED_DETAIL = {
    TALK_DOWN: "Talk Down isn't part of this account's plan. It's included with AI Local and Hybrid, "
               "or can be added to Basic Local on My subscription.",
    VOICE_CALL: "AAC Voice Call isn't enabled for this account.",
}


def enforcement_applies() -> bool:
    role = os.environ.get("ANYAICAM_RUNTIME_ROLE")
    if role is None:
        try:
            from cloud_config import settings
            role = settings.runtime_role
        except Exception:
            role = "edge"
    return str(role).strip().lower() in ("cloud", "combined")


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
    """True where enforcement does not apply (an appliance), else the entitlement."""
    return (not enforcement_applies()) or customer_entitled(customer_id, feature)


def require(customer_id: str | None, feature: str) -> None:
    """HTTP 403 when the account is not entitled (cloud/combined only)."""
    if not allowed(customer_id, feature):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=NOT_ENTITLED_DETAIL[feature])
