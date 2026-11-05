"""Audited designation of existing appliances as Full VMS. Never runs on its own.

Cloud media credentials require an explicit appliances.gateway_edition marker
(see appliance_cloud._require_gateway_media_authorization). Existing Full VMS
appliances predate the column and are NULL, so they must be designated before
that code serves traffic. This module never converts an appliance it cannot
establish as Full VMS from server records alone:

1. build_plan(): read-only. An appliance is eligible only if its marker is
   NULL, it has no Gateway evidence of any kind (server_gateway_edition.
   GATEWAY_EVIDENCE), the server created it strictly before the operator's
   cutoff (the first moment any Gateway-capable server or installer existed),
   and it was not created by the device claim flow, whose edition the server
   cannot know. Everything else is listed for review, never converted.
2. A person reviews the plan. Its SHA-256 identifies exactly what was reviewed.
3. apply_plan(): inside one caller-owned transaction, a live global
   administrator re-verifies every eligible appliance, sets full_vms only
   where the marker is still NULL, and writes one audit row per appliance in
   the same transaction. Any change since planning aborts the whole apply.

designate_full_vms() handles a single reviewed appliance (for example a
claim-activated Full VMS) with a recorded evidence reference.
"""
import hashlib
import json
from datetime import datetime, timezone

from server_gateway_edition import FULL_VMS, edition_records

PLAN_KIND = "full_vms_edition_backfill"
PLAN_VERSION = 1


class BackfillRefused(Exception):
    pass


def database_identity():
    """A non-secret fingerprint of the target database (its URL may hold a password)."""
    from database_backend import target_key
    return hashlib.sha256(repr(target_key()).encode()).hexdigest()[:16]


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _canonical(plan):
    return json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()


def plan_sha256(plan):
    return hashlib.sha256(_canonical(plan)).hexdigest()


def _classify(db, appliance, cutoff):
    """('eligible' | 'review' | 'recorded', reasons) from server records only."""
    marker, editions, any_gateway = edition_records(db, appliance["id"])
    if marker == FULL_VMS and any_gateway:
        return "review", ["CONFLICT: recorded full_vms but Gateway evidence exists (cloud media is refused)"]
    if marker is not None:
        return "recorded", [f"edition already recorded: {marker}"]
    reasons = []
    if any_gateway:
        reasons.append("Gateway evidence present: " + (", ".join(sorted(editions)) or "unspecified edition"))
    created = _timestamp(appliance.get("created_at"))
    if created is None:
        reasons.append("created_at is missing or invalid")
    elif (created.tzinfo is None) != (cutoff.tzinfo is None):
        reasons.append("created_at and cutoff differ in time-zone form")
    elif created >= cutoff:
        reasons.append("created on or after the cutoff")
    if appliance.get("activated_via") == "claim":
        reasons.append("created by the device claim flow; the server cannot know its edition")
    if not appliance.get("customer_id"):
        reasons.append("no customer")
    return ("review" if reasons else "eligible"), reasons


def build_plan(db, *, created_before):
    """Read-only plan. Returns (plan, sha256)."""
    cutoff = _timestamp(created_before)
    if cutoff is None:
        raise BackfillRefused("created_before must be an ISO-8601 timestamp.")
    eligible, review, recorded = [], [], {}
    for appliance in db.execute("SELECT * FROM appliances ORDER BY id").fetchall():
        appliance = dict(appliance)
        status, reasons = _classify(db, appliance, cutoff)
        item = {"appliance_id": appliance["id"], "customer_id": appliance.get("customer_id"),
                "created_at": appliance.get("created_at"), "activated_via": appliance.get("activated_via")}
        if status == "eligible":
            eligible.append(item)
        elif status == "review":
            review.append({**item, "reasons": reasons})
        else:
            marker = str(appliance.get("gateway_edition"))
            recorded[marker] = recorded.get(marker, 0) + 1
    plan = {"kind": PLAN_KIND, "version": PLAN_VERSION, "database": database_identity(),
            "created_before": created_before, "generated_at": datetime.now(timezone.utc).isoformat(),
            "eligible": eligible, "review": review, "already_recorded": recorded}
    return plan, plan_sha256(plan)


def readiness(db):
    """Deployment gate: appliances still without a marker (refused cloud media)."""
    counts = {}
    for item in db.execute("SELECT gateway_edition FROM appliances").fetchall():
        marker = item["gateway_edition"]
        counts[str(marker)] = counts.get(str(marker), 0) + 1
    return {"by_marker": counts, "unmarked": counts.get("None", 0)}


def _require_global_admin(db, actor):
    from appliance_identity import has_global_administrator_grant
    email = str((actor or {}).get("email") or "").strip()
    if not email or (actor or {}).get("role") != "administrator" or not has_global_administrator_grant(db, email=email):
        raise BackfillRefused("A live global administrator is required.")


def _designate(db, actor, appliance_id, details):
    changed = db.execute("UPDATE appliances SET gateway_edition=? WHERE id=? AND gateway_edition IS NULL",
                         (FULL_VMS, appliance_id)).rowcount
    if changed != 1:
        raise BackfillRefused(f"Appliance changed since review: {appliance_id}")
    db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
               "VALUES(?,?,?,?,?,?,?)",
               (actor["email"], actor["role"], "appliance.edition_designated", "appliance", appliance_id,
                json.dumps(details, sort_keys=True), datetime.now(timezone.utc).isoformat()))


def apply_plan(db, plan, expected_sha256, *, actor):
    """Designate the reviewed plan's eligible appliances; caller owns the transaction."""
    if not isinstance(plan, dict) or plan.get("kind") != PLAN_KIND or plan.get("version") != PLAN_VERSION:
        raise BackfillRefused("Not a Full VMS backfill plan.")
    if plan_sha256(plan) != expected_sha256:
        raise BackfillRefused("The plan does not match its reviewed SHA-256.")
    if plan.get("database") != database_identity():
        raise BackfillRefused("The plan was made for a different database.")
    _require_global_admin(db, actor)
    cutoff = _timestamp(plan.get("created_before"))
    if cutoff is None:
        raise BackfillRefused("The plan has no valid cutoff.")
    for item in plan.get("eligible") or []:
        appliance = db.execute("SELECT * FROM appliances WHERE id=?", (item.get("appliance_id"),)).fetchone()
        if appliance is None:
            raise BackfillRefused(f"Appliance no longer exists: {item.get('appliance_id')}")
        appliance = dict(appliance)
        if (appliance.get("customer_id"), appliance.get("created_at")) != (item.get("customer_id"), item.get("created_at")):
            raise BackfillRefused(f"Appliance changed since review: {appliance['id']}")
        status, reasons = _classify(db, appliance, cutoff)
        if status != "eligible":
            raise BackfillRefused(f"Appliance is no longer eligible: {appliance['id']} ({'; '.join(reasons)})")
        _designate(db, actor, appliance["id"], {"edition": FULL_VMS, "basis": "legacy_backfill",
                                                "plan_sha256": expected_sha256,
                                                "created_before": plan["created_before"]})
    return len(plan.get("eligible") or [])


def designate_full_vms(db, actor, *, appliance_id, customer_id, evidence_reference):
    """Designate one reviewed appliance; caller owns the transaction."""
    _require_global_admin(db, actor)
    if not isinstance(evidence_reference, str) or not evidence_reference.strip():
        raise BackfillRefused("An evidence reference is required.")
    appliance = db.execute("SELECT * FROM appliances WHERE id=? AND customer_id=?", (appliance_id, customer_id)).fetchone()
    if appliance is None:
        raise BackfillRefused("Appliance not found for this customer.")
    marker, editions, any_gateway = edition_records(db, appliance_id)
    if marker is not None:
        raise BackfillRefused(f"Edition already recorded: {marker}")
    if any_gateway:
        raise BackfillRefused("The appliance has Gateway evidence; it is never designated Full VMS.")
    _designate(db, actor, appliance_id, {"edition": FULL_VMS, "basis": "reviewed_designation",
                                         "evidence_reference": evidence_reference.strip()})
