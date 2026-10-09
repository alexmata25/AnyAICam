# Linux 1.2.5 capacity identity contract for Green/Red integration

This document is the source-side signing and installer contract for the isolated capacity-identity remediation branch. It is not a deployment authorization.

## Snapshot contract

The complete `anyaicam.feature_entitlements` body remains version 1 and is still signed as canonical JSON with Ed25519. The `identity_transition` extension is optional and is covered by that same signature; it is never trusted as an unsigned side channel.

Initial claim or first activation uses:

```json
{
  "schema": 1,
  "transition_id": "<unique opaque ID>",
  "kind": "initial_claim",
  "from_appliance_id": "<installer-generated provisional claim ID>",
  "to_identity": {
    "appliance_id": "<canonical cloud appliance row ID>",
    "cloud_id": "<cloud ID>",
    "customer_id": "<owner customer ID>"
  }
}
```

The source UUID is obtained from the confirmed claim's `device_id`, or from the optional `provisional_appliance_id` sent with the existing activation-token request. The target is read from the cloud appliance row and must equal all three top-level snapshot identity fields and the appliance's persisted activation identity. On a clean install, root stores the installer UUID only in `capacity_pending_claim.json` as a temporary correlation value; it is not the authoritative identity anchor and cannot grant capacity. The first capacity is accepted only when a valid cloud signature covers an `initial_claim` transition whose source exactly matches that protected pending value. Root writes the signed capacity and high-water mark together as one atomic accepted-state file, then atomically creates the authoritative pin from the cloud-assigned `appliance_id` and removes the pending value. If anchor creation is interrupted after the accepted-state commit, a later helper invocation repairs it only from root-validated accepted state. An invalid/replayed/mismatched snapshot cannot create or change the pin.

For an already activated 1.2.4 upgrade, the fixed root-run migration path preserves the prior migration behavior: it may accept a correctly signed pre-transition snapshot whose complete identity matches the persisted activation identity, including the approved first-migration expiry exception. It pins that cloud identity only after acceptance. This exception is not available to clean installs or the steady-state watcher. The unavoidable pre-migration provenance limitation is recorded in `docs/releases/linux-1.2.5-capacity-migration-risk.md`.

Some 1.2.4 appliances already persisted the cloud-assigned appliance ID while the signed snapshot still contains the installer-to-cloud `initial_claim` transition. With no accepted capacity yet, the helper treats that transition as already reconciled only when the protected pin's appliance ID exactly equals the signed transition target and the complete signed target matches the local activation identity. This first acceptance establishes the high-water mark; it does not authorize a different identity. A wrong target, mismatched pin, unauthorized transition, invalid signature, or replay remains rejected.

Subsequent re-enrollment or ownership transfer uses:

```json
{
  "schema": 1,
  "transition_id": "<unique opaque ID>",
  "kind": "authorized",
  "reason": "reenrollment | ownership_transfer",
  "from_identity": {"appliance_id": "...", "cloud_id": "...", "customer_id": "..."},
  "to_identity": {"appliance_id": "...", "cloud_id": "...", "customer_id": "..."},
  "authorized_at": "<timezone-aware timestamp>"
}
```

Only the global-platform-admin authorization endpoint may persist this transition for signing. It records the source identity, target identity from the already-current cloud appliance row, reason, time and unique ID; an audit event records the author. It does not itself transfer ownership or move cameras. The root helper requires the source triple to match the previously accepted root state and the target triple to match both the signed snapshot and persisted appliance identity. Ordinary refreshes cannot replace identity. New transition IDs are required for later changes. The cloud can retain the transition object in subsequent snapshots; the root accepted record remembers its ID and monotonic ordering.

## Capacity and signing-key continuity

Cloud `camera_slot_quantity`, `plan_camera_slots`, and `vms_license_capacity` are bounded to `[0, 10,000]`, matching the edge verifier. Values above the supported limit are capped at 10,000 rather than causing the whole signed snapshot to be rejected. The customer's stored entitlement/pricing data is not rewritten.

If stored cloud transition metadata is malformed or no longer targets the appliance row's current identity, the configuration endpoint returns a retryable `identity_transition_unavailable` status without returning owner-scoped configuration or capacity. The edge treats that response as a no-op: it does not publish the partial response, clear its last accepted capacity, or reconcile empty camera/configuration lists. Normal sync resumes after an authorized workflow repairs the transition state.

Root accepted-store schema 2 includes the verified signing public key alongside the signed snapshot and high-water mark. The active keyset must authorize every new snapshot, but a previously accepted capacity remains verifiable offline after that key is retired from the active keyset. Existing schema-1 accepted state lacks this embedded key; deployments must keep its signing key in the keyset until that appliance has received and accepted a schema-2 state. Paid-feature snapshot expiry/keyset handling remains unchanged.

## Installer and Software Update expectation

The root helper, trusted keyset, and any existing accepted state must be present before the VMS is stopped. A fresh installation stages only a protected provisional claim correlation before the first claim; the authoritative cloud identity pin is created after a signed snapshot is accepted. Install/repair and Software Update must package the exact same helper code and systemd units. A fresh first claim triggers signed-capacity acceptance after the cloud writes the initial transition snapshot. A failed helper/identity-bootstrap call is an explicit installer error; an expected unclaimed device reports `awaiting_signed_transition` without creating an anchor.

No accepted cloud/customer identity, capacity or pricing is inferred from local mutable files. Legacy activation with no provisional UUID remains available for enrollment; on a clean install it cannot receive signed capacity until the cloud supplies a valid initial-claim transition tied to the protected claim correlation, while eligible activated 1.2.4 upgrades use the explicit signed migration path.
