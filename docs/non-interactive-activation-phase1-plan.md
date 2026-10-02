# Non-interactive activation — Phase 1 implementation plan

Implements the minimum cloud-side foundation for the QR/pull-based
claim flow recommended in `docs/non-interactive-activation-design.md`
and modeled in `docs/non-interactive-activation-state-machine.md`
(both committed at `327dd62e0c8941468a5b7cbab672ecb72eed577c`). **Plan
only — no code has been written.** Grounded by re-reading, this pass,
the exact current schema and route code this plan modifies:
`app/partner_db.py`, `app/appliance_cloud.py` (`authenticate_appliance`,
`activate_appliance`, `RateLimiter`/`activation_limiter`),
`app/appliance_activation.py` (`persist_activation`, `ActivationConflict`),
`app/main.py`'s route-registration block (~line 47320), and
`appliance-agent/anyaicam_agent/{portal.py,reenrollment.py}`.

## 0. Scope

**In:** pre-activation device identity, `claim/begin`, `claim/status`,
authenticated customer `claim/confirm`, `claim/complete` (one-time
exchange into the existing credential mechanism), idempotent retries,
expiration/replay protection, no secrets in logs, DB columns that make
a future unclaim/reassignment endpoint straightforward to add without
another migration.

**Out (explicitly deferred, not touched by this plan):**
- The local appliance web UI / how a customer actually *sees* the
  claim code (QR rendering, captive portal). Phase 1 is server-side
  only; the agent-side changes in this plan are limited to a thin
  `PortalClient` HTTP client, not a UI.
- Redesigning `first_enroll()` / `coordinated_reenroll()`
  (`appliance-agent/anyaicam_agent/reenrollment.py`) or
  `validate_activation_response()`. `claim/complete`'s response is
  deliberately shaped identically to today's `/api/appliance/activate`
  response so these functions need zero changes.
- Entitlement-driven `cloud_recording_mode` / any Cloud Motion logic
  (Codex's separate work).
- The actual unclaim/reassignment *endpoint* and resale workflow —
  this plan only ensures the schema has the columns that endpoint will
  need (`revoked_at`, nullable `appliance_id`-back-reference), so
  adding it later is additive, not another migration.
- Any change to `appliances`, `appliance_activation_tokens`, or the
  admin-driven `/api/admin/appliances/{id}/activation-token` flow —
  both activation paths (admin-pre-assigned token, self-service claim)
  coexist unmodified, side by side.

## 1. Why a new table, not a schema change to `appliances`

`app/partner_db.py:62` defines `appliances` with `customer_id TEXT NOT
NULL` and `site_id TEXT NOT NULL`. A self-service claim needs a device
to exist, server-side, *before* any customer/site is known — which
today's `appliances` row structurally cannot represent, and
`appliance_activation_tokens` (line 74) is keyed on an existing
`appliance_id` FK for the same reason. Relaxing two `NOT NULL`
columns on a table already referenced by `cameras`, recordings, and
health history would need a SQLite table-rebuild migration (SQLite has
no `ALTER COLUMN`), on both the SQLite and Postgres backends
`database_backend.py` supports — exactly the kind of invasive change
to already-tested, load-bearing machinery this task says not to do.

Phase 1 instead adds one new, fully independent table,
**`appliance_claims`**, that holds all pre-claim and claim-in-progress
state. The `appliances` row itself is only ever `INSERT`ed at
`claim/complete` time, once `customer_id`/`site_id` are known — at
which point every existing `NOT NULL` column is already satisfiable,
so `appliances`' schema and every function that reads it are
untouched.

## 2. Schema changes

One new file-appended migration entry in `app/db_migrations.py`
(additive, follows the existing `('YYYYMMDD_slug', '''SQL''')`
convention used for `detection_events`, `camera_provisioning_requests`,
etc.):

```python
('20260910_appliance_claims', '''
CREATE TABLE IF NOT EXISTS appliance_claims(
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    claim_session_id TEXT UNIQUE NOT NULL,
    claim_code_hash TEXT NOT NULL,
    claim_proof_hash TEXT,
    status TEXT NOT NULL,
    customer_id TEXT,
    site_id TEXT,
    claimed_by TEXT,
    appliance_id TEXT,
    proof_expires_at TEXT,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id),
    FOREIGN KEY(appliance_id) REFERENCES appliances(id)
);
CREATE INDEX IF NOT EXISTS idx_appliance_claims_device_id ON appliance_claims(device_id);
CREATE INDEX IF NOT EXISTS idx_appliance_claims_status ON appliance_claims(status);
'''),
```

- `status` is one of a closed set enforced in application code (not a
  DB `CHECK`, matching this codebase's existing convention of
  application-level enums over SQL constraints): `pending`, `claimed`,
  `completed`, `expired`, `revoked`.
- `claim_code_hash` / `claim_proof_hash` follow the exact
  `password_hash()`/`verify_password()` idiom `appliance_activation_
  tokens.token_hash` already uses — never stored or logged in plain
  text.
- `revoked_at` and the `appliance_id` back-reference exist now,
  unused by any Phase 1 endpoint, specifically so a future unclaim
  endpoint is an additive `UPDATE ... SET revoked_at=?, status='revoked'
  WHERE appliance_id=?` against a column that already exists — no
  migration needed when that phase starts.
- No change to `appliances`, `appliance_activation_tokens`, or any
  other existing table.

## 3. New files

- **`app/appliance_claims.py`** — new module, mirrors the existing
  `object_storage.py`/`updates_storage.py` pattern (standalone
  business-logic module) plus a `register_appliance_claim_routes(app)`
  function matching the `register_appliance_cloud_routes` /
  `register_cloud_feature_routes` convention already used in
  `main.py`. Owns: device-id format validation, claim-code/claim-proof
  generation, the linear-scan-by-hash lookup described in §5, and all
  five route handlers.
- **`app/tests/test_appliance_claims.py`** — see §7.
- **`appliance-agent/anyaicam_agent/tests/test_portal_claim_client.py`**
  — see §7 (new test file; existing `test_setup_wizard.py`/
  `test_reenrollment.py` are not modified).

## 4. Modified files

- **`app/db_migrations.py`** — append the migration from §2. One line.
- **`app/main.py`** — add `from appliance_claims import
  register_appliance_claim_routes` near the existing
  `from appliance_cloud import register_appliance_cloud_routes`
  (~line 47206), and `register_appliance_claim_routes(app)` immediately
  after `register_appliance_cloud_routes(app, page_shell, current_user)`
  (~line 47321) — same registration-order reasoning already documented
  at that call site (specific routes before generic catch-alls). Two
  lines total.
- **`appliance-agent/anyaicam_agent/portal.py`** — add three thin
  methods to `PortalClient` (`claim_begin`, `claim_status`,
  `claim_complete`), following the existing `test()`/`activate()`
  pattern exactly (same `request()` helper, same `PortalError`
  handling, same `sanitize()` scrubbing before any logging). No change
  to `activate()` itself, no change to `setup_wizard.py` in this phase
  (wiring the wizard to actually call these methods, or building any
  non-interactive entry point that uses them, is deliberately follow-on
  work once this cloud-side foundation lands — see §10).

No other existing file changes. `reenrollment.py`,
`appliance_activation.py`, `appliance_cloud.py`'s existing routes, and
`setup_wizard.py` are all read, not modified.

## 5. Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/appliance/claim/begin` | none (device has no credential yet); new `claim_limiter = RateLimiter(10,300)` per client IP, same pattern as `activation_limiter` | Device requests a claim session |
| `GET /api/appliance/claim/status?claim_session_id=` | none — `claim_session_id` itself is the bearer (128-bit random, unguessable, never logged) | Device polls |
| `GET /api/portal/claims/{claim_code}` | `current_user(request)` — logged-in customer | Portal looks up a claim before showing the confirm UI |
| `POST /api/portal/claims/{claim_code}/confirm` | `current_user(request)` | Customer binds the device to one of *their own* sites |
| `POST /api/appliance/claim/complete` | none (still no credential); reuses `activation_limiter` | Device redeems the proof, gets a permanent credential |

Detail:

**`claim/begin`** — body `{device_id}`. Validates
`device_id` against `^[A-Za-z0-9._:-]{8,128}$`. If an `appliances` row
already exists with `cloud_id=device_id.upper()` and has a live
(non-revoked) credential → `409` (already activated; matches the
existing `ActivationConflict` philosophy). Else looks for an existing
`appliance_claims` row for this `device_id` with `status='pending'`
and `expires_at` still in the future — if found, returns that *same*
`claim_session_id`/`claim_code` unchanged (idempotent resume, never a
second live session per device). Otherwise mints `claim_session_id`
(`secrets.token_urlsafe(24)`), an 8-character human-typeable
`claim_code` (`secrets.token_hex(4)`, uppercased), hashes the code
with `password_hash()`, inserts a `pending` row with `expires_at = now
+ 15 minutes`. Returns `{claim_session_id, claim_code, expires_at,
poll_interval_seconds: 5}`. `claim_code` is returned once here and
never logged (the existing `sanitize()`/`FORBIDDEN`-set convention in
`portal.py` is extended to also strip `claim_code`/`claim_proof`
before any request/response is logged, on both sides).

**`claim/status`** — looks up by `claim_session_id` only. Returns
`{status}` always; when `status='claimed'` and the proof is not yet
expired, also returns `claim_proof` — returned on *every* poll while
in that window (never marked "issued" at read time), because
consumption happens only at `claim/complete`, satisfying the
"idempotent retry" requirement from the state-machine doc's §"at most
one active claim session" note. If `expires_at` has passed and status
is still `pending`, this endpoint itself flips the row to `expired`
lazily (no separate sweep job needed for Phase 1) before responding.

**`GET /api/portal/claims/{claim_code}`** — looks up the *pending,
unexpired* claim whose `claim_code_hash` matches, via a bounded linear
scan (`SELECT * FROM appliance_claims WHERE status='pending' AND
expires_at>?`, then `verify_password()` per row) — deliberately not an
indexed exact-match lookup, so the code itself is never stored or
queryable in plaintext. This is an explicit, bounded tradeoff (see
§8) acceptable because concurrently-pending claims are expected to
number in the tens, not thousands. Returns `{device_id, expires_at}`
only — no customer/site data exists yet to leak. Rate-limited per
authenticated user (reuses `request_limiter`) so guessing codes via
the portal is bounded by the same throttle as any other appliance
endpoint.

**`POST /api/portal/claims/{claim_code}/confirm`** — body `{site_id}`.
Re-does the §"lookup" scan, then requires: claim still `pending` and
unexpired; `site_id` resolves to a `sites` row whose `customer_id`
matches a customer belonging to the authenticated user's own
partner/customer scope (exact boundary check mirrors whatever
`require_partner_access`/`partner_identity` or the plain customer-scope
check `current_user` already exposes elsewhere in `app/main.py` for
site ownership — reused, not reinvented). On success: mints
`claim_proof` (`secrets.token_urlsafe(32)`), hashes it, `UPDATE
appliance_claims SET status='claimed', customer_id=?, site_id=?,
claimed_by=?, claimed_at=?, claim_proof_hash=?, proof_expires_at=?
WHERE id=? AND status='pending'` — the `WHERE status='pending'` clause
plus checking `rowcount==1` is the same double-confirm-before-mutate
pattern `activate_appliance()` uses for token consumption, so a
double-click or retry can't re-claim. `proof_expires_at = now + 5
minutes` (short — the device is expected to be actively polling).
Returns `{status:'claimed', device_id}` — **the proof itself is never
returned to the browser**, only ever delivered to the device via
`claim/status`, so a browser-side leak (XSS, shared screen, browser
history) can't hand out a redeemable credential.

**`claim/complete`** — body `{claim_session_id, claim_proof}`. Looks
up by `claim_session_id`; requires `status='claimed'`,
`proof_expires_at` in the future, and `verify_password(claim_proof,
claim_proof_hash)`. Consumes the proof atomically: `UPDATE
appliance_claims SET status='completed', completed_at=? WHERE id=? AND
status='claimed'`, checking `rowcount==1` exactly like
`activate_appliance()`'s `token_rows` `used_at` guard — a second call
with the same proof gets `409` (`FAILED`/`proof_already_used` in the
state-machine doc's vocabulary), never a second credential. On the
first, successful call: creates the `appliances` row (`INSERT INTO
appliances(id, customer_id, site_id, cloud_id, created_at, ...)
VALUES (...)` — `cloud_id = device_id.upper()`, satisfying its
existing `UNIQUE NOT NULL`), mints the permanent credential exactly as
`activate_appliance()` does (`secrets.token_urlsafe(48)`, insert into
`appliance_credentials`), calls the existing, unmodified
`persist_activation()` from `appliance_activation.py`, and returns a
response with **exactly the same shape** as today's `POST
/api/appliance/activate`: `{appliance_id, cloud_id, credential,
credential_id, partner_id, customer_id, site_id, message}`. This is
the one-time exchange into the existing enrollment/credential
mechanism the task asked for — the agent's `reenrollment.py`
(`validate_activation_response()` → `first_enroll()`) needs zero
changes because it only ever sees this familiar shape, regardless of
which of the two cloud-side paths produced it.

## 6. Authentication/authorization boundaries

| Boundary | Mechanism |
|---|---|
| Device → cloud, before any credential exists (`claim/begin`, `claim/status`, `claim/complete`) | No bearer credential possible yet by definition; protected instead by rate limiting (`claim_limiter`, reused `activation_limiter`), unguessable random session/proof tokens, and short expiries — same trust model RFC 8628 device-grant flows use, and the same one `activate_appliance()` already relies on today for its own unauthenticated `cloud_id`+token pair |
| Customer → cloud (`claim/{lookup,confirm}`) | Existing `current_user(request)` session auth, already passed into `register_appliance_cloud_routes` — reused unchanged |
| Customer → their own site only | Explicit `site_id` ownership check against the authenticated user's own customer scope before `confirm` is allowed to succeed — the one new authorization rule this plan adds, and the one place a bug would let a customer claim a device onto someone else's site, so it gets its own dedicated test (§7) |
| Device → cloud, after `claim/complete` | Unchanged: `authenticate_appliance()`'s existing `X-Appliance-Id`/`X-Request-Timestamp`/`X-Request-Nonce`/Bearer-credential contract, since Phase 1 hands the device a credential in that same existing shape |

## 7. Automated tests

New file `app/tests/test_appliance_claims.py` (`TestClient(main.app)` +
`database_backend.override_target(sqlite_path=...)`, matching this
codebase's established FastAPI test pattern):

1. `claim/begin` with a fresh `device_id` creates a `pending` row and
   returns a code/session id.
2. `claim/begin` called twice for the same still-pending `device_id`
   returns the *same* `claim_session_id`/`claim_code` (idempotent
   resume), not a second row.
3. `claim/begin` for a `device_id` that already has an activated
   `appliances` row → `409`.
4. `claim/status` for an unknown/garbage `claim_session_id` → generic
   not-found response (never distinguishes "wrong id" from "expired",
   to avoid a session-id enumeration oracle).
5. `claim/status` before confirm → `pending`; after confirm → returns
   `claim_proof`; polling again before `claim/complete` returns the
   *same* proof (idempotent).
6. `claim/status` past `expires_at` while still `pending` → lazily
   flips to `expired`, and stays `expired` on subsequent polls.
7. Portal `GET /claims/{code}` with a wrong/mistyped code → `404`,
   indistinguishable in timing/shape from an expired code (best-effort;
   not a hard timing-safety guarantee in Phase 1, called out as a known
   limitation in §8).
8. Portal `confirm` binding to a `site_id` that does **not** belong to
   the authenticated customer → `403` (the dedicated authorization test
   from §6).
9. Portal `confirm` called twice on the same claim (double-click) →
   second call `409`, only one `appliances`/credential row ever exists.
10. `claim/complete` with a correct, unexpired proof → `200`, response
    shape asserted field-for-field identical to today's
    `/api/appliance/activate` response keys, and a real `appliances`
    row now exists with the right `customer_id`/`site_id`/`cloud_id`.
11. `claim/complete` replayed with the same (now-consumed) proof →
    `409`, credential count unchanged (replay protection).
12. `claim/complete` with an expired proof (`proof_expires_at` in the
    past) → `409`, `appliances` row not created.
13. Full happy-path integration test: `begin` → `confirm` → `status`
    (proof appears) → `complete` → resulting credential authenticates
    successfully against the existing `authenticate_appliance()` via a
    normal heartbeat call — proves the exchange into existing
    machinery actually works end to end, not just in isolation.
14. Secret-hygiene test: capture log output (or inspect
    `sanitize_appliance_payload`-equivalent) across the full happy path
    and assert neither `claim_code`, `claim_proof`, nor the final
    `credential` value ever appears in any logged line.

New file
`appliance-agent/anyaicam_agent/tests/test_portal_claim_client.py`:

15. `PortalClient.claim_begin/status/complete` construct the expected
    request shape and surface `PortalError` on non-2xx, matching
    `test()`/`activate()`'s existing test patterns.
16. `sanitize()`'s `FORBIDDEN` set is asserted to also cover
    `claim_code` and `claim_proof` (regression test tied directly to
    the "no secrets in logs" requirement).

Existing test files (`test_appliance_updates_latest.py`, the wizard and
reenrollment tests, etc.) are not modified — nothing in this plan
changes their behavior.

## 8. Known, explicit limitations (honest tradeoffs, not oversights)

- The portal lookup-by-code (§5) is a linear scan over pending claims,
  not an indexed lookup, so the code is never stored in plaintext.
  Acceptable at expected Phase 1 volumes; would need revisiting (e.g.
  a keyed-HMAC indexed column) if concurrent pending claims ever grew
  into the thousands.
- Timing-safety of the "wrong code" vs. "expired code" portal response
  is best-effort, not constant-time-guaranteed, in this phase.
- No background sweep job for expired `pending` claims — cleanup is
  lazy (on next `status`/`lookup` touch). Acceptable because expired
  rows are inert and small in number; a periodic janitor can be added
  later without a schema change.

## 9. Implementation order (small, reviewable commits)

1. **Schema only**: `app/db_migrations.py` migration + a bare
   migration-applies-cleanly test. No routes yet.
2. **`claim/begin` + `claim/status`**, device-facing, with tests
   1–6. No portal/customer-facing code yet — reviewable as "device can
   open and poll a claim session that goes nowhere."
3. **Portal `lookup` + `confirm`**, with tests 7–9. Still no
   `claim/complete` — reviewable as "a customer can bind a device to
   their site" in isolation, claim stuck at `claimed`.
4. **`claim/complete`** + the `persist_activation()` exchange, with
   tests 10–13 (including the end-to-end authenticate-afterward test).
   This is the commit that actually produces a working appliance.
5. **Agent-side `PortalClient` methods** + tests 15–16, plus the
   `sanitize()` `FORBIDDEN`-set extension and log-hygiene test 14.
   Deliberately last and separable — the cloud-side foundation
   (commits 1–4) is independently completed and testable without the
   agent ever changing at all.
6. **Documentation commit** (this plan superseded by a short "Phase 1
   shipped, here's what changed" note, plus updating
   `docs/customer-appliance-readiness-blockers.md`'s "no factory-time
   non-interactive identity/config seeding path" line to reflect that
   the cloud side now exists even though the terminal-session blocker
   for actually *invoking* it from the appliance is still open).

Each commit: full regression suite run and reported (pass/fail/skip
counts, pre-existing vs. new failures distinguished), secret scan
before committing, code and any doc updates in separate commits per
this session's standing convention.

## 10. Rollback boundaries

- Every commit in §9 is additive-only: one new table, one new module,
  two new lines in `main.py`, three new methods appended to
  `PortalClient`. Reverting any single commit (or all of them) deletes
  code and, at most, drops the one new table — `appliances`,
  `appliance_activation_tokens`, `reenrollment.py`, and the existing
  `/api/appliance/activate` path are never touched, so the existing
  admin-driven activation flow keeps working unmodified regardless of
  whether any part of this plan is rolled back.
- No destructive migration exists in this plan (no `ALTER`/`DROP` of
  an existing table), so rollback never requires a data-recovery step
  beyond dropping `appliance_claims` itself, which by design holds no
  data anything else depends on.
- Nothing in this plan is deployed, merged, or run against staging,
  production, Samsung, or live cloud resources — it is presented here
  for approval only, per the task's explicit stop condition.

## 11. Explicit non-actions in this pass

Per the task's constraints: no code was written, no migration was
run, no branch was pushed, and nothing here touched production, shared
staging, Samsung, cameras, or live cloud resources. This document is
the complete deliverable for this task; implementation does not begin
until this plan is reviewed and approved.
