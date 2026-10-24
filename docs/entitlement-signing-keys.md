# Entitlement-signing keys — appliance trust anchor

Appliances run Talk Down and AAC Voice Call only with an entitlement snapshot
signed by the cloud (`app/appliance_entitlements.py`). They trust **only** the
public keyset installed at one fixed path:

| | |
|---|---|
| File on the appliance | `/etc/anyaicam-update/entitlement_signing_keys.json` (fixed in code; no environment variable can move it) |
| Owner / mode | `root:root`, not group/other-writable (installed `0644`), in a `root:root`, not group/other-writable directory (`0755`); not symlinks |
| VMS container | bind-mounted read-only (`docker-compose.yml`) |
| Format | canonical `{"keys": {"<key_id>": "<base64 32-byte Ed25519 public key>"}}`, sorted, indent 2, LF-terminated |
| Keyset digest | SHA-256 of that canonical form |
| Installed by | the installer (`installer/13-entitlement-signing-keys.sh`) or a signed Software Update (`appliance-agent/system/apply_release.py`) — both only when the keyset's SHA-256 equals `ENTITLEMENT_SIGNING_KEYS_SHA256` in the release's `release.env` |
| Checked by | the VMS on every use, and `installer/validate.sh` with the same ownership/mode rules |

Nothing the cloud sends at runtime is trusted: keys in a configuration
response, inside a snapshot, in the local cache, or in `vms.env` (writable by
the `anyaicam` user) can neither add nor replace a key. With no keyset, or a
malformed or unsafely owned one, every paid feature is denied; recording, live
view and the rest of the VMS are unaffected.

The private key never leaves the cloud and never appears in a release,
installer, update, appliance or log. The builder, the installer step and the
Software Update applier all refuse key containers and private key material.

## Building a release (provenance check)

A 32-byte private seed is indistinguishable from a 32-byte public key by
length, so the builder only packages a keyset that matches a digest the
operator obtained **independently**:

1. **Keyset** — download it from the cloud you are building for:
   `GET https://<cloud>/api/appliance/signing-keys` (public, read-only).
   Save the JSON as `entitlement-signing-public-keys.json`.
2. **Digest** — obtain it separately, on the cloud host itself, over your
   authenticated admin channel (SSH/SSM into the cloud host, then inside the
   cloud container):

       python -m entitlement_keyset

   It reads the active public keys from the cloud database
   (`identity_signing_keys`, public half only), prints the canonical keyset
   and `sha256 <digest>`. Never take the digest from the same download as
   the keyset, from a chat message, or from a ticket that quotes the
   download.
3. **Build** with both:

       --entitlement-signing-public-keys entitlement-signing-public-keys.json \
       --entitlement-signing-keys-sha256 <digest>

   The builder refuses: a keyset without a digest, a digest without a keyset,
   a keyset whose canonical SHA-256 differs, key containers (PEM, OpenSSH),
   private-key material, extra fields, malformed ids, values that are not 32
   raw bytes, and an empty set. It packages the canonical keyset at
   `payload/keys/entitlement-signing-public-keys.json` and records its digest
   in `release.env` and the artifact manifest.
   `--no-entitlement-signing-keys` builds a release with no keyset; one of the
   two choices is required.

## How appliances get it

* **New appliances / repair installs** — `install.sh` runs step 13: checks the
  packaged keyset against `release.env`, refuses private material, installs it
  `root:root 0644` at the fixed path (`install -d -m 0755 -o root -g root` on
  the directory). `validate.sh` then checks owner, mode and digest.
* **Already-installed appliances** — a signed Software Update carries the same
  keyset. The root applier trusts it only through the existing chain: the
  manifest signature (release-signing key, already root-owned on the
  appliance) covers the package SHA-256, which covers `release.env` (naming
  the keyset digest), `artifact-files.json` and the keyset file. It validates
  the keyset (canonical, public-only) while verifying the release, then
  installs it atomically (temp file + rename, never through a symlink) into
  the root-owned directory **before** the VMS is stopped. Any failure —
  digest mismatch, unnamed or missing keyset, private material, unsafe
  directory, write error, bad signature, tampered package — stops the update
  with the running release and the existing keyset untouched, so paid
  features stay as they were (or denied; never granted). A release with no
  keyset leaves the existing one alone. If a later step rolls the
  application back, the new keyset stays: it is the signed release's own.
  The update result records `"entitlement_keyset": "updated"` or
  `"unchanged"`.

No new trust anchor is introduced: an appliance that has no release-signing
key accepts no Software Update at all, and therefore no keyset through it.

## Rotating the cloud's signing key

The keyset holds several keys, so rotation overlaps. The cloud signs with its
newest non-revoked key in `identity_signing_keys`
(`appliance_identity.ensure_signing_key`), so adding a key there switches
signing at once; there is no "pending key" state. Therefore:

1. Generate the new Ed25519 key pair off the cloud; keep the private half in
   the cloud's secret store only.
2. On the cloud host, `python -m entitlement_keyset --add <new-id>=<new public key>`
   prints the old+new keyset and its digest. Build a release with that keyset
   and digest.
3. Distribute it (installer or signed Software Update) and confirm every
   appliance has it: its update result shows `entitlement_keyset: updated`
   for that release, or `validate.sh` passes "Entitlement-signing keyset is
   provisioned root-owned".
4. Only then add the new key to the cloud's `identity_signing_keys`, so it
   becomes the signing key (snapshots carry the key id).
5. Later, revoke the old key on the cloud and ship a release whose keyset
   drops it.

An appliance that receives a snapshot signed by a key it does not trust
denies paid features until it gets a release that trusts that key; it never
learns a key from the cloud.

## TRUST-1 (Linux 1.2.7.1): the protected trust registry

From 1.2.7.1 the appliance's trust anchor is a **registry** with an explicit
signer lifecycle, plus a monotonic **protected trust state** (design:
`wt-1271-design/TRUST1-G2-DESIGN.md`; code: the shared resolver block,
byte-identical in `app/appliance_entitlements.py`,
`appliance-agent/system/capacity_accept.py` and
`appliance-agent/anyaicam_agent/updater/release_checks.py`).

| | |
|---|---|
| Registry | `/etc/anyaicam-update/entitlement_trust_registry.json`, `root:root 0644`, canonical JSON (sorted, indent 2, LF) |
| Format | `{"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": "production", "generation": N, "issued_at": ..., "keys": {id: {"public_key", "state": active/retired/revoked, "purposes": [capacity, features, analytics, local_access], "environment", "changed_generation", "appliance_ids"?}}}` |
| Protected state | `/etc/anyaicam-update/entitlement_trust_state.json`, `root:root 0644`: generation, registry SHA-256, every key binding ever installed, the revoked set |
| Delivered by | **only** the release chain: release key, signed manifest, package hash, `release.env` `ENTITLEMENT_TRUST_REGISTRY_SHA256`, registry. Installer step 13 and the root applier use the one routine `apply_release.install_trust_registry()` (registry first, then state). An entitlement key never signs the registry; nothing from cloud configuration sync is ever used as a registry or a key (Phase 1). |
| Flat keyset | still shipped and installed: the registry's projection (active + retired keys, revoked excluded), for 1.2.7 readers and an intentional rollback |

**Lifecycle.** *Active* keys verify new grants. *Retired* keys verify only
grants the appliance already holds (accepted camera capacity until replaced,
the cached Talk Down/Voice Call snapshot until its 24 h expiry, the analytics
grant until its <= 168 h expiry), so rotation causes no outage. *Revoked*
keys verify nothing, cached or new. A key id that an earlier generation bound
and a later registry omits is treated as **revoked** ("absent = revoked"). A
key from another environment never authorizes camera capacity, and authorizes
analytics/features/local access only for the appliance ids it lists (Phase 1
ships no such key: the builder, `release_checks` and the tool refuse lab/test
ids and other environments in a production registry).

**Monotonic.** The generation never decreases; a different document at the
same generation (a replay or tamper), a key id rebound to other key material,
or a revoked key coming back (under its id or another one) is refused by the
installer and the applier with every trust file unchanged. Re-installing the
same release (`--repair`) is a no-op; if the registry file was deleted, it
restores exactly the generation the protected state names. A lower or
missing registry once generation >= 1 was installed is `registry_rollback`:
the VMS **fails closed** (no camera licensed, every paid feature denied) with
that reason in `vms_capacity.capacity_status()` and one CRITICAL log line,
and never falls back to the flat keyset or to a key stored in an accepted
grant. Re-installing the current generation (installer `--repair` or the
same signed update) restores it. Recordings, event media and credentials
are never touched.

**Legacy mode.** With no registry installed **and** no generation >= 1
recorded in the protected state or in the accepted capacity, the flat keyset
is trusted as active for every purpose (existing 1.2.7 installs; Windows
1.3.3 until Blue's packaging ships the registry). A key stored in an accepted
grant never confers trust in any mode. A 1.2.7.1+ release always carries a
registry (the builder refuses otherwise), so a Linux 1.2.7.1 appliance never
runs in legacy mode after install. **Upgrade path:** the 1.2.7 Software
Update applier does not know the registry, so the first 1.2.7 -> 1.2.7.1 step
must be the 1.2.7.1 installer (or repair); later updates install registries
through the 1.2.7.1 applier.

### Building a 1.2.7.1+ release

1. On the cloud host (admin channel), print the next registry generation:

       python -m entitlement_keyset --trust-registry                        # first: every active cloud key, generation 1
       python -m entitlement_keyset --trust-registry --previous gen-N.json \
           [--add ID=PUBLIC_B64] [--retire ID] [--revoke ID]                # next generation

   It never drops a previous key, never moves a key backwards, never rebinds
   an id, and prints `sha256 <digest>` last. Keep every released generation.
2. Before the first 1.2.7.1 release, verify read-only against the production
   cloud that the registry holds **every** key in `identity_signing_keys`
   that signed a grant appliances still hold (owner decision 8; a missing key
   is revoked on every appliance and stops its capacity).
3. Build with `--entitlement-trust-registry registry.json
   --entitlement-trust-registry-sha256 <digest>` (instead of the flat-keyset
   flags). The builder verifies the digest, validates the registry with the
   appliance's own checks and derives the flat keyset projection.

### Rotation and revocation (registry)

* Rotation: `--add next-id=...` (generation N+1, ship it, confirm appliances
  have it), switch the cloud to the new key, then `--retire old-id`
  (generation N+2). Existing accepted capacity signed by the old key keeps
  working; new grants from it are refused; the next sync's grant from the new
  key replaces it. A new key always gets a **new key id**.
* Revocation (compromise): `--revoke id` and ship. On every appliance that
  installs it, everything signed by that key stops at the next read
  (capacity none, features/analytics denied) until a newer grant from an
  active key is accepted.

### Offline revocation propagation delay (Phase 1)

Revocation is delivered **only** by the installer or a signed Software
Update. An appliance that is offline, or that has not installed the release
carrying the revoking registry, keeps honouring grants from a since-revoked
key: accepted camera capacity indefinitely (it never expires offline), the
Talk Down/Voice Call snapshot for up to 24 h after its issue, analytics for
up to 168 h. The delay therefore equals the time until that appliance
installs the revoking release. A cloud-delivered, update-key-signed
demotion-only notice that would shorten this is Phase 2 (owner decision).

### Backups

The registry, the protected trust state and the accepted capacity
(`vms_capacity_accepted.json`) must be backed up and restored **together**.
Restoring an accepted state without its registry/state (or onto an
appliance whose registry lacks its signer) fails closed with a diagnostic
(`registry_rollback` or `unknown_key`); restoring a registry older than the
state is `registry_rollback`.
