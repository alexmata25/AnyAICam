"""Record a claim-activated appliance's edition from verified paid entitlements (owner, 2026-11-10).

A successful device claim proves possession of the device, never which edition was bought, so completing a claim
records nothing (appliance_claims.claim_complete). Cloud media stays refused for a NULL marker
(appliance_cloud._require_gateway_media_authorization) until this module, or a reviewed designation
(full_vms_edition_backfill), records one.

An appliance is recorded full_vms here only when every check passes on server records alone:

1. it was created by the claim flow, belongs to a customer and has no recorded edition;
2. it has no Gateway evidence of any kind (server_gateway_edition.edition_records), and its customer has no Gateway
   activity of any kind (checkout attempts, entitlements, subscriptions, provisioning or claim recovery). A customer
   with Gateway activity may own both editions and the device does not say which one it is, so those appliances wait
   for a reviewed designation;
3. the customer holds a verified PAID Full VMS camera plan: an active camera_slots_local / camera_slots_hybrid
   entitlement carrying a Stripe subscription or checkout reference, or an active billing-v2 per-camera plan carrying
   a Stripe subscription;
4. and a verified PAID VMS software licence: an active vms_license entitlement carrying a Stripe reference, or a paid
   AnyAiCam appliance order whose recorded payment intent is not refunded or disputed, or the licence an active
   billing-v2 plan carries while its purchase is not reversed;
5. and the resulting usable camera capacity is above zero.

Nothing here contacts Stripe or any other provider: "verified" means the server's own webhook-maintained records.
Gateway editions are never recorded here; they need their own verified evidence (gateway_edition_evidence). Any error
leaves the appliance unrecorded (fail closed). The write and its audit row share one transaction and only ever change
a NULL marker.
"""
import json
import logging
from datetime import datetime, timezone

from server_gateway_edition import FULL_VMS, EditionUnverifiable, edition_records

logger = logging.getLogger(__name__)

ACTOR = {"email": "system:claim-edition-registration", "role": "system"}
BASE_PLAN_PRODUCTS = ("camera_slots_local", "camera_slots_hybrid")
# Every customer-level record of Gateway involvement (server_gateway_edition.GATEWAY_EVIDENCE has the per-appliance
# view; gateway_stable_installation_claim_ids has no customer column and is covered per appliance).
CUSTOMER_GATEWAY_TABLES = ("appliance_edition_provisioning_decisions", "internal_gateway_provisioning_authorizations",
                           "gateway_checkout_attempts", "local_gateway_remote_subscriptions",
                           "cloud_gateway_camera_entitlements", "gateway_claim_recovery_requests")


def _stripe_reference(item):
    return bool(str(item.get("stripe_subscription_id") or "").strip() or
                str(item.get("stripe_checkout_session_id") or "").strip())


def paid_full_vms_evidence(customer_id):
    """The verified paid Full VMS plan and licence for this customer, or None. Read only."""
    import billing_status
    import customer_entitlements
    import per_camera_billing
    from partner_db import rows
    if not customer_id:
        return None
    entitlements = [dict(item) for item in customer_entitlements.get_entitlements_for_customer(customer_id)]
    active = [item for item in entitlements if item.get("status") == "active"]
    plan = [item["id"] for item in active if item.get("product") in BASE_PLAN_PRODUCTS and _stripe_reference(item)
            and int(item.get("camera_slot_quantity") or 0) > 0]
    v2 = per_camera_billing.entitlement_for_customer(customer_id)
    v2_paid = bool(v2 and v2.get("status") == "active" and str(v2.get("stripe_subscription_id") or "").strip()
                   and int(v2.get("camera_quantity") or 0) > 0)
    if v2_paid:
        plan.append("camera_plan_entitlements_v2:" + str(v2.get("id") or v2.get("stripe_subscription_id")))
    licence = [item["id"] for item in active if item.get("product") == customer_entitlements.VMS_LICENSE_PRODUCT
               and _stripe_reference(item) and int(item.get("camera_slot_quantity") or 0) > 0]
    for order in rows("SELECT id,stripe_payment_intent_id FROM hardware_orders WHERE customer_id=? "
                      "AND sku LIKE 'AIC-APPLIANCE-%' AND status='paid'", (customer_id,)):
        intent = str(order.get("stripe_payment_intent_id") or "").strip()
        if intent and not billing_status.hardware_payment_reversed(intent):
            licence.append("hardware_order:" + str(order["id"]))
    if v2_paid and not per_camera_billing.purchase_reversed(customer_id):
        licence.append("camera_plan_entitlements_v2_licence:" + str(v2.get("id") or v2.get("stripe_subscription_id")))
    if not plan or not licence or customer_entitlements.usable_camera_capacity(customer_id) <= 0:
        return None
    return {"plan": sorted(plan), "licence": sorted(licence)}


def _customer_gateway_activity(db, customer_id):
    from server_gateway_edition import _table_exists
    for table in CUSTOMER_GATEWAY_TABLES:
        if not _table_exists(db, table):
            raise EditionUnverifiable(f"Gateway evidence table is missing: {table}")
        if db.execute(f"SELECT 1 FROM {table} WHERE customer_id=? LIMIT 1", (customer_id,)).fetchone():
            return table
    return None


def register_claimed_appliance_edition(appliance_id):
    """'recorded', or why not: 'not_claim', 'already_recorded', 'gateway', 'unpaid' or 'unverifiable'.

    Safe to call repeatedly (claim completion and every configuration poll of an unrecorded claimed appliance)."""
    from partner_db import connection, row
    try:
        appliance = row("SELECT * FROM appliances WHERE id=?", (appliance_id,))
        if not appliance or appliance.get("activated_via") != "claim" or not appliance.get("customer_id"):
            return "not_claim"
        if appliance.get("gateway_edition") is not None:
            return "already_recorded"
        customer_id = appliance["customer_id"]
        evidence = paid_full_vms_evidence(customer_id)
        if evidence is None:
            return "unpaid"
        with connection() as db:
            marker, _, any_gateway = edition_records(db, appliance_id)
            if marker is not None:
                return "already_recorded"
            if any_gateway or _customer_gateway_activity(db, customer_id):
                return "gateway"
            changed = db.execute("UPDATE appliances SET gateway_edition=? WHERE id=? AND customer_id=? "
                                 "AND activated_via='claim' AND gateway_edition IS NULL",
                                 (FULL_VMS, appliance_id, customer_id)).rowcount
            if changed != 1:
                return "already_recorded"
            details = {"edition": FULL_VMS, "basis": "paid_entitlement", "customer_id": customer_id, **evidence}
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (ACTOR["email"], ACTOR["role"], "appliance.edition_designated", "appliance", appliance_id,
                        json.dumps(details, sort_keys=True), datetime.now(timezone.utc).isoformat()))
        logger.info("claim edition recorded appliance_id=%s edition=%s", appliance_id, FULL_VMS)
        return "recorded"
    except Exception:
        logger.exception("claim edition registration failed closed appliance_id=%s", appliance_id)
        return "unverifiable"
