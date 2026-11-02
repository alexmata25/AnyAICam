# Gateway offline remote authorization: source trace and proposed contract

Status: proposal only, 2026-11-02. No remote deadline, billing rule, trust
purpose, retention setting or local licence behavior is changed by this
document. Numbered source references below describe the inspected baseline
following `9bf20fe`; the independent in-process mitigation below is a later
follow-up and does not supply the missing deadline contract.

## Finding

The server stores subscription period and failed-payment facts, but the
appliance configuration does not carry an authoritative remote-access deadline.
The edge persists only a product-mode string. It therefore cannot establish
when remote access must end during an outage. A clock check added to that
string would invent a policy and an authority source.

An offline appliance also cannot learn an unannounced cancellation, refund,
dispute or trust-key revocation instantly. A bounded signed authorization
can limit this delay; its maximum window and renewal/grace treatment require
an explicit remote-access contract. Scheduled expiry already known to the
server can be represented exactly once that contract is defined.

The follow-up in `product_mode.apply_configuration_mode` adds an independent
process-local denial latch before persisting an explicit Gateway cancellation.
If a state write fails, the running process denies remote access despite an
older Hybrid file remaining readable. An omitted field, malformed mode,
direct cache write or failed fresh Hybrid write cannot clear that denial;
only a successfully persisted explicit Hybrid configuration can reopen it.
This changes no deadline or local capacity rule and Full VMS retains its
existing behavior. It is deliberately not durable revocation: after process
restart an older readable Hybrid cache can still exist. The protected
generation, state integrity and restart decisions below remain required.

The Linux adapter document
`RED-TO-GREEN-GATEWAY-API-CONTRACT-DRAFT12-REV6-20261101.md` is not an
authoritative remote expiry contract. No new remote policy is inferred from it.

## Actual data path

| Stage | Source | Verified behavior and limitation |
| --- | --- | --- |
| Current v2 subscription | `app/per_camera_billing.py:_upsert_current` (929), `_period_end` (906), `sync_from_stripe_event` | Reconciles the provider's current subscription through the existing webhook path. Stores `current_period_end`, `cancel_at_period_end`, `stripe_status`, `payment_failed_at`, scheduled plan/quantity/time, and entitlement status in `camera_plan_entitlements_v2`. The period end can come from the subscription or its first item. It is a billing fact, not an existing edge remote lease. |
| Legacy subscription facts | `app/customer_entitlements.py:_period_end` (952), `record_billing_state` (965) | Stores period end, scheduled cancellation and provider status in `customer_entitlements`. `record_billing_state` explicitly describes these fields as display-only, without changing access statuses. `expires_at` also exists on legacy entitlement rows; the product-mode resolver does not consult it. |
| Failed renewal | `app/billing_status.py:mark_payment_failed` (142), `sweep_grace` (220), `grace_ends_at` (249); `app/per_camera_billing.py:_set_subscription_payment_failed` | Existing server grace is seven days from the first recorded unpaid renewal. Repeated failure does not extend it. Suspension/recovery remains server-owned. Stored failure timestamps can be naive local ISO timestamps; a new remote contract must resolve their timezone before signing UTC deadlines. |
| Mode resolution | `app/per_camera_billing.py:product_mode` (1093); `app/customer_entitlements.py:product_mode_for_customer` (238) | An active v2 row supplies its plan's mode. If that helper returns an empty mode, the outer resolver consults active legacy camera-slot rows, preferring Hybrid. No time is returned. Thus a canceled/suspended v2 row plus active legacy Hybrid can still resolve Hybrid; this is existing fallback behavior, not permission to choose one row's deadline for another row's authorization. |
| Authenticated configuration | `app/appliance_cloud.py:authenticate_appliance` (112), nested `appliance_configuration` (736), response (1002) | Authenticates bearer credential and appliance ID, request timestamp and single-use nonce; rejects revoked appliances. Configuration resolves `product_mode`, returns it with signed feature and analytics documents, and queues mode restarts when needed. The response has no signed remote authorization or remote deadline. Request replay protection does not make an unsigned cached mode a signed response grant. |
| Edge receipt | `app/edge_camera_sync.py:_control_plane_headers` (116), `_control_plane_get` (131), `sync_provisioned_cameras` (349-411) | Uses existing control-plane URL and appliance credentials. Unreachable, malformed camera lists and unavailable identity transitions exit without replacing the mode. A well-formed response reaches `product_mode.apply_configuration_mode`. |
| Edge storage and decision | `app/product_mode.py:_read_persisted_mode` (95), `remote_access_authorized` (129), `apply_configuration_mode` (154), `persist_mode` (163) | Stores only `{"mode":"local"}` or `{"mode":"hybrid"}` atomically. Gateways require persisted Hybrid and effective Hybrid. Explicit no-mode/invalid mode on a successful configuration maps to Local; a missing mode field retains the previous mode. Administrative Hybrid alone cannot grant access. No identity binding, remote deadline or signed remote proof exists in this file. |
| Remote use | `app/product_mode.py:gateway_tunnel_request_denied`; `app/webrtc_publisher.py:_remote_access_allowed` (1110), `_close_remote_whep_sessions` (1114), `_remote_access_guard`; `app/main.py:maintenance_mode_middleware`, `appliance_media_fetch_endpoint` (146403) | Existing checks gate new remote work, reject the configured tunnel peer while retaining LAN routes, reject media-fetch capabilities after cancellation, and retry closing established WHEP sessions. These consumers are candidate integration points for a future verified remote grant. |

The proposed deadline must follow the same entitlement provenance that grants
remote access. It cannot be selected by taking the earliest timestamp from
unrelated Local, Hybrid, add-on, historical or superseded subscription rows.
Multiple valid remote sources and the legacy fallback need an explicit rule.

## Existing signed contracts that remain separate

`app/feature_entitlements.py:appliance_snapshot` (203) signs appliance/cloud/
customer identity, paid Talk Down/Voice Call features, camera capacity, issue
time and an expiry based on the existing feature snapshot TTL (default 24
hours, configurable within its existing bounds). Its expiry is not a remote
subscription deadline.

`analytics_entitlement_snapshot` (312) issues a separate premium analytics
document. Its existing window is 168 hours; `analytics_authorization_end`
(276) can shorten it for recorded plan cancellation, loss of Smart Motion
through a scheduled plan change, or a Test Lab lease. The current computation
also has a ten-minute minimum window. Copying it would impose that existing
analytics grace on remote access, which has not been approved.

`app/appliance_entitlements.py:verify` (706) binds signed features to the
activation identity. Paid features enforce expiry. Accepted camera capacity
has a deliberately distinct verification path that preserves offline local
capacity, including across clock rollback. Premium analytics has separate
MAC-protected state, an observed-time high water mark and expiry checks
(`store_analytics_grant`, 931; `_grants_from_state`, 959). These are patterns
to assess, not a remote policy or a complete state-replay defense.

Current trust purposes are exactly `analytics`, `capacity`, `features`,
`local_access` (`appliance_entitlements.py:TRUST_PURPOSES`, 143). There is no
remote-access trust purpose. Existing registry verification rejects unknown
purposes; adding one requires a compatible trust/installer/server rollout,
not just another call to the existing signer.

## Proposed server document, pending decisions

Add a separately signed `remote_access_authorization` member to the existing
authenticated configuration. Do not repurpose `product_mode`, camera capacity,
the feature TTL or the analytics document. Suggested schema, with no chosen
window or grace duration:

```text
type, version
appliance_id, cloud_id, customer_id, identity_generation
authorization_generation
remote_allowed
issued_at, not_before, expires_at
authorization_reason
entitlement_reference
signature
```

Names are proposed. All authorization-relevant fields must be inside the
signature. Use a dedicated type/version and an explicitly approved signing
purpose. Bind identity generation to an authorized transfer/re-enrollment
workflow; do not copy capacity identity transitions as permission for remote
service. An opaque entitlement reference identifies the chosen authority
without disclosing payment details or credentials to the UI.

The server computes `expires_at` from the approved remote revalidation window
and the applicable authoritative service end, including the approved renewal
and failed-payment treatment. It must not turn every routine renewal boundary
into an unexpected service cutoff, add a fresh grace on every refresh, or
extend known cancellation via an arbitrary minimum lease. Missing, ambiguous,
invalid or stale authority must produce an explicit deny/unavailable result,
not an unlimited allow document. Denials carry a generation that supersedes
earlier grants. Signing must not require provider queries or mutations on
every configuration request; use the established authoritative reconciliation
path and define its acceptable freshness separately.

## Proposed edge verification and storage

1. Verify type/version, signature, approved trust purpose/environment and key
   lifecycle using provisioned trust. Keys merely returned alongside the
   document are not trust anchors. Bind exact appliance/cloud/customer and
   identity generation before accepting remote authority.
2. Validate strict UTC timestamps, time ordering and the approved maximum
   window. Compare authorization generation against a protected monotonic
   generation/digest record. Identical retransmission is idempotent; an equal
   generation with different content is invalid. A previously accepted deny
   must not be reversible by replaying an older allow, including after restart.
3. Separate authorization generation from observed trusted time. Same-boot
   monotonic elapsed time prevents extending a lease by moving the wall clock
   backwards. Persist observed time and revocation high water marks atomically
   under the protected config directory; enforce their integrity and binding.
   A MAC alone cannot detect replay of a complete older valid state file.
   Cross-reboot state rollback therefore needs a protected monotonic anchor
   or a fresh online proof, with the residual administrator threat stated.
4. On rollback, corrupt/missing protected state, missing trust, wrong identity,
   unsupported schema or persistence failure, deny remote use. Do not copy the
   accepted-capacity `accepted=True` clock exception. A signed fresh response
   may establish trusted time according to an approved clock-recovery rule;
   simply resetting the local high water mark must not reauthorize an old grant.
5. Store only verified authority and required high water metadata. A transport
   outage leaves the verified grant usable only until its own deadline. A
   successful explicit deny closes remote sessions immediately. Define
   successful missing/invalid-document behavior and migration separately from
   transport unreachability. Do not silently fall back to cached Hybrid after
   this protection has been enabled.
6. A denial must take effect in memory before any best-effort state write.
   Denial persistence failure needs explicit restart behavior: preserving an
   older allow file is insufficient to prove durable revocation. A protected
   monotonic revocation record or mandatory fresh proof is needed to prevent
   restart from resurrecting it. Do not report durable revocation if storage
   could not record it.

Software on a machine controlled by an administrator cannot prove time or
state integrity against that administrator controlling the clock, disk,
secrets and process. Hardware-backed monotonic storage/secure time or online
verification would be a separate requirement. This proposal does not promise
instant offline revocation or protection against a fully compromised host.

## Consumers, cancellation and local continuity

Route Gateway `remote_access_authorized()` through the future verifier, then
retain its existing use sites. Recheck at session creation and capability use,
and retain an independent guard for established session teardown. Expiry must
not wait for the next successful cloud poll or a process restart. Bound guard
cadence and teardown retries explicitly; provider tunnel/forwarded-source
acceptance is still required to prove the real network path is identified.

The future grant controls remote access only. Event upload retains its
separate camera licence, explicit opt-in, motion entitlement, fresh session
authorization and credential checks; no implication that a remote grant
authorizes an upload. Local recording, local-first playback, LAN live view,
Basic Motion and local notification permissions continue through their
existing local capacity and authorization paths. Paid analytics keeps its
own signed expiry. Never rewrite accepted capacity, customer media, upload
preferences or retention during remote expiry/cancellation/reconnect.

On reconnect, only a newer valid allow for the current identity can restore
remote use after a deny. UI should distinguish verified-until, expired,
revoked, unverifiable and cloud-unreachable states without claiming an outage
means an expired subscription. Until implementation, the current notice
correctly reports authorization from the last cloud configuration and the
inability to check subscription changes offline; it shows no invented date.

Full VMS retains current behavior unless separately approved for migration.
Deploy server support before enforcing new-only Gateway clients. Define how
an older server, an absent document, a downgrade, and an already accepted
unsigned Hybrid cache behave during rollout. Any temporary legacy exception
must be explicit and bounded by policy; otherwise it becomes a permanent
expiry bypass. Existing binaries may reject a trust registry containing an
unknown new purpose, so registry rollout must be version-aware.

## Owner and contract decisions still required

1. What exactly authorizes remote access for v2 and legacy accounts, including
   multiple active sources and v2-to-legacy fallback after suspension? Confirm
   scheduled downgrade/cancellation and one-time Local interaction.
2. What is the maximum offline remote revalidation window, and what should
   happen at a normal unpaid-yet renewal boundary before any failed-payment
   event is known? Existing seven-day failed-renewal grace alone does not
   answer that question. Define deadline inclusivity, allowed clock skew,
   authority freshness and timezone treatment of stored naive timestamps.
3. Which signing purpose, trusted environment, generation allocation and
   transfer/rotation/revocation flow will issue these documents? Who owns
   additive API and compatible keyset/registry rollout?
4. Which existing Gateway installations require migration, and what is the
   missing/invalid-document behavior during migration, server downgrade and
   client downgrade? Is Full VMS explicitly excluded?
5. What clock/state threat model is required across reboot and disk restore?
   Is protected software state sufficient, or must remote access require
   hardware-backed time/state or fresh online verification after uncertainty?
6. What maximum session teardown delay, retry behavior and authenticated tunnel
   topology must acceptance prove? Assign an isolated installed test target;
   source checks cannot prove real proxy/source handling or service lifecycle.

## Isolated acceptance cases after approval

Use actual signer/verifier functions with disposable keys and in-memory
fixtures; inject time rather than waiting or contacting a provider. Cover
v2/legacy provenance, scheduled end, renewal and existing grace boundaries,
unknown/missing end, terminal cancellation, refund/dispute and recovery.
Verify exact deadline boundaries, malformed time, oversized window, wrong
identity/generation/key/purpose, older and conflicting equal generations,
replayed pre-cancellation allow, state tampering and whole-state replay,
clock rollback/forward, restart, persistence failure, missing document versus
unreachable transport, and valid reconnect.

Exercise real remote media and session functions with mocked transport and
assert denial precedes capability/file/secret lookup; prove session cleanup
retries independently of blocked control polling. Across each denial, use
actual local camera/playback/notification functions with an independently
accepted local capacity fixture to prove continuity and permission isolation.
Render the actual Gateway notice and verify that it reports authority/expiry
without inventing online billing status. Preserve existing feature/analytics
and Full VMS regression contracts. Installed tunnel, lifecycle and UI checks
remain a separately authorized isolated acceptance exercise.

No remote-expiry implementation or policy tests accompany this proposal:
the missing contract prevents specifying truthful expected remote expiry
behavior. The independent process-local write-failure mitigation has isolated
regressions for receipt-time denial, both Gateway editions, failed flush/
replace, reactivation, actual LAN HLS middleware passage, licensed camera
source selection and local-first playback. These are cancellation and local
continuity checks, not proof of offline subscription expiry or durable
revocation across restart. Existing completed tests are not presented as new
evidence of either missing guarantee.
