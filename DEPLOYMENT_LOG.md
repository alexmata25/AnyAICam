# AnyAiCam Deployment Log

Persistent record of production changes, kept in sync with the reconciliation
workflow established 2026-08-20/21: every completed phase gets an entry here
before being considered done. Format: newest entry first.

---

## 2026-08-21 — Customer-facing analytics reads: tenant-scoped cloud `detection_events` replaces mock fallback

**Feature:** authenticated portal customers now see their own real,
tenant-scoped detection events through the existing
`GET /api/analytics/events` and `GET /api/analytics/summary` routes
(and the existing `/analytics` page, unchanged — its client-side
`fetch()` calls are same-origin and already carry the customer's
session cookie), instead of the local `analytics_events.json`/mock
fallback. Every non-customer caller (no session, or a non-customer-
portal role) is completely unaffected — same
`analytics_events()`/`mock_analytics_events()` fallback as before,
byte-for-byte.

**Application commit deployed:** `f69629f`
**Branch:** `production-reconcile-20260820`
**Deployment time:** `2026-08-21T21:12:02Z`

**Implementation (`app/main.py` only):**
- New `_customer_detection_events(request)` — mirrors
  `_customer_playback_cameras()`'s exact identity/role/scoping
  pattern (`partner_identity(request)`, gated to
  `CUSTOMER_PORTAL_ROLES`). `customer_owner` is scoped strictly to
  `detection_events.customer_id == identity.customer_id`;
  `customer_viewer` is further restricted to cameras where
  `customer_camera_permissions.can_playback=1` (reusing that existing
  permission rather than adding a dedicated `can_view_analytics`
  column — a smallest-change choice, worth a dedicated permission
  later if the two should diverge). Returns `None` for any
  non-customer-portal caller, which both routes treat as "render the
  existing legacy/mock experience unchanged". Scope always comes from
  the authenticated identity and server-side JOINs to
  `cameras`/`sites` — never from any request parameter.
- New `_build_analytics_summary(events, mock_data)` — the original
  aggregation body extracted verbatim and parameterized, zero logic
  changes, shared by both branches so the type/camera/hourly/7-day
  breakdown exists exactly once.
- `analytics_event_search()` (`GET /api/analytics/events`) and
  `analytics_summary_api()` (`GET /api/analytics/summary`) both gained
  a `request: Request` parameter and branch on
  `_customer_detection_events(request)`. A real customer with zero
  events gets `[]` with `mock_data:false` — an honest empty state can
  never be reported as demo/mock activity.
- Untouched by this milestone: YOLO/person detection, motion
  detection, the analytics-sync worker (edge → cloud ingestion into
  `detection_events`), and the recording pipeline. This is a read-path
  change only.

**Validation (disposable environment — fresh clone, standalone
container, never touching the real `anyaicam-vms`):**
- New suite (`tests/test_customer_analytics_events.py`): **16/16 passed**
- Full suite: **128/128 passed**

**Production backup checkpoint:** `20260821-210607`
- DB: `recordings/partner_portal.db.sqlite-backup-pre-customer-analytics-deploy-20260821-210607`
- `.env`: `backups/env-pre-customer-analytics-deploy-20260821-210607`
- App directory: `backups/app-pre-customer-analytics-deploy-20260821-210607.tar.gz`
- Compose file: `backups/docker-compose.pre-customer-analytics-deploy-20260821-210607.yml`
- Rollback reference: `backups/rollback-reference-20260821-210607.txt`

**Verification performed after deployment:**
- Production container healthy after `docker compose up -d --force-recreate vms`.
- `detection_events` remained exactly 1 row; proof event `b2609518c1ae`
  unchanged (same id, camera, site, event_type, confidence, timestamp).
- Real authenticated `customer_owner` call (minted via the app's own
  live session-signing mechanism against the real, already-existing
  account for this customer — no new DB rows, no session_id, nothing
  written) returned:
  - `GET /api/analytics/events` → `mock_data:false`, exactly 1 event,
    `camera:3`, `site:"Home"`, `event_type:"car"`,
    `confidence:0.6666`, `thumbnail:null`, `linked_recording:null`.
  - `GET /api/analytics/summary` → `total_events:1`,
    `type_counts:{"car":1}`, `camera_counts:{"3":1}`,
    `active_camera:3`, `mock_data:false`.
- Authenticated non-customer (`administrator`) call to both routes
  confirmed the legacy path is reached and behaves exactly as before
  (same local-file-backed response, unaffected by this change).
- Existing appliance/recording routes remained healthy; real appliance
  traffic (`/configuration`, `/heartbeat`, `/cameras`, `/commands`,
  `/scan-jobs`, `/recordings/status`) uninterrupted through the
  recreate.
- Zero new errors/tracebacks/exceptions in logs since the recreate.
- Ryzen was not touched during this rollout.

**Current safety state:** `ANYAICAM_ANALYTICS_SYNC_ENABLED=false`,
`ANYAICAM_RECORDING_UPLOAD_ENABLED=false` — analytics ingestion
(edge → cloud sync into `detection_events`) remains disabled on both
EC2 and Ryzen; this milestone only changes how the single existing
proof row is *read*, not how new rows arrive.

**Rollback:** restore the `20260821-210607` checkpoint (DB/`.env`/app
directory/compose file) or `git revert f69629f`, then
`docker compose up -d --force-recreate vms`. No schema rollback
required — this milestone added no migration and no new table.

---

## 2026-08-21 — Pilot activation: AWS recording resources verified, cloud-side config set (still inert)

**Phase:** First activation step of the controlled pilot rollout. Not a
code change — the real AWS resources for the recording pipeline were
created by the account owner, verified from EC2, and their identifiers
were added to production config. Both feature flags remain `false`;
nothing is enabled yet.

**AWS resources created (by the account owner, outside this session):**
- S3 bucket `anyaicam-recordings-prod-20260820` — private, SSE-S3,
  versioning disabled, Object Lock disabled, lifecycle rule
  `anyaicam-recordings-failsafe` (deletes incomplete multipart uploads
  after 7 days).
- `anyaicam-recording-upload-role`, `anyaicam-recording-read-role`,
  `anyaicam-recording-lifecycle-role` — the three roles designed in
  `docs/r1-recording-iam.md`, `docs/r4-recording-read-iam.md`,
  `docs/r5-recording-lifecycle-iam.md`.
- `anyaicam-ec2-app-role` given a new inline policy
  `assume-recording-roles`, scoped to `sts:AssumeRole` on only those
  three roles.

**Verification performed from EC2 (real AWS calls, no code changes):**
- Confirmed the running instance resolves to
  `assumed-role/anyaicam-ec2-app-role/i-0f0fb6a78871b20d4` in account
  `880690594006`.
- All three roles accepted `sts:AssumeRole` from that identity (trust
  policy confirmed working, not just reviewed as a document).
- Real object-level privilege-separation test against a throwaway key
  (`recordings/_verification_probe/.../probe.txt`): upload role could
  `PutObject` but not `GetObject`/`DeleteObject`; read role could
  `GetObject` (byte-for-byte match against what was uploaded) but not
  `PutObject`/`DeleteObject`; lifecycle role could `DeleteObject` but
  not `PutObject`/`GetObject`. Test object created and removed as part
  of this check — nothing left behind in the bucket.
- **Not independently verifiable from EC2:** bucket-level settings
  (encryption, versioning, Object Lock, the lifecycle rule itself) —
  by design none of the three roles, nor the base EC2 role, carry any
  bucket-level read permission, only single object-level actions each.
  Those specific settings are taken on the account owner's report, not
  independently confirmed here.

**Config change:** `.env` on EC2 (gitignored, not a tracked file — no
git commit for this step, hence no commit SHA below) gained:
```
ANYAICAM_RECORDING_UPLOAD_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-recording-upload-role
ANYAICAM_RECORDING_READ_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-recording-read-role
ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-recording-lifecycle-role
ANYAICAM_RECORDING_S3_BUCKET=anyaicam-recordings-prod-20260820
ANYAICAM_RECORDING_UPLOAD_ENABLED=false
ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=false
```
`AWS_REGION=us-east-1` was already set from the live-relay work and is
unchanged.

**Why this is still inert:** `ANYAICAM_RECORDING_UPLOAD_ENABLED=false`
keeps R1's credentials-issuing endpoint returning 404 exactly as before.
R4's presigned-URL path has no separate flag — it fails closed on its
own whenever the `recordings` table has no `status='available'` rows,
which is still the case (0 rows). `ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=false`
keeps R5 off entirely. **Also still true regardless of these flags:**
`RUNTIME_ROLE=cloud` on EC2 means the actual ffmpeg remux/transcode/
upload worker (`recording_uploader.py`) does not and cannot run here —
it only runs under `RUNTIME_ROLE in {edge, combined}`, i.e. on the
physical pilot appliance itself. Enabling appliance-side recording
upload is a separate step, being done directly on the appliance by the
account owner (who has SSH access there), not from this session.

**Production backup:** `.env.pre-recording-aws-config-20260820-234809`.

**Verification performed after the config change:**
`docker compose restart vms` → healthy, no new errors in logs.

**Rollback:** restore `.env.pre-recording-aws-config-20260820-234809`,
restart `vms`.

**Next steps (one component at a time, each to be reported before the
next):** (1) appliance-side `RUNTIME_ROLE`/`ANYAICAM_RECORDING_UPLOAD_ENABLED`
verification and activation, done directly on the pilot appliance; (2)
flip `ANYAICAM_RECORDING_UPLOAD_ENABLED=true` on EC2 once the appliance
side is confirmed ready; (3) confirm a real recording reaches the
`recordings` catalog; (4) confirm customer Playback can retrieve and
play it back in a browser; (5) only then consider enabling the
retention sweep, and only against a real, already-verified recording.

---

## 2026-08-21 — Pilot activation prep: codec-aware cloud copy (H.264 remux, HEVC transcode, unsupported fail-safe)

**Phase:** Pre-activation step of the controlled pilot rollout, extending the
prior MKV → MP4 remux entry below from H.264-only to codec-aware. Not a new
R-numbered phase. Prompted by your requirement that AnyAiCam support both
H.264 and H.265/HEVC cameras automatically, with no manual codec selection
by the customer or installer, and with appliance CPU load considered for
any transcode work.

**Production backup:** `app/recording_uploader.py.pre-codec-aware-20260820-223745`.

**Commit:** `d779e57a4a67678e4bc3e08b1012f583c579d44e` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree — all three confirmed at this commit with matching file hashes.

**What changed:**
- `_detect_video_codec()` *(new)* — ffprobe-based codec detection run once
  per completed recording before any cloud copy is prepared. Returns
  `None` (never raises) on any probe failure.
- `_prepare_cloud_copy()` *(new)* — the codec-aware dispatch point, now
  called from `_relay_camera_once()` in place of the old direct call to
  `_remux_to_mp4()`:
  - **h264** → `_remux_to_mp4()` (unchanged stream-copy path from the
    prior entry — still free, still no re-encode).
  - **hevc / h265** → `_transcode_hevc_to_h264()` *(new)* — a real
    `ffmpeg -c:v libx264` re-encode (audio stream-copied, not
    re-encoded), faststart, same duration-verification check as the
    remux path. Browser support for HEVC is not assumed.
  - **anything else, or undetected** → returns `None`. The original MKV
    is left completely untouched, the reason is logged once per file
    (a per-camera in-memory set avoids re-probing/re-logging the same
    file on every scan tick), and **no `recordings` catalog row is ever
    created** for it — Playback never gets a broken entry.
- **Appliance CPU protection:** a new module-level
  `threading.Semaphore` (`_transcode_semaphore`, default concurrency 1,
  env `ANYAICAM_RECORDING_TRANSCODE_MAX_CONCURRENCY`) wraps the HEVC
  transcode's ffmpeg call, so multiple HEVC cameras on the same
  appliance queue for transcode capacity one at a time by default rather
  than running concurrently and overwhelming the appliance. Runs inside
  `asyncio.to_thread()`, so the blocking `.acquire()` never touches the
  event loop.
- New env-configurable constants: `CODEC_DETECT_TIMEOUT_SECONDS`,
  `ANYAICAM_RECORDING_TRANSCODE_PRESET` (default `veryfast`),
  `ANYAICAM_RECORDING_TRANSCODE_CRF` (default `23`),
  `ANYAICAM_RECORDING_TRANSCODE_THREADS` (default `2`),
  `ANYAICAM_RECORDING_TRANSCODE_TIMEOUT_SECONDS` (default `600`),
  `ANYAICAM_RECORDING_TRANSCODE_MAX_CONCURRENCY` (default `1`).
- `app/tests/test_recording_codec_aware.py` *(new)* — 14 tests: fast/mocked
  detection and dispatch-branch tests (h264→remux only, hevc/h265→
  transcode only, unsupported codec calls neither and leaves the original
  file's bytes untouched, undetectable codec fails safe, no staging
  output for an unsupported codec, the unsupported-codec de-dup cache
  probes only once across three scans), a concurrency test proving the
  semaphore actually caps simultaneous execution at 1, and two **real
  ffmpeg/ffprobe integration tests**: a genuine H.264-in-MKV clip is
  detected and routed to remux only, and — the strongest proof this
  works — a genuine HEVC-in-MKV clip is run through the real (unmocked)
  `_transcode_hevc_to_h264()` and the *output* file is reprobed to
  confirm its own codec is h264 (a real re-encode happened, not a
  renamed copy), with duration preserved and the original MKV untouched.

**Verification performed:**
- `ast.parse` clean on both changed/new files.
- `docker compose restart vms` → healthy, no new errors in logs.
- New suite (14 tests) plus the existing remux suite (8 tests) run
  inside the real deployed container via an ephemeral pytest install
  (removed afterward): **22/22 passed**, including both real ffmpeg
  integration tests.
- Full existing regression suite run: 13 pre-existing failures in
  `test_login_csrf.py`, `test_login_csrf_http_integration.py`, and
  `test_partner_db_initialization.py` were confirmed to **already exist
  on the prior baseline** (`718dbd0`) — the pre-edit backup was
  temporarily restored, the container restarted, and the same three
  files re-run against it, producing the identical 13 failures. None of
  the three files reference `recording_uploader.py`. Not a regression
  introduced by this change; the codec-aware file was restored
  immediately after this comparison and reconfirmed healthy.
- Zero AWS configuration changed — this is appliance-local ffmpeg
  processing only; `ANYAICAM_RECORDING_UPLOAD_ENABLED` remains unset and
  the entire pipeline remains fully inert in production.

**File hash verification (EC2 == GitHub == Ryzen 5), both at commit `d779e57`:**

| File | SHA-256 |
|---|---|
| `app/recording_uploader.py` | `e4fe843e...49eb82c43ee4b6a236321f` |
| `app/tests/test_recording_codec_aware.py` | `43820ea1...ea6ea6f8f6d21a95aa76253` |

**Rollback:** restore `recording_uploader.py.pre-codec-aware-20260820-223745`,
restart `vms`. On Git: `git revert d779e57` followed by a push.

**Codec support decision: SETTLED.** The recording pipeline now handles
H.264 (free remux) and H.265/HEVC (concurrency-limited real transcode)
automatically per-file, with a safe no-op for anything else. No customer
or installer codec selection exists or is needed.

**Next step:** AWS IAM/S3 activation (upload/read/delete roles + recordings
bucket + lifecycle), one component at a time, per the controlled pilot
plan — still not started; no AWS credentials have been available anywhere
reachable throughout this project.

---

## 2026-08-21 — Pilot activation prep: playback format resolved, MKV → MP4 remux

**Phase:** Pre-activation step of the controlled pilot rollout (between R5
and the AWS activation steps). Not a new R-numbered phase — a required fix
identified while resolving the pilot activation's playback-format gate.

**Codec confirmation:** you ran `ffprobe` against a real, recent recording
on the real pilot appliance (`7eb499b6d1`) and confirmed `codec_name=h264`,
`1280x720`. This resolved the open playback-format question from R3/R4's
deployment log entries.

**Production backup:** `app/recording_uploader.py.pre-h264-mp4-remux-20260820-222535`.

**Commit:** `be47a87a8920d04ddb8c68b7ff1e7b7d1b4a898b` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree — all three confirmed at this commit with matching file hashes.

**Files changed:**
- `app/recording_uploader.py` — new `_remux_to_mp4()` (ffmpeg `-map 0 -c copy -movflags +faststart`, no re-encode — free given the confirmed H.264 codec) writes to a dedicated per-camera staging subfolder; the original MKV is never opened for writing, moved, or deleted. New `_verify_output_duration()` (ffprobe-based, advisory if ffprobe itself is unavailable, a real failure on genuine mismatch) confirms the remux didn't truncate the recording before it's ever uploaded. `_upload_recording()`'s `Content-Type` changed to `video/mp4`; the uploaded S3 key now derives from the MP4 filename, so R2's catalog automatically references the MP4 object with **zero R2 code changes**.
- `app/tests/test_recording_remux.py` *(new)* — 8 tests: 7 fast/mocked failure-path tests, plus one **real integration test** that generates an actual H.264-in-MKV clip with ffmpeg and runs the genuine `_remux_to_mp4()` against it (not mocked), confirming a valid, correctly-timed, faststart MP4 with the codec preserved.

**Verification performed:**
- `ast.parse` clean.
- **50/50 tests passed** (8 new + 12 existing R3 + 9 R1 + 5 R2 + 7 R4 + 9 R5) run inside the actual deployed container — including the real ffmpeg integration test, which genuinely exercises the production remux path end-to-end (not just mocked).
- Confirmed R4's `_customer_camera_recordings()` needs **no code change** — its `{name}` field already derives from `s3_key.rsplit('/', 1)[-1]`, and the presigned-URL logic and the Playback page's `<video controls>` element were already fully file-extension-agnostic.
- Real production `recordings` table confirmed still 0 rows. Live-relay, R1, R2, R4, R5, and all customer-facing routes confirmed unaffected.
- `docker compose restart vms` → healthy, no new errors in logs.
- **Zero AWS configuration changed** — `ANYAICAM_RECORDING_UPLOAD_ENABLED` remains unset; this is still fully inert in production.

**File hash verification (EC2 == GitHub == Ryzen 5), all at commit `be47a87`:**

| File | SHA-256 |
|---|---|
| `app/recording_uploader.py` | `cdede512...3ac8ffa25a0a58269e` |
| `app/tests/test_recording_remux.py` | `31c3c591...835e26cc1fc8469a813` |

**Rollback:** restore `recording_uploader.py.pre-h264-mp4-remux-20260820-222535`, restart `vms`. On Git: `git revert be47a87` followed by a push.

**Playback format decision: SETTLED.** MP4 (faststart, stream-copied H.264) is the production format for the confirmed-H.264 pilot camera. No CPU-heavy transcode was introduced. If a future camera's codec is ever not H.264, browser support for that codec in MP4 is a separate, not-yet-decided question.

**Next step:** AWS IAM/S3 activation (upload/read/delete roles + recordings bucket + lifecycle), one component at a time, per the controlled pilot plan — not yet started.

---

## 2026-08-21 — R5: Recording retention and lifecycle sweep (foundation only)

**Phase:** R5 of the recording-pipeline roadmap. Scope: retention policy
enforcement and automatic cleanup only, per the approved architecture and
your explicit preference for straightforward hard deletion over an
archive tier.

**Production backup:** `app/main.py.pre-r5-retention-sweep-20260820-215013`.

**Commit:** `c274c218f785b5e80cc4f935584541ac4b90c826` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree — all three confirmed at this commit with matching file hashes.

**Files changed:**
- `app/recording_retention_sweep.py` *(new)* — periodic cloud-side worker (mirrors `live_relay_idle_sweep.py`'s tick/worker shape) that hard-deletes a recording (S3 object + catalog row) once older than its **owning customer's own current plan's `retention_days`** — never hardcoded. A recording with no plan on file is left alone. A recording is only removed from the catalog after its S3 delete is confirmed, so a failure is retried, not lost track of.
- `app/main.py` *(modified, +14 lines)* — four small additions mirroring the existing worker-wiring shape, dual-gated (`RUNTIME_ROLE` + its own dedicated `ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED` flag, defaulting off).
- `docs/r5-recording-lifecycle-iam.md` *(new)* — the delete-only role's IAM design (a **fourth**, independently-scoped role — upload/read/delete are each their own role now), plus an illustrative S3 bucket lifecycle rule as a failsafe backstop (not the primary mechanism — S3 rules can't read a database column).
- `app/tests/test_recording_retention_sweep.py` *(new)* — 9 tests, including a direct, formal re-verification of the exact approved gate.

**Verification performed:**
- `ast.parse` clean on both files, locally and on production.
- **42/42 tests passed** (9 new + 12 R3 + 9 R1 + 5 R2 + 7 R4) run inside the actual deployed container.
- **The exact approved gate, verified directly**: a 2-day-plan customer's recording 3 days old is confirmed deleted (object-delete called, catalog row removed); a 30-day-plan customer's recording at the same age is confirmed untouched (object-delete never attempted). Also verified: most-recent-plan selection on a plan upgrade, no-plan-on-file safety (never deletes, regardless of age), failed-delete retry-safety, and the exactly-at-the-boundary edge case (not yet expired at exactly N days).
- Confirmed inside the container: worker correctly stays inert on this box (`RUNTIME_ROLE=cloud`, flag unset).
- Real production `recordings` table confirmed still 0 rows after the test run.
- Live-relay, R1, R2, R3, R4, and all customer-facing routes confirmed unaffected. **Zero customer, partner, or admin UI changes** — this phase is entirely a cloud-side background worker.
- `docker compose restart vms` → healthy, no new errors in logs.

**File hash verification (EC2 == GitHub == Ryzen 5), all at commit `c274c21`:**

| File | SHA-256 |
|---|---|
| `app/main.py` | `8af25948...ca14ecd90932abb7af3c1a07` |
| `app/recording_retention_sweep.py` | `c590c504...ec63b5f8acac07b5e15e94f9a` |
| `app/tests/test_recording_retention_sweep.py` | `abdcf18a...94ec242fa0d7f632ce81f9df8` |
| `docs/r5-recording-lifecycle-iam.md` | `eb7628f7...8f23820bd4e9f3a9f65bde76` |

**Rollback:** restore `main.py.pre-r5-retention-sweep-20260820-215013`, delete `app/recording_retention_sweep.py`, restart `vms`. On Git: `git revert c274c21` followed by a push.

**What's implemented (R1 through R5 complete):**
- End-to-end foundation: tenant-safe upload credentials (R1) → durable catalog (R2) → appliance-side uploader (R3) → customer-facing retrieval with presigned URLs (R4) → automatic, plan-derived retention cleanup (R5).
- Four independently-scoped IAM roles designed (upload/read/delete, plus the pre-existing live-relay role), each fails closed until applied.
- Every phase individually tested, verified against the real deployed container, and fully synced across EC2/GitHub/Ryzen 5.

**What still remains before production rollout:**
1. **Three IAM roles must actually be applied in AWS** — `docs/r1-recording-iam.md` (upload), `docs/r4-recording-read-iam.md` (read), `docs/r5-recording-lifecycle-iam.md` (delete) — none of this application code can create them; each requires IAM-admin AWS credentials.
2. **A dedicated recordings S3 bucket** (or prefix decision finalized) with the illustrative failsafe lifecycle rule from `docs/r5-recording-lifecycle-iam.md` actually applied.
3. **Four feature flags flipped on**, in order, only after each is individually verified: `ANYAICAM_RECORDING_UPLOAD_ENABLED` (R1/R3), `ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED` (R5) — `ANYAICAM_RECORDING_READ_ROLE_ARN` (R4) is set as soon as its role exists, no separate boolean flag gates it.
4. **A real pilot appliance** running R3's uploader end-to-end, producing real catalog rows — the R1/R3 architecture gate ("one real end-to-end upload confirmed on the pilot appliance") has not yet been exercised against real AWS.
5. **The playback-format decision is still open** — R3 uploads native MKV; this should be revisited before broad rollout since MKV isn't reliably browser-seekable (the original reason customer Playback was never wired to the legacy local recording scheme in the first place).
6. **Cost and monitoring**: no alerting exists yet on sweep failures, upload failures, or unexpected deletion volume — worth adding before this runs unattended in production.
7. Everything remains fully disabled by default; flipping flags on should happen one at a time, verified at each step, ideally against a single pilot appliance/customer before any broader rollout — matching the same discipline the live-relay rollout (Phase 6f/8) already used.

---

## 2026-08-21 — R4: Connect customer Playback to the recording catalog (foundation only)

**Phase:** R4 of the recording-pipeline roadmap. Scope: customer retrieval
wiring only, per the approved architecture. R5 was explicitly not started.

**Production backup:** `app/main.py.pre-r4-customer-playback-wiring-20260820-213548`.

**Commit:** `b04fe0065c351e6cf252ad762f27e05325754c48` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree — all three confirmed at this commit with matching file hashes.

**Files changed:**
- `app/main.py` — `_customer_camera_recordings(camera_id)` now runs a real query against R2's `recordings` table (`WHERE camera_id=? AND status='available'`), returning the `{start,end,url,name}` shape the Playback UI has expected since it was built. New `_presigned_recording_url(s3_key)` signs GET URLs via a dedicated, read-only STS role — fails closed (`None`) when unconfigured; a recording with no signable URL is skipped, never shown as a dead link.
- `docs/r4-recording-read-iam.md` *(new)* — the read-only role's IAM design + illustrative (not executed) AWS CLI commands, same split as R1's doc.
- `app/tests/test_customer_recordings_r4.py` *(new)* — 7 tests, including a direct, formal re-verification of the approved gate (owner sees all own cameras; viewer with no `can_playback` grant sees none).

**Verification performed:**
- `ast.parse` clean on `main.py`.
- **33/33 tests passed** (7 new + 12 R3 + 9 R1 + 5 R2) run inside the actual deployed container — `main.py` can only be imported there or via Windows-native Python (a pre-existing, already-documented project constraint).
- Confirmed the real production `recordings` table is still **0 rows** after the test run — all test seeding used a throwaway sqlite file via `override_target()`, never the real database.
- Live-relay, R1, R2, R3, and all customer-facing routes (`/customer-live`, `/playback`, `/customer-account`) confirmed unaffected.
- `docker compose restart vms` → healthy, no new errors in logs.
- **Zero frontend changes** — the Playback page's timeline, legend, clip list, and transport controls are byte-for-byte untouched. Zero changes to Live, Settings, partner portal, or admin portal. Subscriptions/entitlements untouched.

**File hash verification (EC2 == GitHub == Ryzen 5), all at commit `b04fe00`:**

| File | SHA-256 |
|---|---|
| `app/main.py` | `b9dd1c68...c72f1c4f0777cabc3c5b086c2` |
| `app/tests/test_customer_recordings_r4.py` | `49fa602d...eeeb2950e3615318d56596` |
| `docs/r4-recording-read-iam.md` | `03b18831...6663bdd` |

**Rollback:** restore `main.py.pre-r4-customer-playback-wiring-20260820-213548`, restart `vms`. On Git: `git revert b04fe00` followed by a push.

**What's implemented:**
- The real tenant-safe query path from the `recordings` catalog to the customer Playback page, gated by the same `customer_owner`/`customer_viewer` scoping already proven for Live and the camera list.
- A working, fail-closed presigned-URL mechanism, ready for real traffic the moment its role exists.
- Formal test coverage of the exact approved access-control gate.

**What still needs to be done before this is customer-visible:**
1. **R1's upload-role IAM** (`docs/r1-recording-iam.md`) must be applied in AWS, and `ANYAICAM_RECORDING_UPLOAD_ENABLED` set true, before any real recording can be uploaded at all.
2. **R4's new read-role IAM** (`docs/r4-recording-read-iam.md`) must *also* be applied — a separate, read-only role from the upload role — before any uploaded recording can actually be served to a customer's browser, and `ANYAICAM_RECORDING_READ_ROLE_ARN`/`ANYAICAM_RECORDING_S3_BUCKET`/`AWS_REGION` set.
3. A real appliance running R3's uploader (pilot appliance, `RUNTIME_ROLE=edge`) needs to actually produce and upload recordings for any catalog row to exist.
4. **R5 (retention/lifecycle)** hasn't been built yet — recordings would currently accumulate with no expiration once real uploads begin.
5. The playback-format decision (native MKV vs. transcoded fMP4 vs. VOD-HLS) is still open — R3 currently uploads native MKV, which may not play/seek reliably in all browsers once real footage exists; this doesn't block R4/R5 but should be revisited before broad rollout.

Until all of #1–#3 above are done, Playback will continue to show "No recordings available yet" for every customer — correctly and safely, by design.

---

## 2026-08-21 — R3: Appliance-side recording uploader (foundation only)

**Phase:** R3 of the recording-pipeline roadmap. Scope: the appliance-side
uploader module and its `main.py` wiring only, per the approved
architecture. R4 was explicitly not started.

**Production backup:** `app/main.py.pre-r3-recording-uploader-20260820-211216`
(the one existing file this phase modified; the uploader module and its
test file are new).

**Commit:** `6ffdbf586cb1f57dc6cb3ca12c07a978b9e7426c` on branch
`production-reconcile-20260820` (amended once to fix a commit-message
quoting bug on my end — the file changes in the amended commit are
identical to the original; only the message text changed), pushed to
GitHub, synced to the Ryzen 5 worktree — all three confirmed at this
commit with matching file hashes.

**Files changed:**
- `app/recording_uploader.py` *(new)* — watches each camera's completed local recording files, requests R1 credentials, uploads via boto3, notifies R2's catalog endpoint. Mirrors `live_relay_uploader.py`'s security model exactly (duplicated, not imported). Not gated by a per-camera "active" flag (recording is continuous, unlike on-demand live viewing). Failed uploads are retried on the next scan rather than dropped, since a recording is durable evidence. No transcoding — uploads MKV as produced; the playback-format decision remains open for R4/later.
- `app/main.py` *(modified, +10 lines)* — four small additions mirroring `live_relay_task`'s exact lifespan wiring shape (import, task creation, cancel-on-shutdown, graceful-wait append).
- `app/tests/test_recording_uploader.py` *(new)* — 12 tests against a monkeypatched `RECORDINGS_FOLDER`, never touching the real path.

**Verification performed:**
- `ast.parse` clean on both files, locally and on production.
- **26/26 tests passed** (12 new + 9 R1 + 5 R2) run inside the actual deployed container via the same ephemeral pytest install/cleanup pattern as R2.
- Confirmed inside the container: `RUNTIME_ROLE=cloud` on this box, so the new worker task correctly stays `None`/inert — identical to how `live_relay_task` already behaves here.
- All 4 `main.py` wiring points confirmed present.
- Live-relay route, R1's credentials route, R2's notification route, and all customer-facing routes (`/customer-live`, `/playback`, `/customer-account`) confirmed unaffected (still their expected `401`/`303`).
- `docker compose restart vms` → healthy, no new errors in logs.
- Recordings table confirmed still empty (0 rows) — nothing has actually uploaded, by design (flag off, real IAM role not yet applied).

**File hash verification (EC2 == GitHub == Ryzen 5), all at commit `6ffdbf5`:**

| File | SHA-256 |
|---|---|
| `app/main.py` | `699d68cc...0210a6262b3fb8b8c` |
| `app/recording_uploader.py` | `61d60b2d...36a60387f37b936ae4bec8f` |
| `app/tests/test_recording_uploader.py` | `b34cb5f9...b90a05fe948f49c92` |

**Rollback:** restore `main.py.pre-r3-recording-uploader-20260820-211216`, delete `app/recording_uploader.py`, restart `vms`. On Git: `git revert 6ffdbf5` followed by a push.

**Not done in R3 (explicitly deferred):** no customer-facing retrieval or Playback wiring (R4), no retention/lifecycle sweep (R5), no analytics association (R6/R7), no per-appliance pilot gate. Still fully disabled by default — the flag is unset and the real AWS IAM role from R1's design doc has not been applied, so no real upload can occur yet even if the flag were flipped alone.

---

## 2026-08-21 — R2: Recording catalog table + notification endpoint

**Phase:** R2 of the recording-pipeline roadmap. Scope: the `recordings`
catalog table and its notification endpoint only, per the approved
architecture. R3 was explicitly not started.

**Production backups:**
- `app/db_migrations.py.pre-r2-recordings-catalog-20260820-205758`
- `app/appliance_cloud.py.pre-r2-recordings-catalog-20260820-205758`

**Commit:** `255d28991a5e57a6d768e3bc5a9c22ff3b0353e1` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree — all three confirmed at this commit with matching file hashes.

**Files changed:**
- `app/db_migrations.py` — new `20260821_recordings_catalog` migration: `recordings` table (`customer_id`/`site_id`/`appliance_id`/`camera_id`/`s3_key`/`started_at`/`ended_at`/`duration_seconds`/`size_bytes`/`status`/`created_at`), `UNIQUE(camera_id,s3_key)` for idempotent replay, `idx_recordings_camera_started` for R4's future timeline query.
- `app/appliance_cloud.py` — new `POST /api/appliance/recordings/{camera_id}/available` route, mirroring `live_relay_segment_available()`'s exact auth/flag/prefix-validation shape; still gated behind `ANYAICAM_RECORDING_UPLOAD_ENABLED` (default off).
- `app/tests/test_recordings_catalog.py` *(new)* — 5 tests against a throwaway sqlite DB via the real `initialize_database()` chain.

**Verification performed:**
- `ast.parse` clean on both edited files, locally and on production.
- Migration applied automatically on `docker compose restart vms` — confirmed directly against the real production DB: `recordings` table present with all 12 expected columns, **0 rows**, `schema_migrations` records `20260821_recordings_catalog`.
- All **14** R1+R2 tests (9 credential tests + 5 catalog tests) run **inside the actual deployed container** via an ephemeral `pip install --target=` of pytest (removed immediately after — confirmed `import pytest` fails again post-cleanup, no permanent change to the container's dependencies) — **14/14 passed**.
- New route returns the same `422`/`401` shape the existing `segment-available` route already returns for a missing/unauthenticated request — not a new pattern, matches precedent exactly.
- Live-relay route and R1's credentials route both confirmed unaffected (still `401`).
- `docker compose restart vms` → healthy, no new errors in logs.
- Customer-facing routes (`/customer-live`, `/playback`, `/customer-account`) confirmed unchanged — this phase touched zero customer-facing code.

**File hash verification (EC2 == GitHub == Ryzen 5), all at commit `255d289`:**

| File | SHA-256 |
|---|---|
| `app/db_migrations.py` | `9d2c67aa...86788dc39` |
| `app/appliance_cloud.py` | `b493894a...7034b73648f` |
| `app/tests/test_recordings_catalog.py` | `3db7bc35...ea0b59e3dc10` |

**Rollback:** restore both `.pre-r2-recordings-catalog-20260820-205758` backups, restart `vms` — the `recordings` table itself is harmless to leave in place (empty, nothing reads or writes it once the route/migration code is reverted), or can be dropped manually if desired. On Git: `git revert 255d289` followed by a push.

**Not done in R2 (explicitly deferred):** no appliance-side uploader that calls this route (R3), no customer-facing retrieval or Playback wiring of any kind (R4), no retention/lifecycle sweep (R5), no analytics association (R6/R7), no per-appliance pilot gate. Subscriptions/entitlement behavior untouched.

---

## 2026-08-21 — R1: Recording-upload credential issuance (foundation only)

**Phase:** R1 of the recording-pipeline roadmap (distinct from the earlier
Live Phase 1–8 numbering). Scope: IAM design + credential-issuance
endpoint only, per the approved architecture. R2 was explicitly not
started.

**Production backup:** `app/appliance_cloud.py.pre-r1-recording-credentials-20260820-204658`
(the one existing file this phase modified; all other changes were new files).

**Commit:** `569ea0ff9e99edfbb080ee2dad9a98f5b83b843a` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree (`/home/alejandro/anyaicam-production-reconcile`) — all three
confirmed at this same commit SHA with matching file hashes below.

**Files changed:**
- `app/recording_credentials.py` (new) — tenant-scoped prefix/policy/session-name helpers, mirroring `appliance_protocol.py`'s live-relay equivalents.
- `app/appliance_cloud.py` (modified) — new `POST /api/appliance/recordings/{camera_id}/credentials` route, gated behind `ANYAICAM_RECORDING_UPLOAD_ENABLED` (default off, not set in production) plus unset role ARN/bucket — fails closed (404 disabled / 503 unconfigured).
- `app/tests/test_recording_credentials.py` (new) — 9 tests, all passing, proving per-camera and per-customer prefix/policy isolation.
- `docs/r1-recording-iam.md` (new) — IAM role design + illustrative (not executed) AWS CLI commands; real role creation requires IAM-admin AWS credentials this application does not have.

**Verification performed:**
- `ast.parse` clean on both edited/new Python files, locally and on production.
- `pytest app/tests/test_recording_credentials.py` — 9/9 passed (run in the local `anyaicam` venv; the production container has no pytest installed).
- Inside the running container: `import appliance_cloud` succeeds; `recording_s3_prefix()`/`recording_session_policy()` produce correct, isolated output.
- `docker compose restart vms` → `healthy`; no new errors/tracebacks in logs.
- New route confirmed registered and reachable: unauthenticated `POST /api/appliance/recordings/{camera_id}/credentials` returns `401`, identical to the existing live-relay route's own unauthenticated response — proving it's wired through the same `authenticate_appliance()` gate without weakening it.
- Live-relay route (`/api/appliance/live/{camera_id}/session`) confirmed unaffected — same `401` as before this change.
- No customer-facing route, page, or behavior touched — this phase is 100% backend/appliance-facing and entirely inert (feature flag off) even once deployed.

**File hash verification (EC2 == GitHub == Ryzen 5):**

| File | SHA-256 |
|---|---|
| `app/recording_credentials.py` | `946ae120...66f8bffa` |
| `app/appliance_cloud.py` | `c1fe7127...0ded95e` |
| `app/tests/test_recording_credentials.py` | `b5bb06a5...7dfdf591` |
| `docs/r1-recording-iam.md` | `831e1fee...2909ad48` |

**Rollback:** `cp app/appliance_cloud.py.pre-r1-recording-credentials-20260820-204658 app/appliance_cloud.py`, delete `app/recording_credentials.py`, restart `vms` — reverts production to its R0 state. On Git: `git revert 569ea0f` (or reset the branch to `5f42bd4`) followed by a push.

**Not done in R1 (explicitly deferred):** no `recordings` catalog table or migration, no notification endpoint, no appliance-side uploader, no customer-facing retrieval/Playback wiring, no retention/lifecycle logic, no analytics association, no per-appliance pilot gate (mirrors how live relay's own per-appliance gate arrived much later, as its own phase) — all of that is R2 onward.

---

## 2026-08-21 — R0: Source-of-truth reconciliation

**Phase:** R0 (recording-pipeline roadmap; distinct from the earlier Live
Phase 1–8 numbering).

**Goal:** make production EC2's actual running state the safely captured
source of truth, without overwriting any existing GitHub branch.

**Trigger:** production EC2's working tree had diverged from its own
checked-out branch by 55 files, +456,933/−348,905 uncommitted lines,
accumulated through weeks of direct hand-edits with no corresponding
commits — discovered during an audit against `docs/AI_HANDOFF.md`.

### Actions taken

1. **Production backup/snapshot** — complete tar snapshot of the EC2 working
   tree taken before any Git operation:
   - Location: `/home/ubuntu/anyaicam-snapshots/production-reconcile-20260820-202801.tar.gz`
   - SHA-256: `d1f9c1f73e063eb129e78e6a29671eb8f8c226ed53b4e789b4c2a511d0843012`
   - 399 entries, 23 MB. Excludes only `.git/` and 5 pre-existing legacy
     directory-level backups (see below) — includes `recordings/` and every
     source/config file.

2. **New branch created from current production state:**
   - Old EC2 branch/commit: `feature/phase6-logout-csrf-hardening` @ `393ffc7a80a43b135ce9b8f5a3a422bf81b1c1b9`
   - New branch: `production-reconcile-20260820`
   - New commit: `4371cd9c4a8a1a230282cb0670591e1f1cafa30c`
   - 104 files changed, +3,101,318 / −348,905 (the working-tree drift, now committed)
   - `main`, `build/v1.2-modular-foundation`, and `feature/phase6-logout-csrf-hardening`
     were **not** modified, rebased, or force-pushed — verified unchanged on
     GitHub after the push (`main`@`d08282f`, `build/v1.2-modular-foundation`@`f0ee662`,
     `feature/phase6-logout-csrf-hardening`@`393ffc7`, all identical to before).

3. **Excluded from the commit** (left on EC2 disk, deliberately not
   version-controlled — captured in the tar snapshot above, but not in Git):
   - `.env.pre-global-relay-enable-20260818-024745`, `.env.pre-live-relay-disabled-20260818-021303`,
     `.env.pre-runtime-cloud-20260818-023138` — dated `.env` backups containing
     real secrets (`ANYAICAM_ADMIN_PASSWORD`, `ANYAICAM_APP_SECRETS`, etc.);
     `.gitignore` only covers the literal `.env`, not these variants.
   - `app-backup-before-45cabb4/` (3.0 GB), `app-backup-before-dd3f9b6/` (11 MB),
     `app-live-before-dd3f9b6/` (11 MB), `live-relay-staging-20260818-014324/` (1.4 MB),
     `pre-live-relay-deploy-20260818-014324/` (1.3 MB) — pre-existing full
     directory-level backups from earlier sessions, not source code.

4. **GitHub push:** `production-reconcile-20260820` pushed to
   `github.com:alexmata25/AnyAICam.git`, confirmed present via `git ls-remote`.

5. **Ryzen 5 sync:** the existing OneDrive checkout
   (`.../Desktop/AnyAiCam-VMS`, branch `build/v1.2-modular-foundation`) was
   **not** touched — its own branch and 19 CRLF-only diffs (already confirmed
   content-identical to GitHub via `git diff --ignore-all-space`) were
   preserved exactly as found. A new, separate `git worktree` was created
   instead at `/home/alejandro/anyaicam-production-reconcile`, checked out to
   `production-reconcile-20260820` @ `4371cd9`, 0 uncommitted files, 0 CRLF
   lines (clean native-Linux checkout — the CRLF issue is specific to the
   OneDrive-backed path and doesn't recur here).

### Verification — file hash cross-check

| File | EC2 disk | GitHub branch (`git show`) | Ryzen 5 worktree | Match |
|---|---|---|---|---|
| `app/main.py` | `0b269ad3...a6140cf` | `0b269ad3...a6140cf` | `0b269ad3...a6140cf` | ✅ |
| `app/live_view_page.py` | `b851d97f...2e82678` | `b851d97f...2e82678` | `b851d97f...2e82678` | ✅ |
| `app/live_view_sessions.py` | `0d532739...214332e` | `0d532739...214332e` | `0d532739...214332e` | ✅ |
| `app/customer_platform.py` | `1e198157...aeb3e3` | `1e198157...aeb3e3` | `1e198157...aeb3e3` | ✅ |
| `app/partner_workspace.py` | `0a047bcf...277874` | `0a047bcf...277874` | `0a047bcf...277874` | ✅ |
| `app/partner_portal.py` | `ab0aaf75...857c54b2` | `ab0aaf75...857c54b2` | `ab0aaf75...857c54b2` | ✅ |

**EC2 production application code == `production-reconcile-20260820` @ `4371cd9` on GitHub == Ryzen 5 working baseline.**

### Rollback / recovery

- **Undo the branch entirely:** delete `production-reconcile-20260820` locally
  and on GitHub (`git push origin --delete production-reconcile-20260820`) —
  no other branch was touched, so this is fully reversible with zero impact
  on `main`/`build/v1.2-modular-foundation`/`feature/phase6-logout-csrf-hardening`.
- **Restore any individual file:** `git show 393ffc7:app/<file>` (pre-R0) or
  extract from the tar snapshot above (full pre-commit disk state).
- **No service restart occurred** — this was a pure Git operation; deployed
  application files were not modified.

### Safety confirmations

- Production `vms` service was **not** restarted (no application files were
  changed by R0 — only Git metadata).
- No application behavior was modified.
- R1 (recording pipeline implementation) was **not** started.
