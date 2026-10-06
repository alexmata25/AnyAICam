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
