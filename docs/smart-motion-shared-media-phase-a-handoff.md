# Smart Motion shared-media Phase A — handoff (2026-09-14)

This document is a complete, self-contained handoff for the Smart Motion
shared-media feature so a future Claude/Codex session can resume without
reconstructing this session's work. `docs/PROJECT_CHECKPOINT.md` carries
the same information in the project's own running-log style; this file is
the single-topic, fully-detailed version.

## Authoritative state

- **Repo / worktree**: `alexmata25/AnyAICam`, checked out at
  `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-VMS-reconciliation`.
- **Branch**: `reconcile/golden-foundation-20260911` (the authoritative branch
  for this whole engagement).
- **Starting checkpoint this task began from**: `c1e0323`.
- **Starting deployed runtime (Ryzen and staging)**: `818f07fd0c6e4fba0a14723e811871d9b4ab9c73`.
- **Final source commit (this task)**: `48992a5` (source + tests).
- **Final checkpoint commit (this task)**: see `git log` on `docs/PROJECT_CHECKPOINT.md`
  immediately following `48992a5` (checkpoint entry titled *"Smart Motion
  shared-media Phase A implemented, tested, committed (`48992a5`)..."*).
- **Working tree status at handoff**: clean except this handoff file and the
  checkpoint-doc commit itself, both about to be committed.
- **Deployment status**: **NOT deployed anywhere.** Staging (`anyaicam-staging`)
  and Ryzen are both still running `818f07fd0c6e4fba0a14723e811871d9b4ab9c73`.
- **Environment/infra touched this task**: none. No staging deploy, no Ryzen
  artifact build/install, no AWS/S3 configuration change, no Samsung access.
  `RECORDING_UPLOAD_ENABLED` was not read or written by any command this task
  ran, and remains `false` everywhere per every prior checkpoint.

## What Smart Motion Phase A was intended to fix

Commit `818f07f` (previous session) proved that a correlated Smart Motion
event could reuse its base Motion event's own encoded clip instead of
independently re-encoding and re-uploading identical footage — the
encode/upload dedup itself worked exactly as designed and is not touched by
Phase A. But real production validation of that design (also previous
session) found the *registration* half was completely broken: the appliance
sent the base Motion event's own S3 key while registering media under the
Smart Motion event's own id, and the cloud's pre-existing anti-spoofing check
(`appliance_cloud.py`'s `analytics_event_media_available()`, which requires a
submitted key to be *derived from the registering event's own id*) correctly
rejected every attempt with a real, repeated HTTP 403. Neither of two fresh,
naturally-occurring Camera 1 Smart Motion events produced in that validation
ever got a `detection_event_media` row.

An independent security review (Codex, static read-only review of checkpoint
`c1e0323`/runtime `818f07f`) was then requested and returned **APPROVE WITH
CHANGES**: the shared-media *architecture* (one physical clip, two
independently-owned media rows) is sound, but the specific 818f07f
implementation was incomplete at the cloud trust boundary. Full findings are
preserved in this repo's own session history; the reconciled summary and
design decisions are captured below. Phase A is the implementation of that
reconciled, approved design.

## Files changed (commit `48992a5`)

| File | What changed |
|---|---|
| `app/db_migrations.py` | Two new additive columns (see Schema below), appended to `apply_migrations()`'s existing idempotent-column-check block. No historical migration rewritten. |
| `app/appliance_cloud.py` | New helper `_resolve_parent_motion_event()`; `analytics_event_available()` (`/events`) extended to resolve/freeze/conflict-check the parent correlation; new route `analytics_event_media_shared()` (`/events/{id}/media/shared`); existing `analytics_event_media_available()` (`/events/{id}/media`) **unchanged**. |
| `app/event_media_policy.py` | `daily_seconds_used()` excludes rows with `source_media_id IS NOT NULL`. |
| `app/recording_retention_sweep.py` | `_expired_candidates()` restricts event-media candidates to root rows; `run_retention_sweep_tick()` cascades a root's deletion to its shared siblings, only after the root's own S3 delete is confirmed. |
| `app/analytics_sync.py` | `_build_payload()`'s outgoing wire field renamed `motion_event_id` → `parent_local_event_id` (7 fields now, was 6). The *local* Python dict key in `main.py`, `motion_event_id`, is unchanged. |
| `app/event_media_uploader.py` | `register_shared_event_media()` redesigned: new signature `(event_id, camera_number, parent_local_event_id)`, posts to the new `/media/shared` route, now durable via the existing `event_media_outbox`. `retry_pending_event_media()` branches on the outbox entry's own `kind` field. |
| `app/main.py` | Smart Motion media task (`store_motion_event()`) calls `register_shared_event_media()` with only the base Motion event's own local id; comments updated. |
| `app/tests/test_shared_smart_motion_media_authorization.py` | **New**, 17 tests — real `TestClient` cloud-route contract test for the whole feature. |
| `app/tests/test_shared_event_media_registration.py` | Rewritten, 12 tests — appliance-side function unit tests for the new contract. |
| `app/tests/test_smart_motion_event_media_wiring.py` | Revised, 11 tests — `main.py` integration tests updated for the new call shape. |
| `app/tests/test_recording_retention_sweep.py` | +3 tests — shared-row deletion-candidate exclusion and cascade-only-after-confirmed-delete behavior. |
| `app/tests/test_event_media_policy_shared_accounting.py` | **New**, 3 tests — daily-usage exclusion and the shared route never calling `allows_event_media()`. |
| `app/tests/test_analytics_sync.py` | +1 test, 1 renamed — seven-field payload allowlist and `parent_local_event_id` forwarding. |

No other files were touched. `docs/PROJECT_CHECKPOINT.md` and this handoff
file are committed separately as documentation-only commits.

## Database / schema changes

Both additive, both via the existing idempotent `PRAGMA table_info(...)` /
`information_schema.columns` (SQLite / PostgreSQL) check-then-`ALTER TABLE`
pattern already established at the end of `apply_migrations()` — no new
versioned migration entry, no historical migration rewritten.

```sql
ALTER TABLE detection_events ADD COLUMN parent_detection_event_id TEXT
    REFERENCES detection_events(id);
CREATE INDEX IF NOT EXISTS idx_detection_events_parent
    ON detection_events(parent_detection_event_id);

ALTER TABLE detection_event_media ADD COLUMN source_media_id TEXT
    REFERENCES detection_event_media(id);
CREATE INDEX IF NOT EXISTS idx_detection_event_media_source
    ON detection_event_media(source_media_id);
```

- `parent_detection_event_id` is a **cloud id → cloud id** foreign key —
  resolved exactly once, at analytics-ingestion time, from an
  appliance-submitted **local** id (`parent_local_event_id`), and frozen from
  then on. `NULL` for every event except a real `smart_motion` one whose
  claimed Motion parent has already been independently resolved. A
  `smart_motion` event with this column still `NULL` is **permanently
  ineligible** for the shared-media route until a later resync resolves it —
  never inferred from timestamps or filenames, never retrofitted onto a
  legacy row.
- `source_media_id` is the reference/owner distinction: `NULL` for a root row
  (a real upload), set to that root row's own `id` for a shared/child row.
  Both the retention sweep and daily-usage accounting depend on this.
- `detection_event_media.s3_key` still carries **no uniqueness constraint**
  (confirmed against the live schema before this work began) — two
  independent rows safely referencing one immutable object was already a
  fully supported shape; this column just makes the relationship explicit
  and queryable instead of only inferable by matching key strings.

## Cloud / API changes

### `POST /api/appliance/analytics/{camera_id}/events` (existing route, extended)

Accepts an optional `parent_local_event_id` field, meaningful only when
`event_type == 'smart_motion'`.

- **Fresh insert**: resolves `parent_local_event_id` via
  `_resolve_parent_motion_event(db, camera_id, appliance_id, parent_local_event_id)`
  — scoped to *this exact camera* + *this exact authenticated appliance* +
  parent `event_type == 'motion'`. Returns `None` (never raises) when
  unresolvable; the child event still syncs normally either way.
- **Replay of an existing `local_event_id`**:
  - `event_type` or `event_timestamp` mismatch → **409** (event identity is
    now immutable on replay — this is a new check; previously any replay
    with the same `local_event_id` was silently treated as `duplicate`
    regardless of content).
  - Existing `parent_detection_event_id` is `NULL` and this replay now
    resolves one → **allowed, one-time completion** (the "child synced
    before its parent existed" self-healing case).
  - Existing `parent_detection_event_id` is already set and this replay
    resolves to a **different** id → **409** (frozen once set).
  - Otherwise → `200 {"status": "duplicate", ...}`, unchanged.

### `POST /api/appliance/analytics/{camera_id}/events/{local_event_id}/media/shared` (new route)

Request body: **`{"parent_local_event_id": "..."}` only.** No `s3_key`,
`thumbnail_s3_key`, bucket, timing, duration, or size field exists in this
route's schema at all — an appliance cannot supply an arbitrary storage
reference here even by mistake.

All of the following inside one transaction:

1. Authenticate appliance; `_authorized_camera()` (current assignment,
   unchanged existing helper).
2. Load child `detection_events` row (`camera_id` + `local_event_id`):
   - not found → 404 (scoped, never discloses cross-tenant existence).
   - `event_type != 'smart_motion'` → 403.
   - child's own **stored** `appliance_id != authenticated appliance.id` →
     403 (closes the historical-reassignment gap: a camera reassigned to a
     new appliance cannot resurrect an old event that belonged to a
     different one).
   - child's stored `customer_id`/`site_id` don't match the camera's
     **current** `customer_id`/`site_id` → 403.
   - `parent_detection_event_id IS NULL` → **409** `parent_media_pending`
     (retryable, documented reason).
3. Re-resolve the **submitted** `parent_local_event_id` independently; it
   must equal the child's own already-frozen `parent_detection_event_id` →
   else 403 (a request can never silently reassign a different parent).
4. Load the parent row by that resolved id; verify `event_type == 'motion'`,
   `camera_id` matches the route's camera, `appliance_id` matches the
   authenticated appliance, and `customer_id`/`site_id` match the child's own
   → else 403 (full ownership-chain re-verification, defense-in-depth beyond
   step 1's ingestion-time resolution).
5. Load the parent's own `detection_event_media` row; absent → **409**
   `parent_media_pending`.
6. Existing child media row: same `source_media_id` → `200 duplicate`, **zero
   mutation**; different → 409 conflict.
7. Otherwise `INSERT` a new child `detection_event_media` row, copying
   `s3_key`/`thumbnail_s3_key`/`started_at`/`ended_at`/`duration_seconds`/
   `size_bytes` **verbatim from the parent's own row** — never from the
   request — plus `source_media_id = parent_media.id`. Uses the same
   try-INSERT/fallback-to-SELECT idiom `analytics_event_available()` already
   uses for concurrency-safe idempotent replay (portable across SQLite and
   PostgreSQL unique-violation exceptions).

### `POST /api/appliance/analytics/{camera_id}/events/{local_event_id}/media` (existing self-key route)

**Completely unchanged.** Same deterministic-key derivation, same behavior,
for ordinary Motion and every AI/YOLO class.

## Appliance changes

- `analytics_sync._build_payload()`: outgoing wire field renamed
  `motion_event_id` → `parent_local_event_id`, populated only when the local
  event dict has a `motion_event_id` key (only ever true for a real
  `smart_motion` event created by `main.py`'s `store_motion_event()`). Local
  Python field name in `main.py` is unchanged — this rename is purely at the
  wire-payload level, to make "this is a local id, not a cloud id" explicit
  in the API contract.
- `main.py`'s Smart Motion media task (inside `store_motion_event()`): still
  `await`s `clip_task` (the base Motion event's own already-scheduled media
  task) — now purely as the "did the base event's own media succeed" truthy/
  falsy signal, not to extract any storage value from it. On success, calls
  `register_shared_event_media(event_id=smart_event_id, camera_number=...,
  parent_local_event_id=<base Motion event's own local id>)`. Safe-failure
  behavior is unchanged: a falsy `clip_task` result logs and returns, never
  triggering an independent Smart Motion re-encode; the Smart Motion
  analytics event itself was already persisted earlier regardless.
- `event_media_uploader.register_shared_event_media()`: new signature
  `(*, event_id: str, camera_number: int, parent_local_event_id: str) -> bool`.
  Sends only `{"parent_local_event_id": ...}` to the new route. No S3
  credential, session, or client of any kind is used.

## Durable shared-registration / outbox behavior

`register_shared_event_media()` now writes to the **same**
`event_media_outbox.json` a base upload already uses, extended with an
optional `"kind"` field:

- **`"kind"` absent** (every pre-existing entry, and every entry a base
  Motion/AI-YOLO upload still writes): routed to `upload_motion_event_media()`
  exactly as before — **completely unaffected**.
- **`"kind": "shared"`** (new): entry shape is
  `{event_id, camera_number, kind: "shared", parent_local_event_id, attempts,
  next_attempt_at}` — no `clip_url`, since there's no local artifact to
  protect. Written **before** attempting registration (so a crash mid-call is
  recoverable), removed only on a confirmed `accepted`/`duplicate` response.
  `retry_pending_event_media()` routes `"shared"` entries to
  `register_shared_event_media()` alone — this recovery path **never
  encodes or uploads anything**, ever.

This closes the "lost child registration" gap Codex identified: a process
restart, exhausted initial retries, or a parent whose own media wasn't ready
yet all leave a durable, safely-recoverable intent behind instead of silently
and permanently losing the registration.

## Accounting changes

- `event_media_policy.daily_seconds_used()` adds `WHERE dem.source_media_id
  IS NULL` — only root media rows count toward a camera's daily allowance. A
  shared row references bytes a root already accounted for once and must
  never be charged again.
- The new `/media/shared` route **never calls `allows_event_media()`** at
  all — referencing already-accounted footage is not new physical usage, so
  there is nothing to gate a second time. (Verified by a source-level test
  asserting the function name never appears in that route's own source
  text.)
- Existing plan/entitlement values themselves are untouched.

## Retention / lifetime changes

Implements the user's explicit ordering clarification for this task: **a
database record may never be removed ahead of its own confirmed physical
deletion.**

- `_expired_candidates()`'s `detection_event_media` query is now restricted
  to `WHERE source_media_id IS NULL` — a shared row can never independently
  select itself as a deletion candidate, and never gets its own
  `_delete_recording_object()` call (it owns no S3 object of its own — only a
  reference to its root's).
- `run_retention_sweep_tick()`: once a root's own S3 delete is *confirmed*
  successful, every `detection_event_media` row sharing that root's id via
  `source_media_id` is deleted in the **same transaction** as the root's own
  row. If the S3 delete is **not** confirmed, the loop `continue`s —
  **neither the root nor any of its siblings is touched**, and both remain
  intact for the next tick's retry.
- This sweep (`recording_retention_sweep.py`) is currently **disabled** by
  its own flag (`ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED`, default
  `false`, confirmed off) — this fix closes a latent, not-currently-active
  hazard in a mechanism this feature's own new column would otherwise have
  left exposed if the sweep were ever enabled.

## Security / ownership protections implemented

- Parent/child correlation is **cloud-resolved and frozen**, never trusted
  from a later request.
- Full ownership-chain verification on the new route: same camera, same
  appliance (via each row's own **stored** `appliance_id`, not just current
  camera assignment — this is what closes the historical-reassignment gap),
  same customer/site, correct event types on both sides.
- The shared request **cannot supply any storage reference at all** — its
  schema has no field for one. The cloud derives every approved value from
  the verified parent's own already-registered media.
- Non-mutating idempotent replay: an identical retry returns the same media
  id with zero row mutation; a genuinely conflicting replay is a 409, never
  a silent overwrite.
- Existing ordinary Motion/YOLO deterministic-key route is **completely
  unchanged** — same anti-spoofing check, same behavior.

## Focused test results

All targeted files run together, immediately before the full regression:

```
197 passed, 1 warning in 24.47s
```
(`test_shared_smart_motion_media_authorization.py`,
`test_shared_event_media_registration.py`,
`test_smart_motion_event_media_wiring.py`, `test_event_media_uploader.py`,
`test_event_media_cloud_flow.py`, `test_event_media_local_capture.py`,
`test_motion_event_media_wiring.py`, `test_ai_event_clip_wiring.py`,
`test_analytics_sync.py`, `test_recording_retention_sweep.py`,
`test_event_media_policy_shared_accounting.py`.)

## Full regression results and comparison with the established baseline

```
app/ 86 failed, 1828 passed, 22 skipped, 445 warnings in 448.40s (0:07:28)
```

The complete sorted `FAILED` list was diffed byte-for-byte against the
established baseline captured at the `818f07f`/`c1e0323` checkpoint (86
failed / 1800 passed) — **the diff was empty**. 1828 = 1800 + 28 new tests.
**Zero new regressions.** The 86 pre-existing failures are the same
already-documented, unrelated, environment-specific failures this project
has tracked across many prior checkpoints (AWS_REGION-dependent tests,
known test-order-dependent flakiness, etc.) — none in any file this task
touched.

## Final commit hashes

- **Source + tests**: `48992a5` — "fix: Smart Motion shared-media Phase A —
  persisted cloud correlation, parent-id-only registration, full
  ownership-chain authorization"
- **Checkpoint doc**: committed immediately after `48992a5` on the same
  branch (see `docs/PROJECT_CHECKPOINT.md`'s own entry dated 2026-09-14,
  titled to match).
- **This handoff document**: committed alongside the checkpoint-doc commit.

## Working-tree status

Clean at handoff, aside from the checkpoint-doc + this-handoff-doc commit
itself. No other uncommitted changes.

## What was NOT changed

- `EVENT_CLIP_ENCODE_MAX_CONCURRENCY` — untouched, still whatever it was
  (unset/default `1` on Ryzen last confirmed).
- Live Relay, ffmpeg thread settings — untouched.
- AI/YOLO's own independent clip-dedup logic (`save_yolo_events()`'s own
  `event_group_id`/primary-class-per-scan mechanism) — untouched, and its own
  existing tests (`test_ai_event_clip_wiring.py`) pass unmodified.
- Historical recording upload (`recording_uploader.py`), Playback, RDM,
  entitlements, AWS/S3 IAM policy documents — untouched.
- The existing self-key `analytics_event_media_available()` route — byte-for-
  byte unchanged in behavior; its own tests (`test_event_media_cloud_flow.py`)
  pass unmodified.
- Smart Motion's own correlation logic, classes, or correlation window
  (`smart_motion.py`) — untouched.
- Samsung — not accessed at all this task.

## Confirmation: staging / Ryzen / Samsung / AWS untouched

No SSH session to `anyaicam-staging` or `ryzen-tailscale` was opened during
this implementation task. No `docker build`/`docker run` on either host. No
AWS CLI or boto3 call of any kind was made. No Samsung access. This task was
entirely local source implementation, local test execution, and local git
commits.

## Deferred to Phase B (real findings, not implemented, with justification)

1. **Parent-upload exactly-once retry** — `upload_motion_event_media()`'s own
   retry can re-PUT to S3 after a successful upload but a failed catalog
   registration call. Pre-existing, unrelated to sharing specifically,
   touches the base Motion/AI-YOLO path too — a separate, non-trivial change
   (idempotent PUT via conditional headers or a completion-marker file).
2. **The existing self-key route's own "duplicate mutates" behavior** — Codex
   confirmed `analytics_event_media_available()` unconditionally overwrites
   `s3_key`/`thumbnail_s3_key`/timing/size on any replay. The new shared
   route does not inherit this (its own replay handling is non-mutating by
   design), but the *existing, unrelated* ordinary-Motion/AI-YOLO route's own
   behavior was deliberately left untouched — changing it is a separate
   decision with its own blast radius, not required for shared media to work
   correctly.
3. **Verifying actual S3 bucket versioning / Object Lock configuration** —
   `recording_credentials.py`'s `event_media_session_policy()` grants
   `s3:PutObject` only (no `s3:DeleteObject`), so the appliance itself cannot
   delete an object — but nothing in this codebase enforces conditional
   writes or Object Lock, so an in-place overwrite via a second `PutObject`
   from a credential that *does* have write access is not prevented at the
   storage layer. This was not assumed either way and needs actual AWS
   access to verify — not done this task, and not a Phase A blocker (sharing
   doesn't increase the parent object's own mutability exposure beyond what
   it already has standalone).
4. **Independent detector-attestation / proof of physical causality against a
   compromised appliance** — a fundamentally larger trust-model problem
   (hardware attestation or independently-generated correlation). No
   authorization-layer design can solve this; the recommendation throughout
   is conditional on the appliance being trusted to report detections, same
   as every other event type in this system today.
5. **A reconciliation policy for legacy Smart Motion rows with no persisted
   parent relationship** — explicitly left as a separate decision for the
   user. These rows remain permanently unshareable; nothing retrofits them.

## Bugs / concerns / deviations from the approved design

None identified. The implementation matches the approved design report
section-by-section, including the one explicit clarification (S3-delete-
before-DB-delete ordering for the retention sweep). Two minor test-authoring
mistakes were made and self-caught during this task (comparing against the
wrong id field in one assertion; two fake functions in the retry-worker test
not replicating the real functions' own `event_media_outbox.remove()` side
effect) — both were fixed before the final full regression run and are not
present in the final committed test files.

One open, unresolved *question* (not a bug): whether the two real Smart
Motion events from the prior session's validation
(`a45fdd02900c`/`297649df7ab0`, synced under the OLD six-field payload before
this fix existed) should ever be reconciled. Per the approved design and
Codex's own explicit instruction, they are **not** retroactively resolved —
their `parent_detection_event_id` will remain `NULL` forever unless a
separately-approved reconciliation policy is designed later. This is
intentional, not an oversight.

## Exact next recommended action

1. **Deploy to staging** (`anyaicam-staging`) using the established
   versioned procedure (pre-deploy DB+source backups, source tarball hash
   verified before/after transfer, `deploy-portal:48992a5` built, env-drift
   check, `portal-green` recreated, post-deploy health/byte-hash/DB-integrity/
   row-count verification) — cloud-only pieces of this fix take effect
   immediately; the edge-side pieces (main.py, event_media_uploader.py,
   analytics_sync.py) will not functionally activate on staging itself
   (`runtime_role=cloud`), matching this project's own established pattern
   for edge-only fixes deployed to staging "for source-commit consistency."
2. **Build and stage the Ryzen artifact** (`installer/build_release_installer.py
   --vms-commit <full 48992a5 SHA> --vms-repo .`), hash-verify before and
   after transfer, independently confirm the artifact contains the new code
   (e.g. `grep` for `parent_local_event_id`/`register_shared_event_media` in
   the extracted payload), stage at `~/anyaicam-install-<short-sha>/` on
   Ryzen. **Do not run the install** — that is always the operator's own
   `sudo ./install.sh --repair`.
3. Once both sides are confirmed healthy on the new build (the same
   POST-INSTALL PASS checklist pattern used for every prior deploy this
   engagement), **do the controlled real-data validation**: wait for a fresh,
   naturally-occurring Camera 1 Motion + Smart Motion pair (do not force one)
   and confirm the full chain end to end — two independent analytics events,
   two independent `detection_event_media` rows, the SAME `s3_key`/
   `thumbnail_s3_key`, exactly one physical encode/upload, both events'
   media retrievable through the normal authorized customer path, valid
   playable clip and valid thumbnail. This is exactly the validation the
   prior session attempted and found blocked — it should now succeed.
4. Only after that real-data validation succeeds, consider whether/when to
   revisit the Phase B items above, and separately, whether/when to begin
   the AI/YOLO event-media workload optimization (a much larger, distinct
   piece of work — see the Future Work section below).

**None of the above is authorized to begin automatically.** Each deploy step
follows this project's own standing "stop and report, wait for approval"
discipline.

---

## Future work (recorded for context only — NOT authorization to implement)

These are broader product-area findings and priorities established over the
course of this engagement, preserved here so a future session has the full
picture without re-deriving it. **None of these are approved for
implementation** — they require their own separate scoping and explicit
approval, exactly like every other piece of work in this project.

- **AI/YOLO event-media workload**: OPTIMIZE. The event-media clip-encoding
  backlog diagnosis earlier this engagement found AI/YOLO clip-building
  accounts for roughly 80% of total encode-queue volume (vs. ~4% for Smart
  Motion) — the dominant, still-unaddressed contributor to appliance CPU
  pressure. The right direction is very likely to **investigate and reuse
  eligible Motion footage** the same way Smart Motion now does, rather than
  simply raising `EVENT_CLIP_ENCODE_MAX_CONCURRENCY` (measured to be unsafe
  given the appliance's current lack of spare CPU headroom, dominated by
  Live Relay's own real-time re-encodes).
- **LPR**: needs development first — not yet in a testable state.
- **People Counting**: built, but needs fixes/optimization before customer
  testing, including a duplicated-YOLO-inference concern that should be
  investigated before further work.
- **PPE**: needs development before testing.
- **Talk Down**: a real browser → cloud → appliance → Hikvision ISAPI
  implementation exists, but the microphone → camera-speaker hardware
  operation itself remains unproven end-to-end.
- **Playback thumbnails**: wired and ready for a controlled validation pass
  (not yet performed).
- **Playback cursor / infinite scrolling**: needs fixes.
- **Investigate**: needs fixes.
- **Notifications**: needs fixes and real delivery validation.
- **RDM**: a real fleet-management foundation exists, but it is not yet the
  production subscription/feature ON/OFF control plane. Desired-state vs.
  actual-state convergence, disable/removal behavior, and tenant-scoping
  issues all need work before RDM can serve that role.
- **Facial Recognition**: remains a separate integration/development gap,
  not started.
- **P2P Live View**: remains future work. The existing S3/CloudFront Live
  Relay architecture is the proven, working fallback and should remain the
  baseline until/unless P2P is separately scoped.
