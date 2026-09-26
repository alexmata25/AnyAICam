# Local Recording device-bound licensing ("Local ID") — design (2026-09-19)

**Status: design only. No installer or application code has been
written for this yet** -- explicitly requested to be scoped and
designed before any installer work begins, and added to the Local
Recording roadmap rather than implemented now.

## What already exists, and why it isn't enough on its own

`installer/09-identity.sh` already generates an `appliance_id` (a
random UUID) at install time and writes it to a JSON file
(`$IDENTITY_FILE`, `chmod 0600`). This is a real, existing precedent
for "stable per-install identity" -- but it is exactly the "plain
editable config value" this task explicitly says not to trust alone:
it is a random value with no relationship to the actual hardware, no
signature, and nothing that would stop it from working correctly after
being copied byte-for-byte to a different PC. The design below adds a
second, independent layer on top of it rather than replacing it --
`appliance_id` can remain one *input* into the new fingerprint, but the
thing that actually gates whether the software runs is new.

## Goal

One purchased Local license = one authorized AnyAiCam installation/
device, unless support explicitly transfers it -- enforced by binding
the license to the specific machine, not to a copy of the application
files.

## Architecture

```
ACTIVATION (one-time, requires connectivity)
  installed machine
    -> compute device fingerprint (hash of stable local characteristics)
    -> send {license_key, fingerprint_hash} to the licensing service
    -> service checks license_key is unused/not-revoked for a DIFFERENT
       fingerprint, generates a new Local ID, and issues a SIGNED
       license certificate: {local_id, license_key, fingerprint_hash,
       product="local", issued_at} signed with the service's private key
    -> certificate stored locally (secure file permissions, not secret-
       dependent -- its authenticity comes from the signature, not from
       being hidden)

EVERY STARTUP (offline, no connectivity required)
  -> recompute the fingerprint from THIS machine's current
     characteristics
  -> verify the stored certificate's signature against an embedded
     PUBLIC key (ships with the software, needs no network call)
  -> verify the certificate's fingerprint_hash matches the freshly
     recomputed one
  -> both pass -> proceed normally
  -> either fails -> fail closed (see "what fail-closed means" below)
```

### Device fingerprint: what goes in, what doesn't

Combine several **stable-but-not-oversensitive** local characteristics
that do not travel with a copied folder, then hash them -- never store
the raw values:

- `/etc/machine-id` (Linux) -- lives outside the application directory
  entirely, regenerated per OS install; this alone already defeats the
  literal "copy the software folder to another PC" threat, since the
  copied folder does not carry the source machine's `/etc/machine-id`
  with it.
- The root/boot volume's filesystem UUID.
- The primary network interface's MAC address (a weaker signal alone,
  since it's spoofable, but a useful additional factor combined with
  the others -- never used as the sole input).
- The existing `appliance_id` from `09-identity.sh`, purely as one
  additional input (it's already generated and stored; reusing it
  costs nothing and ties the new system to the existing install-time
  identity rather than inventing a parallel one).

`fingerprint_hash = SHA-256(machine_id || volume_uuid || mac_address ||
appliance_id)`. Only this hash is ever transmitted or stored -- never
the raw inputs. Any of these individual characteristics can drift
slightly on real hardware over time (e.g. a NIC swap); this is
addressed by the migration/replacement workflow below, not by
building a "close enough" fuzzy-match into the verification itself
(fuzzy matching would weaken exactly the guarantee this system exists
to provide).

### Why signed, not just stored

A plain locally-stored fingerprint (even a correct one) is just
another editable file -- nothing stops someone from recomputing what
*this* machine's fingerprint would be and overwriting the stored value
to match. The signature is what makes tampering detectable: the
certificate's fields are signed by the licensing service's private
key, which never leaves the service. Startup verification only needs
the corresponding *public* key (embedded in the shipped software),
so **verification itself needs no network call and no cloud identity
check** -- satisfying "preserve offline Local operation" and "do not
tie the Local product to continuous cloud identity" directly. Editing
any field (license_key, fingerprint_hash, local_id) invalidates the
signature; recomputing a matching signature requires the private key,
which only the licensing service holds.

### What "fail closed" means here, precisely

Per the explicit instruction not to risk corrupting recordings or
locking customers out of their own video: a validation failure
(missing certificate, bad signature, fingerprint mismatch) disables
**new licensed operation** -- new recording, new feature access --
never deletes, encrypts, or blocks access to footage already on disk.
Existing recordings remain readable/exportable through whatever
recovery/local-access path already exists for an unlicensed install.
This is licensing enforcement, not DRM on the media itself.

### Migration/replacement (RDM/support workflow)

A `local_licenses` table (cloud side, alongside the existing
entitlement tables such as `customer_entitlements.py`/
`analytics_entitlements.py`) tracks: `license_key`, `local_id`,
`fingerprint_hash`, `status` (`active` / `revoked` / `transferred`),
`issued_at`, `activated_at`, `revoked_at`, `revoked_reason`,
`replaced_by_local_id`. A support/RDM action (gated by the existing
role-permission system this codebase already uses everywhere else,
not a new ad hoc check):

1. Marks the old `local_id` `revoked` for that `license_key`.
2. Issues a fresh activation allowance for the same `license_key`,
   which the customer's new/repaired machine uses to go through the
   same one-time activation flow above, generating a new `local_id`
   bound to the new fingerprint.

Legitimate hardware changes (a customer's own NIC swap, drive
replacement) are handled by the *same* transfer workflow, not by
loosening what counts as a fingerprint match.

### Local vs. Hybrid stay fully separate

- **Local**: one-time purchase, this signed-certificate/fingerprint
  system, no recurring billing, no ongoing cloud entitlement check,
  fully offline after activation.
- **Hybrid**: the existing recurring, cloud-enabled product with its
  own Stripe-backed subscription/entitlement checks
  (`customer_entitlements.py` and friends) -- untouched by this
  design. The two verification paths are never merged: a Local install
  never calls a subscription-status endpoint, and Hybrid's entitlement
  checks never depend on a device fingerprint.

### Audit logging

New audit events, reusing whatever this codebase's existing
audit/event-recording convention is (the same principle AACO's
`unlock_door` already follows -- append to the existing audit path,
never a bespoke new one): `local_id.created` (at first fingerprint
generation), `local_license.activated` (license_key, local_id,
fingerprint_hash, timestamp), `local_license.validation_failed`
(reason: `signature_invalid` / `fingerprint_mismatch` /
`certificate_missing` / `certificate_corrupt`), `local_license
.transferred` (old_local_id, new_local_id, revoking support identity,
timestamp).

### Secure storage of the local identity

The certificate file follows the same pattern `09-identity.sh` already
establishes for `IDENTITY_FILE`: owned by the service account, `chmod
0600`, not world-readable. Because the certificate's integrity comes
from the signature rather than from secrecy, encrypting it at rest is
a defense-in-depth nice-to-have (e.g. wrapping it via the OS's own
keychain/DPAPI where available) rather than the core control -- the
core control is "can this be forged or edited without the private
key," and the answer is no regardless of file permissions.

## What this explicitly does not do

- No invasive DRM, no encryption of recordings, no kill-switch that
  can destroy or lock away a customer's own video files.
- No requirement for continuous/periodic phone-home after activation
  -- verification is fully local and offline.
- No fuzzy/approximate fingerprint matching that would weaken the
  actual anti-copy guarantee -- hardware changes go through the
  explicit support transfer workflow instead.
- No merging of Local's licensing model with Hybrid's subscription
  model.

## Open questions for whenever implementation actually begins

- Exact signing algorithm (Ed25519 is the natural choice: small
  signatures, fast verification, no dependency this codebase doesn't
  already have a CPU-only equivalent for).
- Where the licensing service issuing certificates actually lives
  (a new small endpoint on the existing cloud portal, versus a
  separate service) -- an infrastructure decision, not a cryptographic
  one, and out of scope for this design pass.
- Exact behavior/UX when validation fails on an otherwise-working
  installation (a persistent banner directing the customer to support,
  versus a harder block) -- a product decision to make when installer
  work actually begins.
