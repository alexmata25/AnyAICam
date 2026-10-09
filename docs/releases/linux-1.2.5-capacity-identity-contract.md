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
  "from_appliance_id": "<installer-pinned UUID>",
  "to_identity": {
    "appliance_id": "<canonical cloud appliance row ID>",
    "cloud_id": "<cloud ID>",
    "customer_id": "<owner customer ID>"
  }
}
```

The source UUID is obtained from the confirmed claim's `device_id`, or from the optional `provisional_appliance_id` sent with the existing activation-token request. The target is read from the cloud appliance row and must equal all three top-level snapshot identity fields and the appliance's persisted activation identity. Root accepts this only as the first accepted capacity state and only when `from_appliance_id` equals the protected installer pin. The signed capacity and high-water mark are written together as one atomic accepted-state file; the root pin is then updated by a separate atomic replacement. If that second write is interrupted, the next bootstrap/acceptance invocation repairs the pin only from the root-validated signed transition already stored in accepted state. A failed/invalid claim snapshot cannot change the pin.

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

Root accepted-store schema 2 includes the verified signing public key alongside the signed snapshot and high-water mark. The active keyset must authorize every new snapshot, but a previously accepted capacity remains verifiable offline after that key is retired from the active keyset. Existing schema-1 accepted state lacks this embedded key; deployments must keep its signing key in the keyset until that appliance has received and accepted a schema-2 state. Paid-feature snapshot expiry/keyset handling remains unchanged.

## Installer and Software Update expectation

The root helper, trusted keyset, root identity pin, and accepted state must be present before the VMS is stopped. Install/repair and Software Update must package the exact same helper code and systemd units. A fresh first claim must trigger signed-capacity acceptance after the cloud writes the initial transition snapshot. A failed identity-bootstrap call is an explicit installer error; a valid but not-yet-claimed provisional identity may report `awaiting_signed_transition` and must not rewrite the pin.

No existing cloud/customer identity, capacity or pricing is inferred from local mutable files. Legacy activation with no provisional UUID remains available; if its root pin differs from the cloud-assigned identity, it receives no signed capacity until a separately authorized transition is signed.
