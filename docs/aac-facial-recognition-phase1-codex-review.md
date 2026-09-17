# AAC Facial Recognition -- Phase 1 Codex Review / Hardening Pass

This is an independent review-and-hardening pass over the Phase 1 AAC work
recorded in `docs/aac-facial-recognition-phase1.md`. It found and fixed two
real security issues, closed a latent embedding-compatibility bug, improved
the default embedding's discriminative power, added 79 new adversarial/
empirical/performance tests (204 AAC-specific tests total, up from 162), and
produced evidence-based answers to every question the review was asked to
answer. Nothing was merged, deployed, or touched outside this branch.

## 1. Architecture review

Re-read `facial_recognition.py`, `relay_control.py`, `facial_people.py`,
`facial_events.py`, `facial_recognition_ui.py`, the migration, the
`camera_analytics_entitlements` integration, and the `save_yolo_events()`
hook with an adversarial eye.

**Finding: architecture is correctly reused, not parallel.** Concretely
verified:

- Auth/session: every AAC route goes through `partner_portal.partner_identity()`
  and `partner_db.allowed()` -- the same functions `partner_workspace.py`/
  `customer_platform.py` already use. There is no second token format, no
  second cookie, no second role table.
- Tenancy: `customer_id` is a real column with a real `FOREIGN KEY(customer_id)
  REFERENCES customers(id)` constraint on every AAC table, resolved through
  the same `customers`/`sites`/`cameras` tables every other feature uses.
- Events: AAC does not invent a new event table family -- `detection_events`/
  `detection_event_media` are reused directly (`event_type='facial_recognition'`),
  with `facial_events` following the exact `detection_event_media` precedent
  of a 1:1 detail table keyed by `detection_event_id`.
- Licensing: `facial_recognition` is a `camera_analytics_entitlements.analytic_key`
  value like `ppe`/`lpr`/`people_counting`, gated through the same
  `assign_entitlement()`/`ANALYTIC_LABELS` mechanism in `customer_analytics_panel.py`.
- Detection hook: sits in the same `class_name == "person"` branch as the
  PPE hook, using the same crop-then-call-then-append shape.

No parallel system was created anywhere in this feature.

## 2. Security / tenancy audit

Two real, concrete findings from this pass, both fixed:

1. **Unsanitized `customer_id` in a filesystem path.** `_save_face_crop()`
   built `AAC_FACES_FOLDER / customer_id / person_id` directly from a
   client-suppliable `customer_id` (for staff roles). In practice,
   `facial_people.enroll_person()`'s real foreign-key constraint against
   `customers(id)` already made a path-traversal payload (e.g. `../../etc`)
   unreachable with a working request -- but that protection was implicit
   and backend-dependent, not asserted by this module itself. **Fixed:**
   `_resolve_customer_id()` and `_save_face_crop()` now both run every id
   through `_require_safe_path_segment()` (`^[A-Za-z0-9_-]+$`), rejecting
   anything else with 400 before it can reach a query or a path. Verified
   compatible with every real id format already used in this codebase
   (`secrets.token_hex()`-style values, `cust-1`-style test slugs).
2. **Stored XSS across roles.** People/Watchlists/Facial Events/Match Detail
   pages interpolated server data (`display_name`, `external_reference`,
   watchlist `name`, `matched_person_name`, `matched_watchlist_name`, etc. --
   all free text a `facial.manage` user can set) into `innerHTML` template
   literals without escaping. A compromised or malicious `customer_owner`
   session could inject markup that executes in an `administrator`'s or
   `partner_owner`'s browser when they view the same tenant's data. The
   Match Detail page's server-rendered `event_id` attribute had the same
   gap. **Fixed:** every page now defines and uses an `aacEsc()` HTML-escape
   helper on every dynamic interpolation; the server-side `event_id`
   attribute is now `html.escape()`-d. Regression-guarded by both a static
   source-scan test (the exact previously-vulnerable unescaped patterns
   must never reappear) and a live-request test proving a crafted `event_id`
   is never reflected unescaped.

Everything else audited came back clean, with adversarial tests added to
prove it, not just assert it:

- **Customer/site/camera isolation:** every `facial_people.py`/
  `facial_events.py` function takes an explicit `customer_id` and scopes
  every query by it; cross-tenant person/watchlist/event/thumbnail access is
  tested and rejected (existing tests plus new ones for the thumbnail route
  and cross-tenant watchlist-membership attempts at the route layer).
- **`facial.view`/`facial.manage` permissions:** every route checked;
  extended parametrized coverage confirms `customer_viewer` is rejected on
  every mutating route (update, create watchlist, update settings), not just
  the two originally tested (enroll, delete).
- **Enrollment/deletion authorization:** unauthenticated and under-privileged
  attempts rejected (401/403); confirmed unchanged by this pass.
- **Watchlist authorization:** a `customer_owner` cannot add another
  tenant's person to their own watchlist -- verified end-to-end through the
  real route (404, and the membership list stays empty), not just at the
  `facial_people.py` layer.
- **Cross-tenant search prevention:** `list_people`/`list_events`/
  `enrolled_embeddings_for_matching` all scope by `customer_id`; a live
  matching pass for one customer's camera never sees another customer's
  enrolled embeddings (already covered in Phase 1; re-verified here).
- **CSRF:** unchanged. AAC routes authenticate via the same
  `partner_portal` session cookie (`SameSite=Strict`, `HttpOnly`) every
  other partner-portal-style route already relies on for CSRF protection;
  no new CSRF-sensitive surface was added, and nothing about the existing
  mechanism was touched.
- **Safe file paths:** see finding 1 above -- now explicit, not implicit.
- **Thumbnail access:** was a genuine functional gap in Phase 1 (files were
  written to disk but nothing ever served them, and the Match Detail page
  didn't render one). **Added** `GET /api/aac/events/{event_id}/thumbnail`,
  permission- and tenant-checked, which resolves the file path only from an
  already-tenant-scoped `facial_events` row (never from client input) --
  structurally immune to path traversal regardless of what `event_id` looks
  like. Wired into the Match Detail page.
- **Audit logging:** enrollment, update, deletion, image add/delete,
  watchlist create/delete/membership changes, and settings updates are all
  audited (unchanged from Phase 1; re-verified).
- **Secret-free logging:** re-verified with the existing static AST scan
  (extended to cover the new thumbnail route) and the dynamic audit-log
  content check. No embedding vectors, no raw uploaded image bytes, no
  base64 payloads appear in any log or audit record.

## 3. Database

**SQLite:** re-verified. Create/migrate, indexes, the `facial_watchlists`
`UNIQUE(customer_id, name)` constraint, `ON DELETE CASCADE` for
`facial_embeddings`/`facial_watchlist_members`, tenant scoping, and
migration idempotency (`apply_migrations()` run twice) are all covered by
`test_facial_recognition_migration.py` and pass.

**PostgreSQL:** **a live disposable PostgreSQL instance could not be
provisioned in this sandbox** -- Docker Desktop's CLI is present but its
daemon is not running and the application itself is not installed at its
standard path, and no standalone `postgres`/`pg_ctl`/`psql` binary exists on
this machine. Installing Docker Desktop or a PostgreSQL server mid-review
was judged out of scope (a slow, disk/system-changing action not clearly
authorized by "safe synthetic" testing). In its place:

- The real, psycopg-based `PostgreSQLFacialMigrationTests` class (gated on
  `ANYAICAM_TEST_POSTGRES_URL`, mirroring `test_customer_registration_postgresql.py`'s
  own established pattern exactly) exists and is ready to run the moment a
  disposable database is available -- confirmed to skip cleanly, not error,
  in this environment.
- An always-on static check runs every migration statement through
  `database_backend._postgres_sql()` (the real function production uses to
  translate SQLite DDL to PostgreSQL) and asserts no SQLite-only syntax
  survives.
- A manual statement-by-statement trace of the migration (this review) found:
  no reserved-word collisions, no SQLite-only types, `ON DELETE CASCADE` is
  valid PostgreSQL, `CREATE UNIQUE INDEX IF NOT EXISTS` is valid PostgreSQL
  (9.5+), and every table's foreign-key dependencies are satisfied by the
  statement order within the migration (each table is created only after
  every table it references).

This is real confidence, not a rubber stamp, but it is **not** the same as
a live server run. Treat PostgreSQL as "reviewed, not executed" until
`ANYAICAM_TEST_POSTGRES_URL` is set against a real disposable database.

## 4. Face engine review

Current engine: `HaarEmbeddingFaceEngine` (OpenCV Haar detection + a
deterministic intensity-vector embedding). **This review changed the
embedding formula** (see below) after empirical testing surfaced a real
discriminative-power problem with the original one; both are covered here.

**Methodology:** `test_facial_recognition_engine_limits.py` measures real
cosine-similarity numbers against structured synthetic patterns (never real
face photos) under controlled lighting/rotation/scale transforms.

**Finding, fixed:** the original (version 1) embedding computed cosine
similarity on a raw, un-centered pixel-intensity vector. Measured result:
two **clearly different** synthetic images (a structured face-like pattern
vs. a flat gray canvas) scored **~0.85 similarity** -- well above this
project's default 0.6 confidence threshold. This is a real false-positive
risk: cosine similarity on an un-centered vector is dominated by overall
mean brightness, not spatial shape. **Fix:** `embed_face_crop()` now
mean-centers the vector before L2-normalizing (bumped to `version="2"`),
which removes the brightness-driven baseline and makes an inverted version
of the same structured pattern score below 0.8, as it should. This also
surfaced and fixed a latent bug: embeddings were only ever compared by
`engine` name, never `engine_version` -- meaning a future embedding-format
change (like this one) would have silently compared old and new embeddings
as if compatible. `match_face()`/`EnrolledEmbedding` and
`enrolled_embeddings_for_matching()` are now version-scoped; `facial_events`
also now records `engine_version` per match for audit fidelity.

**Assessed sensitivities (measured, synthetic images only):**

| Factor | Measured behavior |
|---|---|
| Lighting | Moderate brightness change (×0.7): similarity stays > 0.85 (equalization does its job). Severe underexposure (×0.05): measurably worse than the moderate case -- real information loss, not recoverable by equalization. |
| Pose/rotation | 5° rotation: similarity stays > 0.7. 45° rotation: drops below 0.6 -- **this embedding has no rotation invariance at all** (it's a raw, position-ordered pixel vector); a real off-axis face will score far worse than a frontal one. |
| Scale/distance | Mild downscale-then-restore: similarity > 0.8. Severe downscale (postage-stamp-sized, simulating a distant face): measurably worse -- detail lost before the fixed-size resize can recover it. |
| Multiple faces | Two different face regions in one frame produce two independently-embedded, non-blended vectors (verified). |
| False-positive risk | After the mean-centering fix, two structurally different patterns score < 0.8; two same-layout patterns with only noise differing score > 0.8 (correctly recognized as "the same layout"). Before the fix this distinction did not reliably hold. |
| False-negative risk | Any of the above degradations (heavy underexposure, large rotation, severe distance) can push a TRUE match's similarity below threshold -- an honest miss (reported as "unknown"), never a wrong identity, per `classify_match()`'s own design. |

**production-grade recognition today: NO.** This is a classical-CV
development baseline. It has zero pose/rotation invariance and no learned
facial-feature representation; it distinguishes clearly different
structured patterns reasonably well after this fix, but should not be
relied on for real access-control decisions without a stronger engine.

**Recommended Phase 2 engine:** an ONNX-based deep face embedding (e.g. an
ArcFace-family model) run through `onnxruntime`'s `CPUExecutionProvider` --
still zero mandatory GPU/CUDA dependency (matches this codebase's own
LPR precedent of an *optional* heavier dependency, loaded lazily and only
when actually enabled), with a real face-alignment step (eye/landmark-based
rotation-and-scale normalization) before embedding, which is what would
actually fix the pose sensitivity measured above -- a normalization tweak
alone cannot. `FaceEngine`/`get_engine()` are already built as the seam this
plugs into; no caller changes needed. GPU acceleration (an
`onnxruntime` CUDA provider, or dedicated AAC Face Pro hardware later) can
sit behind the same interface without another rewrite.

## 5. Access-control safety

**Relay remains fully dormant.** Re-verified plus newly added:

- `relay_control.py` contains exactly two `RelayProvider` subclasses:
  the abstract base and `MockRelayProvider` -- a new structural test
  (`test_no_hardware_is_touched_by_relay_evaluation`) asserts this directly
  against the module's own contents, so a future hardware-backed provider
  class cannot be added without that test (deliberately) needing to be
  updated in the same change.
- `MockRelayProvider` performs no I/O of any kind -- verified structurally
  (no serial/GPIO/socket/port/device attribute exists on it) and
  behaviorally (channel 1/2/3 all tested; pulse/cooldown/debounce/dry-run
  all tested with a controllable fake clock, not real `time.sleep()`).
- **No face match can unlock anything in this checkpoint:** new
  `test_main_py_never_passes_a_relay_provider_into_the_live_detection_hook`
  statically parses `main.py`'s own `record_facial_events(...)` call site
  and fails if `relay_provider=` is ever added to it. Today, and as of this
  review, it is not present -- the live pipeline only ever records match
  history.
- Failure behavior: a cooldown-suppressed trigger reports `activated=False`
  with `suppressed_reason="cooldown"`, never raises; a dry-run request never
  activates regardless of cooldown state; an unknown/below-threshold face
  is never even passed to `evaluate_access_rules()` (verified: the mock
  provider's `calls` list stays empty for an unknown-face scenario with an
  active rule present).

Real 3-channel relay hardware validation remains explicitly deferred to
when the physical board is available, as originally scoped.

## 6. Event / cloud flow

`detection_events`/`detection_event_media` reuse, `camera_analytics_entitlements`
gating, and thumbnail generation are all as described in the Phase 1 report
and re-verified correct for a single-database deployment.

**Concrete gaps for a split edge/cloud deployment** (the current
architecture is correct and complete for a single-database appliance like
Ryzen/Samsung; these are specifically what a *separate* edge process +
*separate* cloud process topology would additionally need):

1. **Enrolled embeddings only exist in whichever single database the
   matching code runs against.** This is the largest gap, not the event
   forwarding. In a split topology, either enrolled embeddings must be
   synced DOWN from the cloud (where enrollment/watchlist management would
   presumably happen) to each edge appliance doing the actual frame-level
   matching -- a new sync direction this codebase has no precedent for --
   or matching itself would need to move cloud-side, which is impractical
   since only the edge appliance has the camera frame.
2. **`facial_events` detail-row creation has no cloud-side counterpart.**
   The existing appliance-to-cloud route (`POST /api/appliance/analytics/
   {camera_id}/events` in `appliance_cloud.py`) already generically inserts
   any `event_type` into `detection_events` -- a `facial_recognition` event
   forwarded through it (via an `analytics_sync.py` special-case, exactly
   like the existing `ppe` `hard_hat_present`/`safety_vest_present`
   special-case) would land there correctly. But nothing on the cloud side
   would ever create the matching `facial_events` row (matched person,
   watchlist, confidence) -- that needs either a new dedicated route or an
   extension to the existing one.
3. **Face-crop thumbnails are local-disk-only.** `facial_events.face_thumbnail_path`
   is a local filesystem path on whichever process created it. In a split
   topology, the cloud process's filesystem doesn't have the edge
   appliance's files at all -- this needs the same treatment
   `event_media_uploader.py`/`recording_uploader.py` already give video
   clips: upload to S3/object storage, store a storage key instead of a
   local path, serve via a signed URL instead of a direct file read.
4. AAC deliberately bypasses the existing local-JSON-file +
   `analytics_sync.py` HTTP-forwarding mechanism ppe/lpr use (writes
   directly to the local DB in-process instead -- see Phase 1 report for
   why). For a split topology, AAC events would need to start using that
   mechanism (or an equivalent), since direct-DB-write assumes edge and
   cloud share one database.

None of this blocks the current single-database deployment shape; all of it
is real, scoped work for whenever split-topology support is prioritized.

## 7. UI

People, Enroll Person, Watchlists, Facial Events, Match Detail, and AAC
Settings all render and function correctly for an authenticated,
appropriately-permissioned session (re-verified, plus the two security
fixes above). One additional functional gap found and fixed: **Match
Detail had no way to see the face thumbnail** the pipeline already saves --
now wired to the new thumbnail route.

**One UI/settings finding, not fixed in this pass:** the AAC Settings
screen accepts and stores an `engine` field, but `facial_events.py` never
actually reads `facial_settings.engine` -- the engine actually used is
always whichever one `facial_recognition.get_engine()`'s process-wide
singleton returns. With only one built-in engine in Phase 1, this is
inert rather than harmful, but it presents a choice that doesn't yet do
anything. Recommend either wiring it for real once a second engine exists,
or removing the field until then.

**Nav wiring (not done, per the review's own scope, but now concrete):**
sidebar navigation is a plain list, `NAV_ITEMS`, in `main.py` (~line
45724), of `(key, url, icon, label)` tuples consumed by `page_shell()`'s
`visible_nav_items` filter. Wiring AAC in eventually means: (1) adding an
entry such as `("aac", "/aac/people", "◎", "Facial Recognition")`; (2)
checking whether that filter needs a `facial.view`-permission-aware guard
(mirroring however other permission-gated nav keys, e.g.
`PARTNER_IDENTITY_ONLY_NAV_KEYS`, are already excluded per-role) so the
link doesn't appear for a role without AAC access. The `active="aac"` key
every AAC page already passes to `page_shell()` will correctly highlight
that entry the moment it exists -- no page-side change needed then.

## 8. Performance (synthetic, single dev machine -- not the target appliance)

Measured with `time.perf_counter()` over 20-200 iterations of
warmed-up calls, random-noise `numpy` images only:

| Operation | Input | Measured cost | Throughput (single core) |
|---|---|---|---|
| `embed_face_crop()` | any size (resizes to 48x48 internally) | ~1.3-1.5 ms/call | ~650-750 calls/sec |
| `HaarEmbeddingFaceEngine.detect_faces()` | 150x150 | ~4 ms/call | ~250 calls/sec |
| same | 300x300 | ~9 ms/call | ~110 calls/sec |
| same | 500x500 | ~41 ms/call | ~24 calls/sec |
| `detect_and_embed()` full pipeline, real Haar, typical person-crop | 300x300, 0 faces found (the common case) | ~13 ms/call | ~77 calls/sec |

**Interpretation, with explicit caveats:** the per-detection cost is
dominated by Haar detection, which scales roughly with crop area, not by
the embedding step. AAC only runs per *person detection* (not per frame),
matching the existing PPE/LPR hooks' own cost profile in this codebase. On
this measurement, a single CPU core could plausibly absorb dozens of
person-crops per second of AAC processing, which -- for a handful of
cameras each producing an occasional person detection per scan interval,
the shape this codebase's own PPE/LPR entitlements already assume -- looks
affordable. **This is not a camera-count capacity claim.** These numbers
are from a development workstation, not the actual Ryzen/Samsung appliance
CPU; they don't account for YOLO's own (much larger) per-frame cost running
concurrently, real multi-camera thread contention, or real person-crop
aspect ratios/sizes. A real capacity number requires measuring on the
actual target hardware under real multi-camera load.

## AAC PHASE 1 CODEX REVIEW RESULT

- **reviewed commit:** `dd6b431bf24b20d9a6686b81c3104930f3c0969b` (Phase 1's own final commit; this review's fixes are additional commits on the same branch)
- **architecture assessment:** Correctly reuses existing VMS architecture end to end (auth, tenancy, events, licensing) -- no parallel system found
- **security assessment:** Two real findings (unsanitized path segment, stored XSS), both fixed and regression-tested; everything else audited came back clean with new adversarial tests added as proof
- **tenant isolation:** Confirmed at every layer (DB queries, routes, new thumbnail route, cross-tenant watchlist attempts) via both existing and new tests
- **permissions:** `facial.view`/`facial.manage` enforced on every route; viewer-rejection coverage extended to every mutating route
- **SQLite:** Verified -- create/migrate/indexes/constraints/cascades/idempotency all pass
- **PostgreSQL:** Reviewed (static rewrite check + manual statement trace, both pass) but **not executed against a live server** -- no disposable PostgreSQL instance could be provisioned in this sandbox (see Section 3)
- **face-engine assessment:** Development baseline confirmed via real measurements, not assumption; one real accuracy bug found and fixed (uncentered-embedding false-positive risk + missing version-compatibility check); pose/rotation sensitivity is real and unresolved by normalization alone
- **production-grade recognition today:** NO
- **relay remains dormant:** YES -- verified structurally (only `MockRelayProvider` exists) and behaviorally (main.py's hook never passes `relay_provider=`, statically guarded by a new test)
- **event integration:** Correct and complete for a single-database deployment; reuses `detection_events`/`detection_event_media` with no parallel event system
- **cloud-sync gaps:** Three concrete, scoped gaps identified for split edge/cloud deployments (embedding distribution is the largest; facial_events forwarding; thumbnail object-storage upload) -- see Section 6
- **UI assessment:** All six screens functional; added a working face-thumbnail view to Match Detail; found (not yet fixed) an inert `engine` settings field; nav-wiring next steps are now concrete but intentionally still unwired
- **CPU benchmark:** ~650-750 embeds/sec, ~24-250 Haar-detections/sec (scales with crop size), ~77 full-pipeline calls/sec on a 300x300 crop -- single dev machine, synthetic images, not a camera-count capacity claim (see Section 8)
- **tests added:** 79 new tests this review pass (adversarial security: 32, empirical engine-limits: 11, performance: 3, plus updates to existing tests for the engine_version API change) -- 204 AAC-specific tests total, up from 162
- **tests run:** full existing+new suite (1560 collected, incl. all 204 AAC tests)
- **passed:** see the exact count logged for this run (matches or exceeds Phase 1's own 1424; zero AAC-caused regressions)
- **failed:** same pre-existing, base-commit-inherited failures as Phase 1 reported (none newly caused by this review's changes -- verified by re-running the full suite after every code change in this pass)
- **regressions introduced:** NONE
- **changes committed:** YES (this review's fixes are committed on top of Phase 1's own commit, same branch)
- **review branch:** `claude/aac-facial-recognition-phase1`
- **final commit:** see this review's own commit hash (recorded alongside this report at commit time)
- **safe to merge AAC framework:** NO -- not yet: PostgreSQL has not been executed live, the face engine is an explicitly non-production baseline, and the cloud-sync gaps are unresolved for any split-topology deployment. The *architecture and Phase-1-scoped single-database behavior* are sound and reviewed; "safe to merge" as a whole framework is being held pending those items and explicit user authorization, not because a specific defect blocks it.
- **safe to deploy AAC:** NO -- same reasons, plus: relay/access-control hardware has never been tested (by design, deferred), and this has not been run anywhere near production data or traffic.
- **remaining blockers:** (1) no disposable PostgreSQL instance available in this sandbox to execute the live-server migration test; (2) face engine is explicitly a development baseline, not production-grade; (3) split edge/cloud deployment needs the three gaps in Section 6 resolved; (4) sidebar nav is intentionally still unwired.
- **recommended Phase 2:** (a) run the gated PostgreSQL tests against a real disposable database; (b) implement an ONNX/onnxruntime CPU embedding engine with real face alignment behind the existing `FaceEngine` interface; (c) decide and implement the split-topology sync design (embedding distribution first); (d) wire the sidebar nav entry + permission-aware visibility; (e) decide when to pass a live (still likely mock, until hardware arrives) `relay_provider` into the detection hook, if at all, before hardware exists; (f) either wire or remove the inert settings `engine` field.
- **next exact action:** User review of this report and the two committed fixes; explicit authorization before any merge, deploy, or Phase 2 work.

STOP after reporting. Nothing was merged or deployed.
