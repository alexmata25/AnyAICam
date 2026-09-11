# Non-interactive customer activation — design

**Status: architecture/design only. No feature code written, nothing
deployed, nothing merged.** Goal: a customer receives a preloaded
Samsung/edge appliance, powers it on, connects it to a network, and
activates it without terminal/SSH access. Builds on
`docs/customer-appliance-readiness-blockers.md` (which already named
this as the #1 blocker) and `docs/cloud-product-architecture-plan.md`
(Cloud Motion/24/7 entitlement work this design must plug into, not
duplicate).

## 1. Audit of the current `anyaicam-setup` wizard

Read directly from `appliance-agent/anyaicam_agent/setup_wizard.py`,
`reenrollment.py`, `portal.py`, and `app/appliance_cloud.py`'s
`POST /api/appliance/activate` -- not summarized from memory.

### 1.1 Inputs it asks for, in order

| # | Prompt | Mechanism | Required? |
|---|---|---|---|
| 1 | Portal URL | `input()`, defaults to `AgentConfig`'s current value | Required (must be correct before anything else works) |
| 2 | Mode (`development`/`production`) | `input()`, defaults to current value | Legacy/convenience -- a shipped customer unit should always be `production`; nothing customer-facing should ever prompt for this |
| 3 | Cloud ID + activation token | `qr_payload()`: paste a `cloud_id\|token` string, **or** point at a QR image file (`zbarimg`), **or** manually type Cloud ID + `getpass()` the token | Required -- this is the actual claim of identity |
| 4 | "Run camera discovery now? [Y/n]" | `input()` | Optional/legacy convenience -- unrelated to activation; a customer appliance should never trigger a LAN scan as a side effect of activation |

### 1.2 Files/config it writes

| File | Written by | Contents | Mode |
|---|---|---|---|
| `{config_dir}/agent.json` | `first_enroll()`/`coordinated_reenroll()` | Full `AgentConfig` (portal_url, mode, cloud_id, all path/interval settings) | `0600` |
| `{state_dir}/credential.json` | same | `{appliance_id, credential_id, credential}` -- the permanent bearer credential | `0600` |
| `{vms_recordings_path}/appliance_identity.json` | same | `{appliance_id, cloud_id, credential, customer_id, site_id, partner_id, activated_at, activation_version}` -- read by the VMS container via the existing recordings bind mount | `0600` |
| `{config_dir}/vms.env` | `_upsert_vms_env_key()` | Adds/replaces `ANYAICAM_CLOUD_URL=<portal_url>` only; every other line untouched | `0640` (installer-owned) |
| `{state_dir}/cameras.json` | `main()` directly, only if discovery was run | Raw scan results | default |

### 1.3 Services/containers started or modified

- `restart_service()` callback → `systemctl restart anyaicam-agent.service` (always, part of the identity-commit transaction itself -- `coordinated_reenroll`/`first_enroll` call it and roll back the identity files if it fails).
- `_queue_privileged_action(config, 'restart_vms', {'confirmed': True})` → writes a marker consumed by `anyaicam-privileged-watcher.path`/`.service` (this session's own fix), which runs `docker compose --project-directory /opt/anyaicam up -d` so the VMS container picks up the new `ANYAICAM_CLOUD_URL`. **Best-effort**: a failure here only prints a warning; it does not fail activation.

### 1.4 Cloud registration / API calls performed

1. `GET /api/appliance/config` (unauthenticated) -- pure connectivity check, no side effect. `PortalClient.test()`.
2. `POST /api/appliance/activate` (unauthenticated), body `{cloud_id, activation_token}`. Server-side (`app/appliance_cloud.py`):
   - Looks up `appliances` by `cloud_id` -- **the row must already exist**, created earlier by an admin/partner (`POST /api/admin/appliances/{appliance_id}/activation-token` is the only thing that ever mints a token, and it operates on an existing `appliance_id`). Tenant binding (`customer_id`/`site_id`/`partner_id`) is decided **before** this call, not by it.
   - Validates the token against `appliance_activation_tokens` (hashed, single-use, expiring), consumes it.
   - Mints a permanent credential (`secrets.token_urlsafe(48)`), stores its hash, marks the appliance `activated`.
   - Calls `persist_activation()` (today's monolith-colocated identity write -- see `appliance_activation.py`'s own docstring on why this is a simplification specific to the colocated deployment model).
   - Returns `{appliance_id, cloud_id, credential, credential_id, partner_id, customer_id, site_id, message}`.
3. `GET /api/appliance/commands` (authenticated with the just-issued credential) -- `verify_authentication()`'s proof that the new credential actually works, called from inside `coordinated_reenroll()`/`first_enroll()` before the identity swap is considered final.

### 1.5 Secrets/credentials handled

- **Activation token**: single-use, short-lived, entered via `getpass()` (never echoed) or embedded in a QR payload. Consumed server-side on first successful use; a second use fails closed (`used_at IS NULL` guard).
- **Permanent credential**: minted server-side, returned exactly once (`"message": "Store this permanent credential securely; it will not be shown again."`), never re-issuable except via re-enrollment (which requires knowing the *old* one first, or an admin-side revoke+reissue).
- Neither value is ever logged -- confirmed by `reenrollment.py`'s own `test_no_secret_logs` test (this session did not need to touch this; it already existed and passes).

### 1.6 Truly required vs. legacy/manual convenience

**Required, must survive into the non-interactive design:**
- A cloud_id (or equivalent device identity) and a proof-of-claim exchanged for a permanent credential.
- Persisting that credential + customer/site binding atomically across the three files (or their eventual replacements), with rollback on any failure.
- Restarting the agent service and the VMS container so the new identity/config actually takes effect.
- Verifying the new credential actually authenticates before declaring success.

**Legacy/manual convenience, drop or replace for a customer flow:**
- Portal URL and mode prompts -- a shipped unit should have a fixed, correct default baked in at build/factory time; no reason to ask a customer.
- The three alternate QR-input mechanisms (paste/file/manual) -- artifacts of a technician doing this by hand at a workbench.
- The camera-discovery prompt -- unrelated to activation; should never run as a side effect of it, non-interactively or otherwise (and per standing constraint, never without separate explicit approval regardless).

## 2. Activation models evaluated

### A. Short one-time code, entered by the customer somewhere (portal or appliance)
Appliance generates/displays a short code; customer types it in. Simple,
but "displayed... where" is the open question -- see below.

### B. QR code, customer claims the device in the AnyAiCam portal
Same underlying exchange as A, but the code (plus enough context to
skip typing anything) travels via a QR the customer's phone camera
reads, opening a pre-filled claim page.

### C. Pre-provisioned device identity, ships waiting to be claimed
The appliance already carries a durable identity (minted at first boot,
or ideally at manufacturing) before it ever reaches the customer.
"Activation" becomes purely an *association* step -- binding an
already-real device identity to a customer/site -- rather than an
identity-*issuance* step gated on a human typing a secret.

### Recommendation: **B, built on top of C's identity model, using a pull-based (device-authorization-grant-style) claim flow.**

This is the same shape as the well-established "smart TV / streaming
device login" pattern (OAuth 2.0 Device Authorization Grant, and
functionally how Chromecast/many IoT platforms onboard a screenless or
input-constrained device): the device never needs to *receive* anything
inbound, never needs the customer to type a device-generated secret
back into the device, and the actual tenant-binding decision happens on
a screen the customer already trusts (their phone, logged into their
own account) rather than on the appliance itself.

Why not A or a pure device-secret-typed-in model: doesn't remove the
"customer must accurately transcribe something" step B eliminates via
QR, and doesn't improve on B in any other dimension.

Why not pure C without B's claim UX: an appliance identity existing is
necessary but not sufficient -- something must still let a *specific
customer* associate themselves with a *specific box*, and a QR-driven
portal claim is the natural, camera-equipped-phone-friendly way to do
that. C's identity-provisioning-timing question (factory vs. first
boot) is independent of the UX and addressed in §3.

This also directly satisfies "no inbound access, no exposed local
server required for the core exchange": the appliance only ever
initiates outbound polling, so it works identically behind whatever
NAT/firewall the customer's home network has, with no port-forwarding
or local-network-discovery dependency for the activation exchange
itself (a local status page, described in §9, is a nice-to-have
convenience layered on top, not a requirement).

## 3. Device identity model

- **Device ID**: assigned when AnyAiCam software is first imaged onto
  the unit (today's `09-identity.sh` already generates a bare UUID
  `appliance_id` at install time -- this becomes the seed, not a new
  concept). Printed on a physical label as both human-readable text and
  a QR code at packaging time.
- **Cloud-side appliance row**: created either (a) at fulfillment time,
  associated with a specific order/serial for a known customer (B2B/
  reseller channel, matches today's admin-pre-assigns-tenant model), or
  (b) lazily, the first time an unclaimed device_id ever calls the new
  claim-begin endpoint (retail/self-serve channel -- no admin action
  needed before shipping). Both are the same DB row shape; only *when*
  it's created and *whether customer_id/site_id are already set* differ.
- **Stronger future option, not a blocker for shipping this design**:
  mint an asymmetric keypair on-device at first boot (or, better, at
  secure factory provisioning) and have the device sign its claim
  requests, so a captured device_id alone is never enough to impersonate
  the device. Phase 2 -- see §11 (threats) for why this isn't required
  to ship the first version safely.

## 4. Token/credential lifecycle

1. **Claim code** (new, short-lived, low-value): generated when a device
   first calls claim-begin; single-use across a bounded TTL (recommend
   10 minutes, matching typical device-grant UX); never authorizes
   anything by itself beyond "let this specific device_id be associated
   with whichever account enters/scans it next" -- it is not a bearer
   credential and never touches the VMS or camera data path.
2. **Claim proof** (new): once the portal-side claim completes, the
   server generates a one-time claim-proof value the device redeems
   (via its next poll) for the permanent credential -- kept separate
   from the claim code so the code (which the customer sees/types/QRs)
   is never itself the thing that unlocks credential issuance; only the
   device's own polling connection ever sees the proof.
3. **Permanent credential**: minted exactly as today (`activate_appliance()`'s
   `secrets.token_urlsafe(48)`, hash stored, returned once). No change to
   this piece -- it already fits every property needed here.
4. **Revocation**: already exists (`POST /api/admin/appliances/{id}/revoke`).
   Reused unchanged for resale/factory-reset (§10).

## 5. Tenant-binding rules

- Binding (customer_id/site_id, optionally partner_id) is set exactly
  once per claim, at the moment the portal-side claim completes -- never
  inferred from network location, IP, or anything the device itself
  asserts about who it belongs to.
- A device_id that is already bound to a customer must reject a new
  claim attempt (`409`, matching `coordinated_reenroll()`'s existing
  "already activated as a DIFFERENT cloud_id" rejection) unless an
  explicit, authenticated re-provisioning action clears it first (§10).
- A claim code/session is bound to exactly one device_id server-side
  from the moment claim-begin is called; the portal claim UI must
  display enough device-identifying information (device_id, and ideally
  a human-friendly label from the box's own printed serial) for the
  customer to confirm they're claiming the box in front of them, not a
  neighboring one.

## 6. Entitlement retrieval flow

Once claimed, the activation-complete response (or an immediate
follow-up authenticated call) must return a snapshot of what the
customer is actually entitled to, so the appliance can configure itself
without any further manual admin step:

- Product tier (Local/Hybrid/Cloud Motion/Cloud 24/7 -- per
  `docs/cloud-product-architecture-plan.md`).
- `camera_quantity` / `analytics_subscriptions.licensed_quantity` (both
  already exist).
- Per-camera `cloud_recording_mode`, if the customer's plan implies a
  default (e.g. every camera on a Cloud Motion plan defaults to
  `'motion'` unless/until the customer changes it) -- this is new
  plumbing; today `cloud_recording_mode` is only ever set by an explicit
  admin API call (`docs/phase1-staging-repair-report.md`'s coverage
  table already flags "deriving `cloud_recording_mode` from a purchased
  entitlement" as Phase 2, not yet built).
- Licensed analytics add-ons (Smart Motion, LPR, PPE, etc.).

This is squarely the Cloud Motion/24/7 architecture's own dependency,
not a new one this design invents -- flagging the connection explicitly
so the two efforts land coherently rather than each assuming the other
already did this part.

## 7. API endpoints (proposed)

All new endpoints below are appliance-facing and unauthenticated except
where noted (a device with no credential yet cannot authenticate) --
each is rate-limited per device_id/IP the same way `activation_limiter`
already rate-limits today's `/api/appliance/activate`.

| Endpoint | Method | Purpose | Request | Response |
|---|---|---|---|---|
| `/api/appliance/claim/begin` | POST | Device announces itself, gets a claim code | `{device_id}` | `{claim_code, claim_session_id, expires_at, poll_interval_seconds}` (mirrors RFC 8628's device/user code + interval shape) |
| `/api/appliance/claim/status` | POST | Device polls for claim completion | `{claim_session_id}` | `{status: "pending"\|"claimed"\|"expired"\|"denied"}`, plus `claim_proof` only when `status=="claimed"` |
| `/api/appliance/claim/complete` | POST | Device redeems the claim proof for permanent credentials | `{claim_session_id, claim_proof}` | Same shape as today's `POST /api/appliance/activate` response |
| `/api/portal/claims/{claim_code}` | GET (customer-authenticated, portal-side) | Portal looks up what device a code refers to, for display/confirmation | -- | `{device_id, device_label, status}` |
| `/api/portal/claims/{claim_code}/confirm` | POST (customer-authenticated) | Customer completes the claim -- this is where tenant binding actually happens | `{site_id}` (customer picks which of their sites) | `{status: "claimed"}` |

`GET /api/appliance/config` and the existing
`POST /api/admin/appliances/{id}/activation-token` /
`POST /api/appliance/activate` are unchanged -- the admin-pre-assigned,
technician-typed path this session's audit documented keeps working
for the B2B/reseller channel and for support-staff-assisted recovery.

## 8. Local state machine

See `docs/non-interactive-activation-state-machine.md` for the full
state/transition table and diagram. Summary: `UNCLAIMED → CLAIM_PENDING
→ CLAIMED → APPLYING_CONFIG → ACTIVE`, with `FAILED`/`RETRYING` states
reachable from any polling or config-apply step, and every state
durably recorded on disk so a power loss resumes from the last
completed step rather than restarting the whole flow.

## 9. Retry/recovery behavior

- **Idempotent by construction**: `claim/begin` for an already-`CLAIM_PENDING`
  or already-`CLAIMED` device_id returns the *existing* session/proof
  rather than minting a new one -- exactly the same "same cloud_id is a
  refresh, different cloud_id is a conflict" idempotency
  `coordinated_reenroll()` already uses for re-activation, applied one
  step earlier in the flow.
- **Power loss / reboot mid-flow**: the local state machine's current
  state and claim_session_id are the *only* things that must survive a
  restart -- persisted the same atomic-write way `appliance_activation.py`
  already persists identity (temp file + `os.replace`). On restart, the
  agent resumes polling from whatever state was last durably recorded,
  never re-calls `claim/begin` if a session_id is already on disk and
  not expired.
- **Internet failure mid-poll**: standard backoff, unchanged in kind
  from `service.py`'s existing offline-queue/retry patterns elsewhere in
  this codebase -- no new retry primitive needed, reuse what exists.
- **Never creates duplicate registrations**: enforced server-side by a
  uniqueness constraint on `(device_id)` for claim sessions (one active
  session per device at a time) and by `activate`-equivalent logic
  reusing the exact same "credential already issued for this appliance
  row" checks `POST /api/appliance/activate` already has.
- **Local UI (optional, not required for the core flow)**: a small local
  web page (reachable via the appliance's own LAN IP, or an mDNS name
  like `anyaicam-<short-id>.local`) showing the current claim code and
  state, for a customer who wants visual confirmation the box is
  waiting to be claimed, and for support staff on-site. Not a
  dependency of the activation exchange itself (§2's recommendation
  reasoning).

## 10. Security threats and mitigations

| Threat | Mitigation |
|---|---|
| Claim code intercepted/guessed | Short TTL (~10 min), single-use, low entropy is acceptable *only* because it never authorizes anything by itself -- it only lets someone attempt to claim a specific already-known device_id, and the actual credential is a separate, high-entropy `claim_proof` only the polling device ever receives |
| Attacker enumerates/guesses device_ids to claim boxes they don't own | Claim confirmation (§7's `/confirm`) requires the customer be authenticated in the portal *and* the device_id must be in an `unclaimed`/fulfillment-eligible state server-side -- an already-claimed or never-manufactured device_id is rejected regardless of code correctness |
| Replay of a captured claim_proof | Single-use, consumed atomically the same way today's activation tokens are (`used_at IS NULL` guard, already proven code) |
| Device impersonation (attacker claims to *be* a device_id they don't control) | Acceptable residual risk for v1, same as today's model (anyone who learns the real device_id + a valid not-yet-used activation token can activate today too) -- closed properly by §3's Phase 2 signed-identity option, not required to ship v1 safely given the code/proof separation above |
| Secrets in logs/UI | Same discipline already proven in this codebase: claim codes and proofs follow the exact never-logged pattern `reenrollment.py`'s `test_no_secret_logs` already enforces for the permanent credential; the portal UI shows the code only to the authenticated customer completing their own claim, never in any admin-wide list |
| Support staff needs to diagnose a stuck activation without seeing credentials | §12 |

## 11. Resale / factory-reset

- `installer/uninstall.sh --purge-all` (this session's own fix, now
  also removing the `anyaicam` system user) already returns software
  and local state to a genuinely clean install-state.
- Missing piece: an explicit **cloud-side unclaim**, so a resold/
  returned unit's device_id becomes claimable again by a *new* customer.
  `POST /api/admin/appliances/{id}/revoke` already exists for
  credential revocation; extend the same admin action (or add a
  paired `unclaim` action) to also clear customer_id/site_id and reset
  claim state to `unclaimed`, so the very next `claim/begin` for that
  device_id starts a genuinely fresh claim rather than being blocked by
  stale tenant binding.
- The local purge and the cloud-side unclaim are two separate actions
  (different systems, different operators) -- the runbook for a
  resale/return should explicitly call out doing both, in either order,
  before the unit ships to its next owner.

## 12. Support diagnosability without exposing credentials

- A new, narrow, admin/support-authenticated endpoint --
  `GET /api/admin/appliances/{id}/activation-status` -- returning only
  the state-machine state, timestamps, and non-secret error codes
  (never the claim code, claim proof, or permanent credential), mirrors
  the existing `diagnostics()` allowlist-not-denylist discipline
  (`commands.py`'s `_CAMERA_SUMMARY_KEYS`) applied to activation status
  specifically.
- Locally, `anyaicam-diagnostics` (already exists, `appliance-agent/
  scripts/diagnostics.sh`) is the natural place to add the same
  activation-state summary for on-site support, with the same
  never-print-the-secret discipline.

## 13. Migration path from the current interactive wizard

1. The claim-based flow is additive: `POST /api/appliance/activate`
   and the technician/QR-paste wizard keep working unchanged for the
   admin-pre-assigned/B2B channel and for manual recovery.
2. `first_enroll()`/`coordinated_reenroll()` are reused as-is as the
   *final* step of the new flow (§7's `claim/complete` response is
   shaped identically to today's `activate` response specifically so
   it can feed the exact same, already-tested enrollment functions
   without modification).
3. `setup_wizard.py` gains a new non-interactive entry point (a second
   `anyaicam-claim` console script, or a flag on the existing one) that
   runs the state machine instead of prompting -- the existing
   interactive wizard is not removed, since it remains the right tool
   for a technician at a workbench or a support session.
4. Rollout order: build and test the new endpoints and local state
   machine against a disposable/staging target first (matching every
   prior phase's own gating discipline in this project), only then wire
   it into what actually ships preinstalled.

## 14. Test plan

- **Server-side, per new endpoint**: mirrors this session's own testing
  conventions (`TestClient` + `override_target` throwaway SQLite,
  `_auth_headers`-style helpers) -- claim-begin idempotency, claim
  expiry, single-use claim_proof, tenant-binding rejection for an
  already-claimed device_id, rate-limiting.
- **Agent-side state machine**: pure function/unit tests per state
  transition (no network), matching `reenrollment.py`'s own test style
  -- resume-after-restart from every persisted state, never re-issuing
  a claim/begin when a valid session already exists, safe behavior on
  an expired session (must restart the flow cleanly, never wedge).
- **Integration**: a full claim → confirm → complete → `first_enroll()`
  round-trip against a disposable target (the same class of environment
  this session's Phase 1 work validated on, never staging/production/
  Samsung), including a simulated power-loss-mid-poll resume.
- **Security-specific**: no claim code, claim proof, or permanent
  credential ever appears in agent or server logs (same style as the
  existing `test_no_secret_logs`); an unauthenticated caller cannot read
  `/api/admin/appliances/{id}/activation-status`.

## 15. Concrete blockers discovered while writing this design

- **No cloud-side "unclaim" action exists today** -- only credential
  revocation. Needed for §11 to actually work; not yet built.
- **`cloud_recording_mode` has no entitlement-driven default** (already
  flagged in `docs/phase1-staging-repair-report.md`) -- §6 depends on
  this existing before "configuration is applied automatically" can be
  fully true; today it would still need one manual admin step per
  camera immediately after claim.
- **No local web UI exists at all today** -- §9's optional local status
  page is new surface area, not a config toggle on something already
  built.
- **Device-identity signing (this design's Phase 2 hardening) requires
  a decision on factory vs. first-boot key minting** -- out of scope to
  decide here; flagged so it isn't silently assumed either way later.
