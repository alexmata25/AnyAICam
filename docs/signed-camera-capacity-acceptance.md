# Signed camera-capacity acceptance (Linux 1.2.5)

The cloud-signed capacity snapshot in `/var/lib/anyaicam/vms/data-config/vms_capacity_signed.json` is untrusted staging input. The root-owned `anyaicam-capacity-accept.path` unit starts the fixed helper in `/opt/anyaicam-agent/privileged/capacity_accept.py`; it independently verifies the Ed25519 signature against `/etc/anyaicam-update/entitlement_signing_keys.json`, the identity, capacity schema, expiration on arrival, and monotonic `issued_at`/digest ordering. It writes the complete snapshot and high-water mark together as one atomic root-owned file at `/etc/anyaicam-update/vms_capacity_accepted.json`. The VMS reads only this accepted file, or an eligible root-owned migration fallback until the first signed authorization is accepted.

The installer-generated UUID is provisional claim correlation, not the appliance's permanent identity. Before claim, the root helper may store that UUID in `capacity_pending_claim.json` under the root-controlled trust directory; this record is not an authorization anchor and cannot grant capacity. The cloud claim creates the canonical `appliance_id` and signs an `identity_transition` from the provisional claim ID to the full appliance/cloud/customer identity. The first capacity is accepted only when its signature and schema are valid, the target matches the appliance's persisted activation identity, and the signed transition source matches the root-protected pending claim ID. The accepted snapshot/high-water state is committed first; only then is `capacity_appliance_identity.json` atomically created with the cloud-assigned appliance ID and the provisional record removed. Failed, malformed, replayed, or mismatched snapshots do not create or change that authoritative anchor.

Later re-enrollment or ownership transfer is not inferred from changed local files or a normal capacity snapshot. A global platform administrator must create a separate cloud transition authorization for a specific old identity and the appliance row's already-established new identity. The cloud signs that transition into the snapshot. Root accepts it only when its protected high-water state matches the exact `from_identity`, the persisted local identity and signed target match, and the snapshot ordering advances. The authorization endpoint does not itself change ownership or move cameras. Once accepted, the cloud may retain the transition field in later snapshots; the accepted root state tracks its transition ID and high-water mark.

Accepted store schema 2 embeds the public key that the root helper verified at acceptance. Because the state is root-owned and atomically replaced with the signed snapshot/high-water, capacity remains verifiable when that key is later retired from the active trust set. Legacy schema-1 accepted states do not contain the key and must be advanced/migrated while their signing key is still in the trust set before that key is removed. This is a staged key-rotation requirement, not a change to the offline-expiration policy. The cloud and edge cap all camera-capacity fields at 10,000, the maximum supported by the acceptance verifier.

For an existing activated 1.2.4 installation, the fixed root-run migration path may accept a valid signed capacity snapshot that predates the transition field, provided it matches the persisted activation identity. It then pins that cloud identity after acceptance. The first migration cannot prove the pre-1.2.5 provenance of mutable local identity or previously replayed snapshots; that historical limitation is unavoidable without a protected anchor that existed before migration and is documented in `docs/releases/linux-1.2.5-capacity-migration-risk.md`. Clean installations do not receive this exception.

An already-present 1.2.4 cloud-ID pin also reconciles a still-signed `initial_claim` transition when there is no accepted capacity and the pin exactly matches the authenticated transition target; it cannot authorize any identity change. If cloud-stored transition metadata is malformed or stale against current appliance ownership, the configuration endpoint returns only `identity_transition_unavailable`. The edge does not publish or apply that response and retries on its next normal sync, preserving the prior accepted capacity and local configuration while withholding owner-scoped data.

An already accepted camera capacity does not expire while the appliance is offline. New snapshots normally must be unexpired. Only the root-run installer/Software Update migration invocation may accept an expired first snapshot when no protected accepted state exists, preserving a previously working offline appliance during the one-time transition. Clean installs and the steady-state path watcher do not get this exception. The existing paid-feature expiration behavior is unchanged. No unsigned capacity from cloud responses or the legacy `vms_capacity.json` file grants capacity in this release. The latter is written only as a rollback-compatibility mirror for an intentional 1.2.4 rollback.

## First rollout requirement

The first rollout to an existing Linux 1.2.5 installation must use the updated 1.2.5 installer/repair path. It provisions the trusted keyset, root helper and path/service units before stopping or recreating the VMS; where a valid signed/migrated capacity is available, it accepts that state and pins the cloud identity before downtime. A clean install stages only a provisional claim correlation until the first cloud-signed capacity is accepted. Later signed Software Update packages carry and install the same helper and units, then validate/accept available state before downtime. Do not use an older installer or treat a Software Update-only rollout as the first trust-state bootstrap.

The root helper is fixed-path and argument-restricted, uses isolated Python (`-I`), imports only the standard library, performs no network access, and is installed root-owned. It rejects unsafe trust/state files, symlinks, hard links, nonregular files, writable trust directories, oversized inputs, invalid identities, replay, and signature failures. Normal uninstall removes the watcher/helper but preserves accepted capacity and trust state for offline continuity; purge-all remains destructive by design.

## TRUST-1 + G2 (Linux 1.2.7.1)

The paragraph above about schema 2 embedding the verified public key describes 1.2.7 and is superseded:

* **Trust is resolved on every read.** The accepted file's `accepted_key` is
  no longer a trust source (1.2.7 let it keep a removed key trusted). The VMS
  and the root helper resolve the snapshot's signer `key_id` against the
  current protected trust registry (`docs/entitlement-signing-keys.md`) for
  purpose `capacity`: active or retired verify; revoked, absent after binding,
  unknown, another environment, a rebound id or a rolled-back registry give
  no capacity, with the reason in `vms_capacity.capacity_status()` (logged
  once per change). `accepted_key` and the new `signer {key_id,
  public_key_sha256}` must equal the resolved key (`binding_mismatch`
  otherwise). Schema stays 2 (additive fields `signer`, `trust_generation`),
  so an intentional rollback to 1.2.7 still reads it.
* **Accepted mode (G2).** Already accepted capacity is re-verified (signature,
  identity, trust, high-water, transition) without any clock check: a clock
  rollback of any size or a cloud outage, including across reboots, keeps the
  last-known-good capacity. New grants stay strict: `issued_at` at most five
  minutes after the appliance clock, unexpired, newer than the high-water
  mark, same identity; a refused grant never replaces the accepted file. A
  clock behind the trusted floor (max(high-water issue time, accepted_at)) is
  reported as `clock_behind_trusted_time` (informational). When NTP corrects
  the clock, the next sync's grant is accepted normally; if it never does, the
  appliance keeps running on its last-known-good capacity.
* **Floor vs trust.** When a new grant arrives the current accepted state is
  checked structurally only (`_state_floor`), so a newer grant from an active
  key replaces a revoked-signer state, while an older or equal one is still
  refused as a replay. The helper never pins an identity from an untrusted
  state. A revoked signer's accepted file is kept as the anti-replay floor.
* **Migration.** Step 16 (and the 1.2.7.1 applier) run
  `capacity_accept.py --migrate-trust-state` under the acceptance lock after
  the registry is installed: it ratchets the protected trust state and adds
  `signer`/`trust_generation` to an existing 1.2.7 accepted state that the
  registry trusts (idempotent; an untrusted state is left unchanged).
* **Known limitation (N1b, owner decision 6).** TRUST-1 does not repair an
  identity transition accepted from a since-revoked lab signer; recovery is a
  full verified restore. No lab key ships in any registry.
* **Windows.** `capacity_accept_windows.py` uses the same policy and resolver
  (registry/state seams, ratchet under its lock). Windows packaging of the
  registry (install-runtime.ps1, build.ps1, prepare-keyset.py) is a follow-up
  for the next Windows candidate; until then Windows runs in legacy mode.
