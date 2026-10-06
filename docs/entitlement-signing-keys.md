# Entitlement-signing keys — appliance trust anchor

Appliances run Talk Down and AAC Voice Call only with an entitlement snapshot
signed by the cloud (`app/appliance_entitlements.py`). They trust **only** the
public keyset the installer provisions:

| | |
|---|---|
| File on the appliance | `/etc/anyaicam-update/entitlement_signing_keys.json` |
| Owner / mode | `root:root 0644`, in `root:root 0755` `/etc/anyaicam-update/` |
| VMS container | bind-mounted read-only (`docker-compose.yml`) |
| Format | `{"keys": {"<key_id>": "<base64 32-byte Ed25519 public key>"}}` |
| Installed by | `installer/13-entitlement-signing-keys.sh`, only if its SHA-256 equals `ENTITLEMENT_SIGNING_KEYS_SHA256` in the signed release's `release.env` |

Nothing the cloud sends at runtime is trusted: keys in a configuration
response, inside a snapshot, in the local cache, or in `vms.env` (writable by
the `anyaicam` user) can neither add nor replace a key. With no keyset, or a
malformed or unsafely owned one, every paid feature is denied; recording, live
view and the rest of the VMS are unaffected.

The private key never leaves the cloud and never appears in a release,
installer, appliance or log. The builder and the installer step both refuse
anything containing private key material.

## Building a release

1. Get the cloud's public keys from the cloud you are building for:
   `GET https://<cloud>/api/appliance/signing-keys` returns exactly the keyset
   format above. Save it as `entitlement-signing-public-keys.json`.
2. Verify each key out of band before trusting it. For example, compare the
   SHA-256 of the raw key with the value computed on the cloud host from its
   `identity_signing_keys` table:
   `python3 -c "import base64,hashlib,json,sys;[print(k, hashlib.sha256(base64.b64decode(v)).hexdigest()[:16]) for k,v in json.load(open(sys.argv[1]))['keys'].items()]" entitlement-signing-public-keys.json`
3. Build with `--entitlement-signing-public-keys entitlement-signing-public-keys.json`.
   The builder validates and canonicalizes the file, packages it at
   `payload/keys/entitlement-signing-public-keys.json` and records its SHA-256
   in `release.env` and the artifact manifest. `--no-entitlement-signing-keys`
   builds an installer whose fresh appliances deny every paid feature; one of
   the two options is required.

The keyset reaches an appliance when this installer runs (fresh install or
repair). Software Update replaces the VMS application only and never changes
a trust anchor, so an appliance upgraded only through Software Update keeps
the keyset it already has (or denies paid features if it has none).

## Rotating the cloud's signing key

The keyset holds several keys, so rotation overlaps. The cloud signs with its
newest non-revoked key in `identity_signing_keys`
(`appliance_identity.ensure_signing_key`), so adding a key there switches
signing at once; there is no "pending key" state yet. Therefore:

1. Generate the new Ed25519 key pair off the cloud; keep the private half in
   the cloud's secret store only.
2. Ship a release whose keyset contains the current and the new public keys;
   install it on every appliance.
3. Only then add the new key to the cloud's `identity_signing_keys`, so it
   becomes the signing key (snapshots carry the key id).
4. After every appliance trusts the new key, revoke the old key on the cloud
   and ship a release whose keyset drops it.

An appliance that receives a snapshot signed by a key it does not trust
denies paid features until it gets a release that trusts that key; it never
learns a key from the cloud.
