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
import json
import uuid
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
MAX_CAMERA_SLOTS = 10_000
_IDENTITY_FIELDS = ("appliance_id", "cloud_id", "customer_id")


def _identity_transition_id() -> str:
    return uuid.uuid4().hex


def initial_capacity_identity_transition(source_appliance_id: str, appliance: dict) -> dict:
    """Cloud-signed bridge from factory UUID to the newly claimed cloud row.

    The caller is the single-use claim/activation completion transaction. The
    source is the installer-generated provisional device UUID. It is only a
    claim-correlation value; the target is the canonical cloud identity and
    becomes authoritative on the appliance only after signed capacity acceptance.
    """
    target = {"appliance_id": str(appliance.get("id") or ""),
              "cloud_id": str(appliance.get("cloud_id") or ""),
              "customer_id": str(appliance.get("customer_id") or "")}
    if not isinstance(source_appliance_id, str) or not source_appliance_id.strip() or not all(target.values()):
        raise ValueError("Cannot create a capacity identity transition from incomplete identity data.")
    return {"schema": 1, "transition_id": _identity_transition_id(), "kind": "initial_claim",
            "from_appliance_id": source_appliance_id, "to_identity": target}


def authorized_capacity_identity_transition(from_identity: dict, to_appliance: dict, *, reason: str) -> dict:
    """Build explicit admin-authorized transition metadata for cloud signing."""
    if reason not in {"reenrollment", "ownership_transfer"}:
        raise ValueError("Unsupported identity transition reason.")
    source = {field: from_identity.get(field) for field in _IDENTITY_FIELDS} if isinstance(from_identity, dict) else None
    target = {"appliance_id": str(to_appliance.get("id") or ""),
              "cloud_id": str(to_appliance.get("cloud_id") or ""),
              "customer_id": str(to_appliance.get("customer_id") or "")}
    if not source or any(not isinstance(value, str) or not value.strip() for value in source.values()) or not all(target.values()):
        raise ValueError("Identity transition must name complete source and target identities.")
    if source == target:
        raise ValueError("Identity transition source and target are identical.")
    return {"schema": 1, "transition_id": _identity_transition_id(), "kind": "authorized",
            "reason": reason, "from_identity": source, "to_identity": target,
            "authorized_at": datetime.now(timezone.utc).isoformat()}


def capacity_identity_transition_status(appliance: dict, target: dict) -> tuple[str, dict | None]:
    """Classify persisted identity-transition metadata without raising.

    A stale or malformed transition is a configuration readiness problem,
    not a reason to fail the whole authenticated appliance configuration
    request. Callers must not issue identity-bound entitlements from an
    invalid transition. ``approved`` means the transition is structurally
    valid and targets the appliance's current cloud identity.
    """
    raw = appliance.get("capacity_identity_transition_json")
    if raw is None:
        return "absent", None
    if isinstance(raw, str) and not raw.strip():
        return "invalid", None
    try:
        transition = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return "invalid", None
    if (not isinstance(transition, dict) or transition.get("schema") != 1 or
            not isinstance(transition.get("transition_id"), str) or
            not transition["transition_id"] or len(transition["transition_id"]) > 128 or
            not all(char.isalnum() or char in "._:-" for char in transition["transition_id"])):
        return "invalid", None
    transition_target = transition.get("to_identity")
    if transition_target != target:
        return "invalid", None
    kind = transition.get("kind")
    if kind == "initial_claim":
        source = transition.get("from_appliance_id")
        if not isinstance(source, str) or not source.strip() or len(source) > 256:
            return "invalid", None
    elif kind == "authorized":
        source = transition.get("from_identity")
        if (transition.get("reason") not in {"reenrollment", "ownership_transfer"} or
                not isinstance(source, dict) or set(source) != set(_IDENTITY_FIELDS) or
                any(not isinstance(source.get(field), str) or not source[field].strip()
                    for field in _IDENTITY_FIELDS) or source == target):
            return "invalid", None
        try:
            authorized_at = datetime.fromisoformat(str(transition.get("authorized_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return "invalid", None
        if authorized_at.tzinfo is None:
            return "invalid", None
    else:
        return "invalid", None
    return "approved", transition


def _capacity_identity_transition(appliance: dict, target: dict) -> dict | None:
    status, transition = capacity_identity_transition_status(appliance, target)
    if status == "invalid":
        raise ValueError("Stored capacity identity transition is malformed or stale.")
    return transition


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
    appliance's own customer.

    camera_capacity (2026-10-09, owner-approved Option A): the licensed camera
    limit -- customer_entitlements.capacity_breakdown(), the smaller of the
    camera plan and the VMS license -- signed with the features, so the
    appliance accepts a capacity only with a valid signature
    (vms_capacity.py). Same type and version: a 1.2.4 appliance ignores the
    added field."""
    import appliance_identity
    import customer_entitlements
    from partner_db import connection
    customer_id = appliance.get("customer_id")
    now = now or datetime.now(timezone.utc)
    breakdown = customer_entitlements.capacity_breakdown(customer_id) if customer_id else {}
    # The edge verifier and camera-numbering model support at most 10,000
    # slots. Clamp every signed component, not just the usable minimum, so a
    # large legacy license cannot make the entire signed snapshot invalid.
    camera_capacity = {field: min(MAX_CAMERA_SLOTS, max(0, int(breakdown.get(field) or 0)))
                       for field in ("camera_slot_quantity", "plan_camera_slots", "vms_license_capacity")}
    identity = {"appliance_id": str(appliance.get("id") or ""),
                "cloud_id": str(appliance.get("cloud_id") or ""),
                "customer_id": str(customer_id or "")}
    body = {
        "type": "anyaicam.feature_entitlements", "version": 1,
        **identity,
        "features": {TALK_DOWN: customer_entitled(customer_id, TALK_DOWN),
                     VOICE_CALL: customer_entitled(customer_id, VOICE_CALL)},
        "camera_capacity": camera_capacity,
        "issued_at": now.isoformat(), "expires_at": (now + snapshot_ttl()).isoformat(),
    }
    transition = _capacity_identity_transition(appliance, identity)
    if transition is not None:
        body["identity_transition"] = transition
    with connection() as db:
        key = appliance_identity.ensure_signing_key(db)
    return {**body, "signature": appliance_identity.sign_body(body, key_id=key["key_id"], private_key_b64=key["private_key_b64"])}
