# Full VMS edition marker: rollout without disrupting Full VMS customers

Status: PREPARED. Nothing has been backfilled or deployed. Every step below
that touches a production database or service needs separate owner
authorization.

## Why

Cloud media credentials (`/api/appliance/recordings/{camera}/credentials`,
live relay, event-media catalog) now require a trusted server-side edition.
`_require_gateway_media_authorization`:
- allows `gateway_edition='full_vms'` with no Gateway evidence: Full VMS
  rules are unchanged;
- refuses NULL, unknown, mixed or conflicting identity before STS (403);
- refuses Local Gateway always, and every Gateway for live relay;
- allows Cloud Gateway only with a verified paid binding for that camera
  (owner decision 2026-11-05):
  - the binding is the authorization basis, and no Full VMS Hybrid plan
    is required;
  - an explicitly `disabled` camera stays off;
  - the customer must have an event retention the event-media sweep will
    enforce (cloud-policy override, v2 plan or legacy plan, 7/14/30 days).
    Without one the upload is refused, because the sweep would never
    expire it;
  - open owner decision: a default Cloud Gateway retention. Until then
    an administrator sets the customer's cloud retention.
- returns 503 for a missing schema or any lookup error.
- reads the edition with a plain connection. Only the Cloud Gateway path
  takes the customer lock (write transaction) needed by paid-binding
  verification, so Full VMS requests never serialize behind writers on
  SQLite.
- The configuration route advertises `motion` for a Cloud Gateway camera
  only when it has an active binding (else `disabled`). Every upload is
  still verified at credential and catalog time.

Existing Full VMS appliances predate the column and are NULL. If the new code
served traffic before they were designated, their cloud event uploads would
be refused. The sequence below prevents that.

## Writers of the marker (candidate)

- Staff onboarding (`partner_workspace` onboarding route): records `full_vms`
  unless a live global administrator chooses `local_gateway`.
- Self-service provisioning: records `full_vms`, also on a retry, and never
  overwrites another edition.
- Internal Gateway provisioning (existing): sets `local_gateway` or
  `cloud_gateway`, and refuses any appliance whose marker differs.
- Device claim flow: records NOTHING. The server cannot know the device's
  edition, so such an appliance is refused cloud media until a trusted
  designation exists: Gateway provisioning, or a reviewed `designate_full_vms`.
  Open product item: give claimed Full VMS installs an authoritative edition
  source (for example purchase evidence) so new claimed Full VMS customers
  are not left waiting for manual review.
- Camera capacity treats `full_vms` exactly as NULL was treated before.

## Backfill (app/full_vms_edition_backfill.py, tools/full_vms_edition_backfill.py)

Eligible only when ALL of the following hold, from server records:
- the marker is NULL;
- there is no Gateway evidence in any of: edition decisions, internal
  authorizations, checkout attempts, Local remote subscriptions, Cloud
  camera bindings, stable claim IDs, claim recovery requests;
- `created_at` is strictly before the operator's cutoff;
- the appliance was not created by the device claim flow;
- it has a customer.

Everything else is listed for review and never converted. A recorded
`full_vms` that has Gateway evidence is reported as a CONFLICT (cloud media
stays refused).

The cutoff is the earliest moment any Gateway-capable server code or Gateway
installer could have activated against that database. It is attested by the
operator from deployment and distribution records.

## Deployment sequence (each step separately authorized)

1. Snapshot the production database (existing backup procedure).
2. Stage the release on a NON-SERVING instance (no traffic). Its startup
   applies the additive migrations. Old code keeps serving and ignores the
   new column.
3. `readiness` (read-only): record counts by marker.
4. `plan --created-before <cutoff> --out plan.json` (read-only). Review every
   eligible and review row. Record the plan SHA-256.
5. `apply --plan plan.json --sha256 <reviewed> --actor-email <global admin>
   --i-am-authorized`. This is one transaction: any appliance that changed
   since review aborts everything. One audit row is written per appliance.
6. Resolve the review list deliberately: `designate_full_vms` with an evidence
   reference for proven Full VMS, Gateway provisioning for Gateways, and
   leave unknowns refused.
7. Gate: `readiness` shows no unmarked appliance that is an active Full VMS
   customer with cloud event protection. Only then switch traffic to the new
   code.
8. Catch-up: rerun `plan` for appliances that old code created between step 2
   and the switch (still before the cutoff), review and apply.
9. Monitor 403 "Appliance edition is not verified for cloud media" responses
   by appliance. Each one is either an unresolved review item or a
   claim-activated appliance waiting for designation.

Rollback: switching traffic back to the previous release restores the old
behaviour. The backfilled markers are additive and harmless to old code.

## Not done here

No backfill was run, no production database was read, nothing was deployed,
and AWS uploads stay disabled.
