# AAC Facial Recognition -- Phase 1

**Branch:** `claude/aac-facial-recognition-phase1`
**Base commit:** `ec5272fb619eda50e188eaac7c6629e1157af3e7` (matches `codex/linux-deb-packaging`,
`codex/authoritative-vms-reconciliation-20260907`, and `codex/samsung-talk-diagnostic-integration-20260908`
at the time this branch was created)

AAC = AnyAiCam facial recognition / access-control analytics. This document
records what Phase 1 built, how it fits into the existing VMS, and what is
explicitly deferred to a later phase. `docs/AI_HANDOFF.md`, this project's
usual source of truth, does not exist at this branch's base commit (it was
present on some other in-flight branches, e.g. `build/v1.2-modular-foundation`,
but not on the reconciled base) -- this document is written as a
`docs/phase6-*`-style milestone report in that same absence, not as a
replacement for AI_HANDOFF.md.

## Architecture: reused, not parallel

AAC is implemented as a modular analytics feature inside the existing
monolith, following the exact same conventions as PPE, LPR, Smart Motion,
and People Counting:

| Concern | Existing precedent reused |
|---|---|
| Per-detection analytics hook | `save_yolo_events()` in `app/main.py`, same "person" branch PPE/LPR already hook into |
| Camera-level licensing | `camera_analytics_entitlements` (analytic_key=`facial_recognition`), gated through `customer_analytics_panel.py`'s existing `ANALYTIC_LABELS`/`assign_entitlement()` |
| Database access | `database_backend.connect()` (SQLite and PostgreSQL, same abstraction every other table uses) |
| Migrations | `db_migrations.py`'s existing `MIGRATIONS` list |
| Auth/session/tenancy | `partner_portal.py`/`partner_db.py` (identity, `ROLE_PERMISSIONS`, `audit()`, `connection()`) -- the same primitives partner-portal-style admin routes already use |
| Event history | `detection_events`/`detection_event_media` (event_type=`facial_recognition`), with a new `facial_events` detail table exactly like `detection_event_media` is already a detail table |

New modules, each scoped the same way `ppe.py`/`lpr.py` are:

- `app/facial_recognition.py` -- face detection/embedding/matching/debounce math. No database, no `main` import. CPU-only.
- `app/relay_control.py` -- relay/access-control abstraction + `MockRelayProvider`. No hardware I/O anywhere in this module.
- `app/facial_people.py` -- enrollment/watchlist/settings DB service, always tenant-scoped by an explicit `customer_id`.
- `app/facial_events.py` -- ties the three together: matches a person crop against enrolled embeddings, writes `detection_events`/`facial_events`, evaluates access rules.
- `app/facial_recognition_ui.py` -- routes (`/aac/...` pages, `/api/aac/...` JSON), permission-checked, tenant-scoped, audited.

## What is genuinely new in the database

Migration `20260908_facial_recognition` in `app/db_migrations.py` adds:

- `facial_people` -- enrolled person profile (name, optional employee/customer reference, site, enabled/disabled status, notes).
- `facial_embeddings` -- one row per reference image's embedding (biometric template). `ON DELETE CASCADE` from `facial_people`.
- `facial_watchlists` / `facial_watchlist_members` -- named lists + membership, many-to-many.
- `facial_events` -- one row per facial match, 1:1 with a `detection_events` row via `detection_event_id`.
- `facial_rules` -- access-control rule definitions (trigger type, relay channel, pulse, cooldown, dry-run, confidence threshold).
- `facial_settings` -- one row per customer (confidence threshold, unknown-event toggle, debounce seconds, engine).

Applies on both SQLite and PostgreSQL through the existing `connect()`/
`_postgres_sql()` abstraction; no new database-specific SQL was introduced
(no `BLOB`, no `AUTOINCREMENT`, no dialect-specific syntax).

## Face engine (CPU-first)

Default and only built-in engine: `HaarEmbeddingFaceEngine` (`facial_recognition.py`).

- **Detection:** OpenCV's bundled Haar frontal-face cascade -- the same
  lazy-singleton-cascade pattern `lpr.py` already uses for its plate cascade.
- **Embedding:** a real, CPU-only, deterministic feature vector (grayscale,
  resized, histogram-equalized, L2-normalized). This is an honest classical-CV
  baseline, **not** a deep-learning face embedding -- it distinguishes clearly
  different people in good conditions but does not claim state-of-the-art
  accuracy. This is a deliberate Phase 1 trade-off, not an oversight.
- **No new dependency, no CUDA:** only `opencv-python` and `numpy`, both
  already hard dependencies of this codebase. `ANYAICAM_FACE_ENGINE`-style
  pluggability is designed in (`FaceEngine` interface, `get_engine()`
  singleton) so a stronger CPU engine (e.g. an ONNX model via
  `onnxruntime`'s `CPUExecutionProvider`) or a future GPU-accelerated engine
  can be added later without any caller changing.

## Relay / access control

The physical 3-channel relay is not available and was not required for this
phase. `relay_control.py` implements the full logical model (channel 1/2/3,
pulse duration, debounce, cooldown, dry-run) behind a `RelayProvider`
interface, with exactly one concrete implementation: `MockRelayProvider`,
which never touches any real I/O. `facial_rules` rows default to
`dry_run=1`; flipping a rule to `dry_run=0` is the one explicit, auditable
action that lets a (currently mock-only) provider "activate" a channel.

**The live detection hook in `main.py` never passes a `relay_provider`
at all** -- `record_facial_events()`'s `relay_provider` parameter defaults to
`None`, which skips access-rule evaluation entirely. Phase 1's running
system only records facial-match history; it does not attempt to drive
access control from the hot detection path yet. Wiring a live relay
provider into that path (real or mock) is explicitly future work, once a
decision is made about when that should start happening automatically.

**No real relay hardware was tested. None is claimed to be compatible.**

## Tenant isolation

Every query in `facial_people.py`/`facial_events.py` is scoped by an explicit
`customer_id` argument -- there is no function that can be called without
one, and every one of those functions' own tests include an explicit
cross-tenant case (a real id belonging to a *different* customer must behave
identically to a nonexistent id). `facial_recognition_ui.py` adds one more
layer on top: `_resolve_customer_id()` hard-pins `customer_owner`/
`customer_viewer` sessions to their own `customer_id`, rejecting any request
that names a different one, before any data access happens at all.

## Retention / deletion

`facial_people.delete_person()`:

- **Hard-deletes** every `facial_embeddings` row for that person (the actual
  biometric templates) and every reference-image file on disk.
- **Hard-deletes** the `facial_people` row itself and their
  `facial_watchlist_members` rows (`ON DELETE CASCADE`).
- **Does not touch** `facial_events`/`detection_events` history for that
  person. Those rows keep their own `matched_person_name`/
  `matched_watchlist_name` snapshot, taken at match time -- so a deletion
  never rewrites or erases historical audit/event records, matching this
  codebase's existing pattern of a detail table (like `detection_event_media`)
  never being retroactively edited by a later, unrelated action.
- Is audited (`facial.person.deleted`) by the calling route, after the
  deletion succeeds.

Enrollment never persists the original uploaded photo -- only the single
detected-and-aligned face crop actually used to produce the embedding is
ever written to disk (`ANYAICAM_AAC_FACES_FOLDER`); the upload itself is
decoded in memory and discarded.

## Security review notes

- Authenticated enrollment/deletion: every mutating route requires a valid
  `partner_portal` session and the `facial.manage` permission (new
  `ROLE_PERMISSIONS` entries in `partner_db.py`).
- No cross-tenant face search: covered above, and exercised by dedicated
  tests in `test_facial_people.py`, `test_facial_events.py`, and
  `test_facial_recognition_ui.py`.
- No secrets/biometrics in logs: `test_facial_recognition_secret_free_logging.py`
  statically scans every new module for a `print()`/`logger` call that
  references a sensitive variable by name, and
  `test_facial_recognition_ui.py::test_audit_log_never_contains_the_raw_uploaded_image_or_embedding`
  proves it dynamically against a real `audit_logs` row.
- CSRF: new routes authenticate via the same `partner_portal` session cookie
  (`SameSite=Strict`, `HttpOnly`) every other partner-portal-style route
  already relies on. No existing CSRF behavior was changed or bypassed.
- File paths: reference-image and thumbnail paths are always constructed
  from a fixed root plus internally-generated ids (`customer_id`/`person_id`/
  `embedding_id`, never raw user input), so there is no path-traversal
  surface from an enrollment request.

## Remaining work (not done in Phase 1)

- The live detection hook does not evaluate `facial_rules`/trigger a relay
  provider automatically -- it only records match history. Wiring that in
  (even with `MockRelayProvider`) is a deliberate follow-up decision, not an
  oversight.
- No sidebar navigation entry was added for the new `/aac/...` pages (the
  shared nav template lives deep inside `main.py`'s `page_shell()`
  machinery); the pages work when navigated to directly but are not yet
  discoverable from the existing UI chrome.
- AAC events are written directly into whichever database this process is
  already configured against (`database_backend.connect()`), not forwarded
  through the appliance-to-cloud `analytics_sync.py` HTTP bridge PPE/LPR use.
  This is complete and correct for a single-database deployment (the current
  Ryzen/Samsung on-prem production shape); a split edge/cloud topology would
  need that forwarding added separately.
- Only the built-in Haar/intensity engine exists; a stronger CPU engine
  (e.g. ONNX + `onnxruntime`) or GPU acceleration is designed to be pluggable
  (`FaceEngine` interface) but was not implemented in this phase.
- `PostgreSQL` migration tests are real and gated on
  `ANYAICAM_TEST_POSTGRES_URL`, but were not run against a live PostgreSQL
  server in this session (none was available); they were verified to skip
  cleanly, and a separate always-on static check confirms the migration SQL
  survives `database_backend.py`'s own SQLite->PostgreSQL rewriting.
