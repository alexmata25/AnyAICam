# Local mode offline licensing: current behaviour and options (2026-10-14)

Status: **decision needed (owner)**. This document changes nothing. The 1.2.6
remediation branch deliberately keeps the licensing model as it is.

## What 1.2.6 actually does

| Situation | Cameras that run | Source |
|---|---|---|
| Fresh install, never claimed (online or air-gapped) | **0** | `vms_capacity.licensed_camera_numbers` returns `[]` with no accepted snapshot |
| Claimed, synced once, then offline indefinitely | Last signed capacity, **forever** | `vms_capacity` ignores snapshot expiry once root has accepted it |
| Claimed, plan cancelled, appliance stays online | 0 after the next sync (~1 min) | signed snapshot carries `camera_capacity: 0` |
| Claimed, plan cancelled, appliance goes offline first | Last signed capacity, forever | same as row 2 |
| Paid runtime features (Talk Down, Voice Call) offline | Until the snapshot TTL (24 h default) | `appliance_entitlements` |

So today "Local" means *no ongoing cloud services*, not *no cloud at all*. Every
appliance needs **one online claim and one configuration sync** before any
camera runs. The product-mode docstring's "an air-gapped Local install keeps
its bootstrap mode" is true for the **mode**, but such an install runs no
cameras.

`app/local_license.py` already holds an offline, device-bound certificate core:
an Ed25519 signature over a hardware fingerprint, verified fully offline. Nothing
calls it yet. Its design doc (`docs/local-recording-licensing-design.md`) leaves
two questions open: where the issuing service lives, and what the product does
when verification fails.

## Options

**A. Keep the one-time online activation (status quo), and say so.**
- Customers must connect once to claim. After that the appliance is offline-capable indefinitely.
- Work needed: fix the wording (product-mode docstring, plan pages, installer/download copy) so nobody expects a fully air-gapped install.
- No change to enforcement.
- Risk: a customer who never connects gets 0 cameras. That is today's behaviour.

**B. Offline licence file (wire `local_license.py`).**
- The owner signs in on another device and downloads a capacity certificate. The certificate is signed by the entitlement keyset and bound to the appliance's identity UUID, or to the `local_license` fingerprint.
- The owner imports it on the appliance through the setup page or a USB drop.
- The root `capacity_accept` helper accepts it the same way it accepts a cloud snapshot, with the same keyset, signature and never-older rules.
- Work needed:
  - an issuing endpoint;
  - showing the appliance's ID or fingerprint on the setup page;
  - a third source in `capacity_accept.py`;
  - revocation (none offline; certificates should carry an expiry or a capacity cap);
  - support tooling for transferring a licence to new hardware.
- This is the only option that gives a true air-gapped install without weakening enforcement.

**C. Per-customer installer carrying a signed capacity.**
- Conflicts with the single published, checksummed installer, and with the rule that the release artifact never carries customer data.
- Not recommended.

**D. Unlicensed starter capacity (e.g. N cameras with no claim).**
- Simplest to build, but it **weakens entitlement enforcement**: copied installers would run cameras for free.
- Out of bounds for this remediation; listed only for completeness.

## Separate but related decisions

- **Capacity never expires offline.** A cancelled customer who disconnects keeps recording at the last signed limit. If that is not intended, capacity needs a grace expiry, which would affect legitimate long outages. This is a policy choice.
- **The Hybrid flags stay on after cancellation.** No mode is persisted for an empty entitlement (`appliance_cloud` "Decision B"). Capacity drops to 0, so cameras stop anyway.
- **Partner-managed accounts without a camera-slot entitlement** lose plan-included Smart Motion under the 1.2.6 fail-closed analytics authorization. Before, they kept the stored value.
