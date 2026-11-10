# Design: trusted server-side edition assignment in the claim flow

Status: PLANNED design, for owner and Codex review. No code yet.
Author: Claude, 2026-11-05. Candidate base: `bc62066`.

## Problem

Since `84b79d8`, cloud media needs a trusted `appliances.gateway_edition`.
Today only two paths record one:
- partner self-service provisioning (`full_vms`);
- internal test authorization (Gateway).

An appliance that joins through the customer claim flow
(`/customer/claim-appliance`) gets no edition. It links correctly, but is
refused cloud media until someone designates it. The claim flow needs to
decide the edition itself, from server records only.

## Rules (never relaxed)

1. The edition comes from server-verified records only:
   - a verified purchase or subscription;
   - an entitlement;
   - proven installation ownership.
2. Nothing the device sends is trusted:
   - the installer's edition;
   - heartbeat or configuration fields;
   - query parameters;
   - a request body;
   - the claim code.
   A device value is at most an advisory hint. A hint that disagrees
   with the server's decision holds the claim for review. It never
   changes the decision.
3. If anything is missing, ambiguous or conflicting, the edition stays
   NULL. The claim still links the appliance, but cloud media stays
   refused (the 84b79d8 behaviour).
4. One purchase unit (a Gateway licence or a Full VMS appliance order)
   is consumed by at most one appliance. Consumption happens under the
   customer lock, in the same transaction as the claim completion and
   its audit row.
5. Gateway evidence always wins over a Full VMS default. An appliance
   with any Gateway evidence is never marked `full_vms` (same rule as
   the backfill).

## Evidence sources (server records)

- **Cloud Gateway:**
  - `cloud_gateway_subscription_evidence`: a verified Stripe
    subscription, product `cloud_gateway`, active and not reversed;
  - its unconsumed quantity is the number of cameras, not appliances.
  The appliance-level proof is a paid `gateway_checkout_attempts` row
  that names this customer, with status `paid_review` resolved to paid,
  `reversal_state='none'` and a verified `payment_event_id`.
  (Open: one checkout = one appliance? See owner questions.)
- **Local Gateway:**
  - the Local Gateway licence purchase record (open: which table is
    authoritative; `local_gateway_remote_subscriptions` covers only the
    optional remote service, not the base licence).
- **Full VMS:**
  - a verified Full VMS hardware order (`hardware_orders`), or a
    `camera_plan_entitlements_v2` Local/Hybrid plan for this customer;
  - and no Gateway evidence for this customer that is still unconsumed
    and could belong to this appliance.
- **Installation ownership:**
  - the claim session proves the device secret (`/api/appliance/claim/begin`);
  - the customer-owner session proves the account;
  - `gateway_stable_installation_claim_ids` ties a reinstall on the same
    installation to its earlier claim. A reinstall keeps its decision;
    a claim by a different customer revokes it.

## Decision at claim confirmation (one transaction)

1. Take the customer lock (`transaction()`, BEGIN IMMEDIATE).
2. Re-read the claim session, the appliance and any existing decision.
3. Collect every unconsumed, verified purchase unit of this customer:
   - Gateway checkout units, by edition;
   - Full VMS orders and plans.
4. Decide:
   - exactly one Gateway unit and nothing conflicting: that edition.
     Write `appliance_edition_provisioning_decisions`
     (evidence_source `verified_gateway_purchase`, reference, version,
     `verified_by='system:claim_flow'`), set the marker and mark the
     unit consumed;
   - no Gateway unit and Full VMS evidence: `full_vms` (marker only,
     plus an audit row);
   - more than one candidate: hold. The customer owner is asked which of
     their own purchases this appliance uses. They pick from a
     server-built list of purchase IDs, never free text. The choice is
     revalidated in a fresh transaction;
   - nothing: hold. The edition stays NULL and the customer sees
     "this appliance is linked; cloud features need a purchase".
5. Write audit rows in the same transaction. On any error, roll back:
   the claim is not completed half-way.

## Revocation

- A refund, chargeback or subscription reversal of the purchase unit
  revokes the decision (`revoked_at`) and returns the marker to NULL.
  Cloud media then stops; local recording is unaffected.
- Transfer to another customer revokes too; the new owner's claim
  decides again.

## Tests to write (with the implementation)

- A device-supplied edition in any field is ignored. A conflicting hint
  holds the claim; it never assigns.
- One Cloud Gateway unit gives `cloud_gateway`. One Local unit gives
  `local_gateway`. Full VMS order only gives `full_vms`.
- Two candidates hold; the customer's choice is revalidated; a choice of
  another customer's purchase is refused.
- No evidence: linked, edition NULL, media refused.
- One unit cannot be consumed twice (two concurrent claims; the second
  holds).
- A refunded unit is not a candidate. A refund after assignment revokes.
- A reinstall keeps the decision. A new customer's claim re-decides.
- Rollback leaves no partial decision, marker or consumption.

## Owner questions

1. Is one Gateway checkout always one appliance? Or can a customer buy
   several Cloud Gateway appliances, or cameras for several appliances,
   in one checkout?
2. What is the authoritative Local Gateway licence purchase record?
3. For a claim with no purchase at all, should the appliance default to
   `full_vms` when the customer has a Full VMS plan but no hardware
   order (for example "install on your own PC")?

## Out of scope

No live Stripe, no webhook enablement, no backfill and no deployment.
The purchase producer (`VERIFIED_GATEWAY_PURCHASE` in
`gateway_edition_evidence.py`) stays unavailable until it is built and
reviewed.
