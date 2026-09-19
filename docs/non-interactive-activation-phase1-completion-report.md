# Non-interactive activation — Phase 1 completion report

Implementation of `docs/non-interactive-activation-phase1-plan.md`
(approved at branch tip `899e8d0031e67a4a7f046e37ba7837b56a821cb7`).
Everything described here is committed on `staging/cloud-integration-
repair`. **Nothing has been merged or deployed, and no live/shared/
production/Samsung resource was touched at any point.**

## Commit chain

| # | Commit | Contents |
|---|---|---|
| 1 | `fb0da90` | `appliance_claims` schema (migration + bare migration test) |
| 2 | `4c45e0c` | `claim/begin` + `claim/status` routes, wired into `main.py` |
| 3 | `bd6eeb6` | Portal `lookup` + `confirm` routes |
| 4 | `f64dd5a` | `claim/complete` (the exchange into existing credentials) |
| 5 | `7afc9dd` | Agent-side `PortalClient` methods |
| 6 | *(this commit)* | Documentation |

## Files changed (full diff, `899e8d0..HEAD`)

Purely additive — 1033 insertions, 0 deletions, across:

- `app/appliance_claims.py` — new module, all five routes
- `app/db_migrations.py` — one new migration entry
- `app/main.py` — two new lines (import + registration call)
- `app/tests/test_appliance_claims.py` — 20 tests
- `app/tests/test_appliance_claims_migration.py` — 2 tests
- `appliance-agent/anyaicam_agent/portal.py` — three new `PortalClient` methods
- `appliance-agent/tests/test_portal_claim_client.py` — 8 tests

No other file was modified. `appliance_cloud.py`'s existing routes,
`appliance_activation.py`, `reenrollment.py`, `setup_wizard.py`, and
the `appliances`/`appliance_activation_tokens` tables are all
byte-for-byte unchanged.

## Endpoints implemented

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/appliance/claim/begin` | none (rate-limited) | Device opens/resumes a claim session |
| `POST /api/appliance/claim/status` | none (rate-limited) | Device polls; receives `claim_proof` once claimed |
| `POST /api/portal/claims/lookup` | `partner_identity()` + `customer_owner` + `appliance.self.link` | Customer looks up a claim by code |
| `POST /api/portal/claims/confirm` | same | Customer binds the claim to one of their own sites |
| `POST /api/appliance/claim/complete` | none (rate-limited) | Device redeems the proof for a permanent credential |

## Schema/migration added

One additive migration (`20260910_appliance_claims`): the
`appliance_claims` table plus `idx_appliance_claims_device_id` and
`idx_appliance_claims_status`. See the plan doc §2 and §1 for why this
is a new table rather than a change to `appliances`. One column beyond
the plan doc's original listing — `claim_proof_plaintext` — was added
during implementation; see "Deviations" below.

## Test results

**Focused (Phase 1 only):**
- `app/tests/test_appliance_claims.py` + `test_appliance_claims_migration.py`: **20 + 2 = 22 passed, 0 failed**
- `appliance-agent/tests/test_portal_claim_client.py`: **8 passed, 0 failed**

**Full regression, cloud app (`PYTHONPATH=app python -m pytest app/tests`):**
This suite has real run-to-run volatility independent of any code
change here, discovered while producing this comparison, and worth
reporting plainly rather than smoothing over:

- First full run against `HEAD` (all Phase 1 commits applied): **40
  failed, 1299 passed, 18 skipped**.
- A second full run of the **pre-Phase-1** tree (`899e8d0`, a separate
  git worktree, zero code difference from before this task started)
  produced **39 failed, 1280 passed, 18 skipped** — not the 34 this
  report's own earlier, narrower spot-check had found against the same
  commit. Re-running identical code twice produced two different
  failure counts.
- Every failing test name in both of the above two full runs was
  cross-checked: all but one already existed in at least one baseline
  run. The one exception, `test_talk_audio_relay.py::test_no_appliance_
  channel_uses_local_isapi_fallback`, was verified to **pass** in
  isolation on both the Phase 1 tree and the pre-Phase-1 tree
  identically (`15 passed` both times) — it only ever fails inside the
  full 1300+ test run, on either tree, meaning it is sensitive to
  whatever else already ran before it that session, not to any content
  difference this work introduced.
- None of the 40 failing tests are in `test_appliance_claims.py`,
  `test_appliance_claims_migration.py`, or any file this work touched.
  Phase 1's own 22 new tests passed in every configuration they were
  run in: isolated, combined, and inside the full suite.

Conclusion: this suite's shared-global-state test isolation is
pre-existing and imperfect (a fact this task's own earlier work this
session had already partially documented for the `RateLimiter`
singletons), and its exact failure set shifts by several tests between
otherwise-identical runs. Phase 1 did not make this worse in any way
that could be isolated or reproduced, and introduced zero reliably
reproducible new failures.

**Full regression, appliance-agent (`python -m pytest tests`):**
**3 failed, 439 passed, 37 subtests passed** (were 431 passed/3 failed
before Phase 1's 8 new tests). The 3 failures are pre-existing,
Windows-host-specific (`FileExistsError`/`OSError` semantics differing
from POSIX for directory-creation-under-a-blocking-file scenarios in
`test_package_dependencies.py`, `test_updater_commands.py`,
`test_updater_state_machine.py`) — confirmed unrelated: none touch
`portal.py` or anything Phase 1 changed, and they fail identically
with Phase 1 fully removed.

## Secret scan

Regex scan (private keys, AWS `AKIA`/`ASIA` keys, GitHub tokens,
Stripe live/test secret keys) against the full `899e8d0..HEAD` diff:
**clean**. Run before every commit in the chain, not just at the end.

## Deviations from the approved plan

Three were found during implementation, all narrowing/correcting the
plan rather than expanding scope, and all explained in the relevant
module's own docstring/comments as well as here:

1. **`claim_proof_plaintext` column added to the schema** (not in the
   plan doc's original table listing). The plan didn't specify how the
   plaintext proof value travels from the portal-confirm call to the
   device's next status poll — an in-process cache was the first
   instinct while writing the code, but that would violate the state-
   machine doc's own "every state transition is durably persisted"
   rule (a cloud-process restart between confirm and redemption would
   silently strand the claim) and would not work correctly across
   multiple cloud worker processes. Storing the plaintext value
   alongside its hash in the same row, nulled out the moment
   `claim/complete` consumes it, fixes both problems and keeps every
   state transition durable, matching the design's own stated
   principle.

2. **`claim_session_id`/`claim_code` moved off the URL, changing three
   endpoint shapes from the plan doc.** The plan specified `GET
   /api/appliance/claim/status?claim_session_id=`, `GET
   /api/portal/claims/{claim_code}`, and `POST
   /api/portal/claims/{claim_code}/confirm` — all three carry a
   bearer-equivalent secret in the URL. This was caught by this
   commit's own automated secret-hygiene test failing against the very
   first implementation, which used those exact shapes: httpx's
   request-line logging showed the claim code appearing in a "logged"
   line, and the same failure mode applies to every access log at
   every real deployment layer (uvicorn, any reverse proxy, a CDN),
   independent of anything this application does — this repo already
   has one documented instance of exactly this mistake for
   password-reset tokens (`docs/customer-appliance-readiness-
   blockers.md`). All three became `POST` endpoints taking the secret
   in the JSON body instead: `POST /api/appliance/claim/status`,
   `POST /api/portal/claims/lookup`, `POST /api/portal/claims/confirm`.
   This is a narrowing, security-motivated correction, not an
   architectural expansion — no new concepts, no new tables, no new
   trust boundary.

3. **`portal.py`'s `FORBIDDEN`/`sanitize()` set was *not* extended to
   cover `claim_code`/`claim_proof`, contrary to what the plan doc
   said this phase would do.** Reading the actual implementation
   showed `sanitize()` strips keys from the *outbound wire body*
   before a request is ever sent (a defense-in-depth measure against
   camera-credential-shaped keys reaching the portal through the
   generic HTTP path at all — see `provisioning.py`'s own comment
   calling it "a second, independent layer of defense") — it is not a
   log scrubber. Adding `claim_code`/`claim_proof` to that set would
   have silently stripped those fields from the real request bodies
   `claim_begin`/`claim_complete` legitimately need to send, breaking
   the exchange outright. "No secrets in logs" is satisfied instead
   the same way it already was for `activate()`/`credential`: none of
   the three new `PortalClient` methods log anything, and no caller of
   them exists yet (wiring the interactive wizard — the only place a
   real logging call site would eventually exist — is explicitly
   deferred; see boundaries below). Regression tests assert both
   directions: claim-flow values survive `sanitize()` unchanged, and
   camera-credential-shaped keys are still stripped as before.

No other deviations. Every explicit boundary held:

- `POST /api/appliance/activate` — unmodified, unmodified tests still pass
- `reenrollment.py` — unmodified
- `first_enroll()`/`coordinated_reenroll()` — unmodified, untouched, and proven to remain unnecessary-to-change by this commit's end-to-end test (the new `claim/complete` response authenticates against the existing `authenticate_appliance()` boundary without any agent-side change)
- `setup_wizard.py` — unmodified
- No entitlement-driven recording logic added
- No local appliance web UI built
- No unclaim/resale endpoint built (only reserved, unused schema columns: `revoked_at`, the `appliance_id` back-reference)
- No changes anywhere near Codex's `codex/motion-event-media-cloud-flow` work

## Remaining work before a real customer claim can be tested

1. **Nothing calls this flow yet.** `anyaicam-setup` is unmodified by
   design; wiring an actual non-interactive entry point (or the
   interactive wizard, as an interim step) to call `claim_begin` →
   poll `claim_status` → call `claim_complete` is unbuilt.
2. **No way for a customer to see or scan a claim code.** The local
   web UI / QR display mechanism is explicitly out of scope for Phase
   1 and remains the single biggest blocker described in
   `docs/customer-appliance-readiness-blockers.md`.
3. **No portal UI page calls `claims/lookup`/`claims/confirm`.** Both
   are plain JSON APIs today; a customer-facing page to enter/scan a
   code and pick a site does not exist.
4. **Live-environment validation not performed.** Per this task's
   explicit constraints, nothing here was run against staging,
   production, or Samsung — only local SQLite-backed automated tests.
   A disposable-EC2-style validation pass (matching this project's
   established pattern for prior phases) would be the natural next
   step before this reaches a real appliance.
5. **Unclaim/reassignment and resale workflow** — schema groundwork
   only (`revoked_at`, `appliance_id` back-reference); no endpoint.
6. **Entitlement-driven recording** — explicitly deferred to Codex's
   separate motion/cloud work; `claim/complete`'s response carries no
   entitlement data, matching today's `/api/appliance/activate`
   exactly.
