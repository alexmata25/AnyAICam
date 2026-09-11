# AnyAiCam — Project Checkpoint (read this first, every session)

**This file is the project's memory, not the AI's.** Before any AI session
begins development work on AnyAiCam, it must:

1. Read this file.
2. Read the authoritative-branch section below and confirm which Git
   branch/commit is the current golden foundation.
3. Read the checkpoint file for whichever appliance/workstream is being
   resumed (`docs/checkpoints/RYZEN.md`, `docs/checkpoints/SAMSUNG.md`).
4. Verify the actual current state (SSH in, check running commit/image, check
   the real database) against what the checkpoint claims — a checkpoint is a
   snapshot, not a guarantee, and drift between sessions is exactly the
   failure mode this file exists to catch.
5. Only then decide the next step. Do not reconstruct project history from
   chat memory, and do not re-fix something this file says is already fixed
   without first checking whether it's actually still broken.

Before switching appliances, branches, or major workstreams — or ending a
session with meaningful progress made — **update the relevant checkpoint file
and commit it**, in the same spirit as this file's own existing update
discipline. A checkpoint that isn't updated is worse than no checkpoint: it
actively misleads the next session.

---

## Why this file exists

Investigated 2026-09-11: real, completed work had spread across multiple
diverged Git branches with no single source of truth, and several real fixes
existed only as manual runtime patches on Ryzen, staging, or AWS — never
committed to source. This meant the same bugs (empty AWS_REGION building a
malformed S3 endpoint, a missing `restart_count` column, portal URL not
persisting through activation) were independently rediscovered and re-fixed
multiple times across different sessions and branches, because nothing told
the next session they'd already been found and fixed elsewhere.

See `docs/reconciliation-2026-09-11.md` for the full inventory, conflict
resolution, and test evidence behind the reconciliation this file records.

---

## Authoritative branch

**Current golden foundation:** `reconcile/golden-foundation-20260911`

This branch merges, with deliberate conflict resolution (not a blind merge):
- `claude/customer-provisioning-phase1` — customer entitlements, Stripe
  checkout/webhooks, hardware fulfillment/returns, tenancy
- `staging/cloud-integration-repair` — the Samsung-validated claim flow,
  installer/privileged-watcher fixes, agent config-precedence fix
- `codex/motion-event-media-cloud-flow` — motion-cloud event-media transport
  (Phase C real-hardware validated on Ryzen), Live View fixes (degraded ≠
  offline, malformed JS template literal), AWS_REGION fail-loud guard,
  `restart_count` migration

**RC1 was deployed to Ryzen (2026-09-11, clean install) and failed
validation** — see "RC1 → RC2 on Ryzen" below. **RC2 fixed that source
defect and was deployed to Ryzen, where its own `validate.sh` surfaced a
second, independent, installer-only defect** — see "RC2 → RC3 on Ryzen"
below. **RC3 fixes that and has been deployed to Ryzen (repair-path
install over the existing RC2 install) and PASSED validate.sh with 0
failures** — see "RC3 on Ryzen — PASSED" below. RC3 is the first golden
build to pass its own installer's full validation on real hardware.
**Claim/activation and camera discovery have NOT been performed yet** —
do not treat those as done.

```
GOLDEN BUILD: golden-foundation-rc3 — commit 4ade235
  (full sha 4ade2352b1ea6da9c56a339650773c01879c0b98)
Built: 2026-09-11, from `reconcile/golden-foundation-20260911` worktree
  `AnyAiCam-VMS-reconciliation`.
VMS container image: IDENTICAL to RC2's -- `git diff --stat 34d9d5c 4ade235
  -- app/ appliance-agent/ Dockerfile requirements.txt requirements-cpu.txt`
  is empty; this commit only changes `installer/validate.sh` and
  `installer/tests/run_tests.sh`. No new VMS image was built; RC3's
  installer, when run, produces the exact same VMS image RC2's did
  (digest sha256:6ff38cc2c81f6398ce6b95abf42ce5056a8f9eec5a67258dd1f2cb06e9f930af,
  tags anyaicam-vms:golden-rc2/golden-rc3/34d9d5c/4ade235 all equivalent).
Installer artifact (the actual thing to deploy — built via
  installer/build_release_installer.py --vms-commit 4ade2352b1ea6da9c56a339650773c01879c0b98):
  anyaicam-appliance-installer-1.1.0-vms-4ade2352b1ea.tar.gz
  artifact sha256: f8797d627a580363f2f1807c9a7d9b91f29c73df84a4348b30fee49afc56e5c7
  embedded source sha256: 051236ff6cd14821019e711e66f97ae061c5a6e539b84b6c631553ff74a3fa21
Tested: installer/tests/run_tests.sh (66 passed incl. 5 new
  ready_endpoint_self_test_ok() cases, same 11 pre-existing unrelated
  failures as the unmodified script) + installer/tests/test_build_release_installer.py
  (12 passed, 1 pre-existing skip).
Deployed to: Ryzen (2026-09-11), repair-path install over the existing
  RC2 install (detect_install_state() correctly reported 5/5 markers ->
  existing, not clean). validate.sh PASSED: 0 failures -- see "RC3 on
  Ryzen — PASSED" below for the full result.

Prior build (RC2, source defect fixed, but its own installer's validate.sh
had a second, independent defect — see "RC2 → RC3 on Ryzen" below):
GOLDEN BUILD: golden-foundation-rc2 — commit 34d9d5c
  (full sha 34d9d5ca96b674f0d4a7212bbf7996d52f063fa0)
VMS container image digest (local build, not yet pushed to a registry):
  sha256:6ff38cc2c81f6398ce6b95abf42ce5056a8f9eec5a67258dd1f2cb06e9f930af
  tags: anyaicam-vms:golden-rc2, anyaicam-vms:34d9d5c
Installer artifact:
  anyaicam-appliance-installer-1.1.0-vms-34d9d5ca96b6.tar.gz
  artifact sha256: 8912bcd17041cad1ad0cbf813a5dcb148b23deb2dd1e0c7b1da17893a3283566
Deployed to: Ryzen (2026-09-11) as a clean install; install itself
  succeeded, but its bundled `validate.sh` failed on the second defect
  above. **Still installed and running on Ryzen as of this writing** --
  not wiped, not touched further (no patches, no restart, no camera
  discovery, no claim) since the failure was diagnosed. See
  "RC2 → RC3 on Ryzen" below and `docs/checkpoints/RYZEN.md` for Ryzen's
  exact current live state before assuming otherwise.

Prior build (RC1, failed validation — kept for history, do not deploy):
GOLDEN BUILD: golden-foundation-rc1 — commit 1dfcbf2
  (full sha 1dfcbf24b704ac4a523560ba7f9441838815fe53)
VMS container image digest:
  sha256:ac680f4201bb7324b135db09559a8c52e584cf5e333bfdf01c6890df23fceff7
  (image config sha256:254524377ad50667be006fdcc646c03221cf02164b03c4cc49bd7499f9c11d67)
appliance-agent package (anyaicam-appliance-agent 0.1.0), built separately --
  it ships to appliance hosts via the installer, not inside the VMS image:
  wheel sha256: 266b649779cb13f55302cbdb740c76c59d2e34acff92aa3caf2f109c32016473
  sdist sha256: 9977b2b7f5471982d97ef51c7f5b57fecabcced14051a80c5596ada8b894afe5
Tested: 2026-09-11, disposable Docker Desktop environment on the Dell/
  OneDrive dev machine (no Ryzen/Samsung/staging/AWS touched). See
  "RC1 validation" below for full results.
Deployed to: Ryzen (2026-09-11), then wiped after validate.sh failed --
  see "RC1 → RC2 on Ryzen" below. No longer running anywhere.
```

Note on reproducibility: the image build is NOT byte-for-byte deterministic
across separate builds (confirmed: an earlier build attempt of the same
Dockerfile+commit produced a different image ID) because `requirements.txt`
pins direct dependencies only -- several transitive dependencies (certifi,
packaging, six, etc.) float to whatever's current on PyPI at build time.
The build IS reproducible in the sense that mattered for this exercise: it
completes from a clean `docker build` with zero manual runtime patches, on
every attempt, from this exact source commit.

---

## RC1 → RC2 on Ryzen (2026-09-11)

RC1 (`1dfcbf2`) was deployed to Ryzen as a genuine clean install (Ryzen's
prior AnyAiCam state -- recordings, activation, agent install -- was
explicitly wiped first, per direct instruction, via `installer/
uninstall.sh --purge-all`'s own target list; disk was reclaimed from 100%
full/0 bytes free down to 346GB free first). This was deliberately the
first-ever zero-manual-patch validation of a truly clean install:
`installer/build_release_installer.py` built a self-contained installer
artifact from the exact commit, transferred via `scp`, SHA-256-verified on
both ends before running, then `sudo bash install.sh` — no hand-copied
source, no runtime edits.

Install itself succeeded (`detected state=clean`, exact commit verified,
both systemd units enabled/active, real appliance identity issued). The
installer's own `validate.sh` then failed: `GET /ready` returned 503.

**Root cause (confirmed via source read + live diagnostics, not
guesswork):** `configuration_issues()` in `app/main.py` required
`AWS_REGION`/external database/S3/public URL/Secrets Manager to be
configured directly on the VMS container whenever `ANYAICAM_ENV=production`
— with no check of `ANYAICAM_RUNTIME_ROLE` at all. This contradicted
`readiness_snapshot()`'s own deliberate role scoping (cloud requirements
only apply to `cloud`/`combined` roles, never `edge`), so every
production-edge appliance was permanently un-ready regardless of
camera/claim state. `ANYAICAM_FORCE_HTTPS` had the identical shape against
its own already-role-aware default (`_default_force_https()`). No prior
Ryzen session ever caught this because AWS_REGION etc. were always
already hand-patched into `vms.env` from earlier, unrelated motion-cloud
validation work — this was the first truly clean install ever checked
against `GET /ready`.

**Fixed in commit `34d9d5c`** (on top of `1dfcbf2`): both checks are now
gated on `RUNTIME_ROLE` the same way `readiness_snapshot()` already is.
Cloud/combined production behavior is completely unchanged. Regression
coverage: `app/tests/test_ready_endpoint_role_aware_configuration.py` (10
tests). Full `app/tests` re-run shows zero new failures from this change.
Locally smoke-tested (see GOLDEN BUILD block above): `self_test.ok`
flips true, `configuration_valid` critical count 6 → 0, for the exact
production+edge+zero-cloud-config shape Ryzen hit.

RC2 (`34d9d5c`) was then deployed to Ryzen the same way as RC1 (Ryzen
purged again first via `uninstall.sh --purge-all`, fresh transfer,
SHA-256 verify, `sudo bash install.sh`). See "RC2 → RC3 on Ryzen" below
for what that deployment found.

---

## RC2 → RC3 on Ryzen (2026-09-11)

RC2 (`34d9d5c`) installed clean on Ryzen exactly like RC1 (Ryzen was
purged again first: `uninstall.sh --purge-all`, plus removal of two
unrelated stale Docker volumes from an old `test1` compose project,
`anyaicam-test1_test1_hls`/`anyaicam-test1_test1_recordings`, confirmed
via their on-disk content/timestamps to be ~2-week-old stale lab-test
data, not RC1 leftovers). `install.sh` reported `detected state=clean`
and the exact approved commit. `validate.sh` then reported exactly one
failure: `FAIL: VMS local ready endpoint responds`.

Live diagnostics on Ryzen (`curl -s -w '\nHTTP_STATUS:%{http_code}\n'
http://127.0.0.1:8000/ready`, read-only, no patches) confirmed the RC1
defect was genuinely fixed: `self_test.ok: true`, `configuration_valid:
true`, `0 critical` issues. `/ready` still returned HTTP 503 only because
`ready: false` — expected, since a fresh, unclaimed, zero-camera edge
appliance has `recording_workers: 0`, and `readiness_snapshot()`
deliberately requires `recording>0` for the edge role.

**Root cause (a second, independent, installer-only defect — confirmed
by reading `installer/validate.sh`, not guesswork):** the ready-endpoint
check used `curl -fsS ... /ready`; curl's `-f` flag treats *any* non-2xx
HTTP status as failure. Since a genuinely fresh appliance's `/ready`
legitimately returns 503 until it has cameras, `validate.sh` could never
pass on a truly clean install — it was conflating "the VMS process
started correctly" (`self_test.ok`) with "this specific appliance
already has a camera recording" (`ready`), a business-readiness
condition `install.sh` never establishes and `validate.sh` was never in
a position to require.

**Fixed in commit `4ade235`** (on top of `34d9d5c`): `installer/
validate.sh`'s check now fetches `/ready` without `-f` (so a 503 still
yields the JSON body) and asserts `self_test.ok` directly instead of the
HTTP status code. Still correctly fails if the VMS is genuinely
unreachable or actually broken. `validate.sh`'s top-level check
invocations were also refactored into a `run_validate()` function behind
the same source-vs-execute guard `install.sh`/`uninstall.sh` already
use, making it safely unit-testable for the first time. Regression
coverage: `installer/tests/run_tests.sh`, 5 new cases (the exact
Ryzen/RC2 shape passes; RC1's real defect shape still fails; a fully
ready appliance still passes; an unreachable VMS still fails; a
malformed response still fails).

RC3 (`4ade235`) was built (installer artifact only — its VMS image is
byte-identical to RC2's, `app/`/`appliance-agent/`/`Dockerfile`/
requirements untouched by this commit), then deployed to Ryzen. See
"RC3 on Ryzen — PASSED" below.

---

## RC3 on Ryzen — PASSED (2026-09-11)

RC3 (`4ade235`) was transferred to Ryzen, SHA-256-verified on both ends
(artifact `f8797d627a580363f2f1807c9a7d9b91f29c73df84a4348b30fee49afc56e5c7`),
extracted, and `release.env` confirmed (`VMS_RELEASE_COMMIT`/
`INSTALLER_SOURCE_COMMIT` both `4ade2352b1ea6da9c56a339650773c01879c0b98`,
`VMS_RELEASE_SHA256` `051236ff6cd14821019e711e66f97ae061c5a6e539b84b6c631553ff74a3fa21`)
before running anything. `sudo bash install.sh` was then run directly
over the existing RC2 install (no wipe this time — RC2 was left running
untouched since its `validate.sh` failure was diagnosed).

`install.sh` correctly detected **`existing`** (5/5 markers), routing
through the repair path rather than clean-install — confirmed, not
assumed. Existing appliance identity was preserved (not regenerated).
Installed release confirmed as the exact approved commit.

**`validate.sh` PASSED with 0 failures** — the first golden build to
pass its own installer's full validation on real hardware:
- Health endpoint: PASS
- Ready endpoint reachable + `self_test` PASS (the `4ade235` fix, live)
- Zero-camera business readiness correctly **not** required at install
  time (the exact behavior `ready_endpoint_self_test_ok()` was written
  to produce)
- `/version` reports exact approved commit: PASS
- All other pre-existing checks (systemd units, quarantine directory,
  suspend/hibernate masking, release markers, etc.): PASS

This physically confirms both the `34d9d5c` role-aware-configuration fix
and the `4ade235` `validate.sh` fix on real hardware, not just in
regression tests or a disposable local container.

**Ryzen's current real state:** RC3 installed and running, `validate.sh`
clean. **Still unclaimed, zero cameras** — no claim/activation and no
camera discovery have been performed. No runtime patches applied at any
point in the RC1→RC2→RC3 sequence. See `docs/checkpoints/RYZEN.md` for
the exact current state before the next session acts on it.

---

## RC1 validation (2026-09-11, `reconcile/golden-foundation-20260911` @ 1dfcbf2)

Full disposable-environment validation record; see the session that
produced `1dfcbf2` for complete command-level detail. Summary:

- **Cold start / health**: clean `docker run` with a minimal env file (no
  manual runtime patches) reaches `/health` → 200 within seconds; startup
  log shows the real `startup.complete` event with `cloud_foundation_ready:
  false` (expected — no AWS/DB configured in this disposable env) and
  `upload_worker: disabled`.
- **DB migrations**: a completely fresh database via
  `partner_db.initialize_database()` + `db_migrations.apply_migrations()`
  has the `appliances.restart_count` column. Confirmed both by this direct
  check and by `app/tests/test_appliance_restart_count_migration.py`.
- **AWS_REGION fail-loud guard**: confirmed live inside the built image —
  an empty `AWS_REGION`/`AWS_DEFAULT_REGION` raises the specific
  `RuntimeError` before ever constructing an S3 client, in both
  `recording_uploader.py` and `live_relay_uploader.py`.
- **cloud_upload_worker() retirement**: confirmed live inside the built
  image — `worker_status` is permanently
  `retired_superseded_by_recording_uploader` even with the legacy
  `ANYAICAM_CLOUD_UPLOAD_ENABLED=true` set. `recording_uploader.py` is the
  sole automatic upload pipeline. Defect class 6 below is now CLOSED (was
  "verify before assuming this is closed" as of the prior checkpoint).
- **Config/data persistence across container recreate**: a marker file
  written to a named volume survived `docker rm` + recreate from the same
  image + same volumes (not just a restart) — proves state lives in the
  volumes, not the writable container layer, matching
  `docker-compose.yml`'s own volume design.
- **appliance-agent test suite** (the actual location of the defect #3
  fix): 479/479 passed on Linux (the real deployment platform) inside a
  container built from this same image, including 3 new regression tests
  for defect #3 (see below).
- **app/tests**: 1587 passed, 63 failed, 22 skipped, run inside the built
  image with `AWS_REGION` set (matching what a real appliance's env would
  have). The 63 failures were spot-checked and trace to the same
  pre-existing, harness/fixture-dependent gaps `docs/reconciliation-2026-09-11.md`
  already documented as unrelated to reconciliation (e.g. `total_camera_
  slots()` returning 0 without a fully-seeded tenant/entitlement fixture) —
  not individually re-root-caused one by one here, since commit `1dfcbf2`
  cannot have changed any of their outcomes (see next point).
- **tests/, ops/tests, installer/tests**: 360 passed, 46 failed, plus 9
  files (`test_tenant_onboarding.py`, `test_unified_customer_platform.py`,
  `test_tenancy_policy.py`, `test_camera_provisioning_health.py`,
  `test_customer_experience.py`, `test_edge_camera_discovery.py`,
  `test_edge_streaming.py`, `test_multitenant_integration.py`, plus the
  non-Python `test_csrf_browser_wrapper.js`) that fail to collect because
  they import modules (`tenancy.*`, bare `partner_db`, `app.edge*`,
  `customer_experience`) that do not exist anywhere in this codebase —
  confirmed via `git log`, every one of these files was last touched
  2026-08-06, over a month before the reconciliation fork point
  (`ec5272f`, 2026-09-08). These are pre-existing orphaned test files, not
  something this session's work broke.
- **Why `1dfcbf2`'s defect #3 fix cannot have regressed app/, tests/,
  ops/, or installer/**: `git diff --stat c65c6e1 1dfcbf2` touches exactly
  two files, both under `appliance-agent/`
  (`anyaicam_agent/config.py`, `tests/test_config.py`). Every file under
  `app/`, `tests/`, `ops/`, and `installer/` is byte-identical between the
  previously-checkpointed commit `c65c6e1` and `1dfcbf2`, so every test
  result in those directories is provably unaffected by this fix.
- **Not exercised in this disposable environment** (out of scope for a
  Windows dev-machine Docker build, and consistent with "do not touch
  Ryzen/Samsung/staging/AWS"): real camera/RTSP hardware, a live Postgres
  backend, real AWS S3/CloudFront, and Stripe. Local/edge-mode code paths,
  the claim/activation logic, and all cloud-flag/migration/guard logic
  were validated at the source/regression-test level instead.

**Cloud feature flag disposition** (all four default `false` in source;
none require a code change to remain that way for a fresh golden install):
- `ANYAICAM_CLOUD_UPLOAD_ENABLED` — **obsolete.** `cloud_upload_worker()`
  is now permanently retired regardless of this flag (see defect 6). Must
  not be set `true` on any new install; the legacy `/api/cloud-recording`
  admin panel stays inert-but-present for continuity, not function.
- `ANYAICAM_RECORDING_UPLOAD_ENABLED` — the real, authoritative recording-
  upload pipeline flag. Correct to default `false` for a fresh/local-only
  install; must be explicitly set `true` (with `AWS_REGION` and S3
  configured) for any appliance whose plan includes cloud recording.
- `ANYAICAM_ANALYTICS_SYNC_ENABLED`, `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED`
  — correct to default `false` in source for a fresh install. Distinct
  from the source default: once an appliance is legitimately provisioned
  for Hybrid/cloud features (Samsung is mid-way through this), these two
  must never be silently reverted to `false` — that's an operational
  config-management rule, not a source-code default, and is unchanged by
  this RC.

---

## Known, confirmed-live defect classes to never silently re-introduce

These were each found independently more than once across different
sessions/branches before being fixed once, permanently, in the golden branch.
If a symptom matching one of these appears again, check whether the fix is
actually present in the code currently running — do not assume it's a new
bug and re-invent a fix:

1. **Empty `AWS_REGION`/`AWS_DEFAULT_REGION`** silently builds
   `https://s3..amazonaws.com` and fails uploads with a cryptic
   `ValueError: Invalid endpoint`. Fixed in both `recording_uploader.py` and
   `live_relay_uploader.py` — both now raise a specific `RuntimeError` before
   ever constructing an S3 client. Regression tests:
   `app/tests/test_aws_region_configuration_guard.py`.
2. **Missing `appliances.restart_count` column** — any database built from
   `db_migrations.py` alone must have it; heartbeat 500s otherwise the moment
   an appliance reports a restart. Regression tests:
   `app/tests/test_appliance_restart_count_migration.py`,
   `app/tests/test_appliance_cloud_restart_count.py`.
3. **Activation not persisting portal URL/cloud identity** — `AgentConfig.
   load()` must not let the installer's own untouched bootstrap placeholder
   silently override a value a successful claim/activation already
   established; `_finish_enrollment()` must write `ANYAICAM_CLOUD_URL` into
   the VMS's own `vms.env` and queue a VMS restart on every (re-)activation.
   Both live in `appliance-agent/anyaicam_agent/config.py` and
   `setup_wizard.py`. **CLOSED as of commit `1dfcbf2` (2026-09-11).** The
   remaining gap — a real, non-default, but *stale* env value (not the
   installer's bootstrap placeholder) from a PRIOR activation could still
   win over a NEWER activation's persisted value, Ryzen's exact failure
   mode — is fixed: once `agent.json` holds a real value for
   `cloud_id`/`portal_url`/`mode`, no environment variable overrides it
   here at all, full stop (previously only a value equal to the field's
   own default was protected). The only supported way to change these
   fields on an activated appliance is re-running setup
   (`first_enroll()`/`coordinated_reenroll()`), which writes `agent.json`
   directly. Regression tests:
   `appliance-agent/tests/test_config.py::LoadPrecedenceTests::test_stale_real_env_value_no_longer_wins_over_a_newer_activation`,
   `::test_administrator_cannot_repoint_an_activated_appliance_via_env_alone`,
   `::EnrollmentIntegrationTests::test_reenrollment_to_a_new_portal_survives_a_stale_real_env_value`
   (479/479 appliance-agent tests passed on Linux with this fix in place).
   **Not yet deployed to Ryzen** — Ryzen is still running old,
   runtime-patched code; see `docs/checkpoints/RYZEN.md`. This closes the
   source-level gap only; Ryzen's actual stale `vms.env` values still need
   a real upgrade once a deployable build exists.
4. **`degraded` appliance health treated identically to fully
   offline/unreachable** — a disk-space or CPU warning must not block Live
   View the way a genuinely unreachable appliance should.
   `GET /api/customer/cameras/{id}/status` now returns a distinct `degraded`
   state; the client still starts the session, surfacing a warning instead
   of refusing outright. Regression tests:
   `app/tests/test_customer_camera_status_health_states.py`.
5. **Malformed JavaScript template literals in generated customer pages**
   can silently kill an entire inline `<script>` block with no visible error
   to the customer. General protection (not just the one incident):
   `app/tests/test_generated_customer_pages_js_syntax.py` parses every
   generated `<script>` block with a real JS engine (esprima).
6. **Two competing recording-upload systems** — the old, pre-tenant-model
   `cloud_upload_worker()` in `main.py` and the new, properly IAM-scoped
   `recording_uploader.py` were both being started as separate background
   tasks. `recording_uploader.py` is authoritative. **CLOSED** (commit
   `c65c6e1`, confirmed live in the built RC1 image 2026-09-11):
   `cloud_upload_worker()` now permanently reports `worker_status=
   retired_superseded_by_recording_uploader` and never scans/uploads,
   regardless of `ANYAICAM_CLOUD_UPLOAD_ENABLED`. Regression tests:
   `app/tests/test_cloud_upload_worker_retired.py`.

---

## Staging (`anyaicam-staging`) — deployment rehearsal complete, GO (2026-09-11)

`anyaicam-staging`'s real, running source (last built 2026-09-10 01:59,
**never rebuilt since** — confirmed by comparing every rollback checkpoint
container's image ID, all four identical) predates the claim flow
entirely: `app/appliance_claims.py` doesn't exist on disk there, and
`POST /api/appliance/claim/begin` genuinely 404s (`{"detail":"Not
Found"}`, the FastAPI app's own 404, not an infra/proxy issue). The
`claim-key`/`live-relay`/`secret-fix` rollback checkpoints from the last
~16h were config/secret rotations only, never source rebuilds.

**Staging's real database is SQLite** (`/var/lib/anyaicam-staging/db/
staging.db`), not PostgreSQL — the `deploy/.env.staging.example` template
in this repo doesn't reflect the real deployed config (`ANYAICAM_DATABASE_BACKEND`/
`ANYAICAM_DATABASE_URL` are unset there; defaults to sqlite).
**Staging has no AWS credentials/region/S3 configured anywhere** —
confirmed both from the real env file and from the live app's own
`startup.complete` log line (`aws_region_configured:false,
s3_configured:false, cloud_foundation_ready:false`). Motion Cloud/S3/
CloudFront/STS integrations are not currently active on this
environment, contrary to an initial review's assumptions — there was
nothing live to "preserve" on that front.

**Backup**: `/var/lib/anyaicam-staging/db/staging-pre-claim-deploy-backup-20260911T222105Z.db`,
SHA-256 `7a997d95b72bff3a7838967dbcc29479243dd037047c231bb8fe4b35ada416ce`,
1,703,936 bytes, `PRAGMA integrity_check: ok`, 69 tables/97 indexes, real
data confirmed (3 appliances, 3 customers, 4 partner_users, 15 cameras).
Created via SQLite's own online backup API, source opened read-only.

**Disposable rehearsal** (entirely separate from live: `~/golden-rehearsal-4ade235/`
on the staging host, disposable copies of the backup, containers bound
to `127.0.0.1` only, never touching `/opt/anyaicam-staging` or
`/var/lib/anyaicam-staging/db/staging.db`):
- **Schema is already 100% caught up**: staging's `schema_migrations`
  has all 22 golden migrations applied already (diffed exactly against
  `app/db_migrations.py`'s full list — identical sets). `appliance_claims`'s
  live schema is a byte-for-byte match to the golden migration (0 missing/
  extra columns) — someone already ran `apply_migrations()` directly
  against the live DB recently (`20260910_appliance_claims` applied
  `2026-09-11T16:55:43`), without ever deploying the route code that uses
  it.
- Running the golden migration path (`partner_db.initialize_database()`)
  against the disposable copy produced a **byte-identical file** before
  and after (same SHA-256) — full confirmation of idempotency, zero
  migration risk remaining.
- Golden commit `4ade2352b1ea6da9c56a339650773c01879c0b98`'s image
  started cleanly against the migrated disposable DB with staging-equivalent,
  secrets-scrubbed config (`ANYAICAM_ENV=staging`, `ANYAICAM_RUNTIME_ROLE=cloud`,
  real non-secret URLs/flags copied from the live env file, throwaway
  `ANYAICAM_APP_SECRETS`). `/health` 200, `/ready` 503 with `self_test.ok:
  true`/`configuration_valid: true` (correct — `cloud` role needs
  `cloud_foundation_ready`, which is false only because AWS isn't
  configured, matching live reality, not a defect). **`POST /api/appliance/claim/begin`
  returned HTTP 200 with a real `claim_session_id`/`claim_code`** — the
  claim flow works end-to-end against the real migrated data.
  `live_relay_idle_sweep_task` started correctly (cloud/combined-role
  gated); no edge-only worker started under `RUNTIME_ROLE=cloud`.
- **Old-image rollback test**: the currently-live image (`8e31af166a1e`)
  starts and serves `/health` cleanly against the migrated schema, and
  correctly 404s on claim/begin (confirming it's genuinely the old,
  pre-claim code) — **image-only rollback after a golden deploy remains
  viable.**
- **Independent finding, not caused by this rehearsal**: the old image's
  `/ready` throws `TypeError: camera_status() missing 1 required
  positional argument: 'request'` — the exact bug the golden branch's
  `_legacy_camera_status()` already fixes. **Confirmed this is a live,
  standing defect on real `https://portal-staging.anyaicam.com/ready`
  right now** (read-only GET, checked directly) — not a rehearsal
  artifact, not something a rollback would newly introduce.

**Rsync scope for the eventual live sync** (exact mirror, not yet
executed): mirror only `/opt/anyaicam-staging/{app,requirements.txt,requirements-cpu.txt,Dockerfile}`
against the golden commit's tracked files. **Must never be touched**:
`/opt/anyaicam-staging/storefront/` (a completely separate service/image,
unrelated to the VMS portal) and `/opt/anyaicam-staging/deploy/` (the
real, live, tuned `docker-compose.yml`/`Caddyfile` — root-owned, actively
used; the repo's own `deploy/*.example.yml` are generic templates, not
what's actually running, and must not overwrite them). No runtime-generated
files were found under the live `app/` tree (no `__pycache__`, no stray
directories) — every subdirectory there matches golden's own tracked
structure exactly, so an exact-mirror sync scoped to those four paths is
safe.

**GO for live deployment**, pending final review of the exact live
procedure (build → rollback-checkpoint rename → `docker compose up -d
portal` → verify → confirm no data-count drift). Live deployment has
**not** been executed — this rehearsal only. Ryzen and Samsung untouched
throughout.

---

## Staging live cutover — ATTEMPTED, STOPPED before any container change (2026-09-11)

**A live cutover was approved and started, then deliberately stopped at
a real, unexpected finding — before the running container was ever
touched.** Live staging is unaffected: `anyaicam-staging-portal` is
still running the exact same old container it was before this attempt
began, confirmed via `/health` (200) and `claim/begin` (still 404,
proving it's genuinely the untouched old code), immediately before this
section was written.

**What actually happened, before this session's own review caught a
tooling defect and stopped the whole approach:**

1. A CRLF-corruption defect was found and fixed in the deployment
   artifact itself before any live write: a raw `git archive` (used for
   both the rehearsal and the first attempt at a live-deployment
   tarball) silently re-introduced CRLF line endings on this Windows
   dev machine's `core.autocrlf=true` config — the exact defect class
   `installer/build_release_installer.py`'s own `run_git()` already
   works around for the *installer* path, but this manual `git archive`
   invocation for the *staging* deployment path did not use that same
   `-c core.autocrlf=false` guard. Rebuilt with the guard; the corrected
   artifact (`ed5da71850eaa0b8471272a0bf89e8f9a548165b7c428dbbfdd7ff55b508a69c`)
   was verified byte-for-byte identical to `git show` for every checked
   file before use. The CRLF-corrupted artifact and its extraction were
   deleted.
2. Fresh pre-cutover DB backup taken and verified:
   `/var/lib/anyaicam-staging/db/staging-pre-cutover-backup-20260911T224358Z.db`,
   SHA-256 `7a997d95b72bff3a7838967dbcc29479243dd037047c231bb8fe4b35ada416ce`
   — identical to every earlier backup taken during this session's
   investigation, confirming zero live writes occurred throughout.
3. Host source tree backed up before any write:
   `/opt/anyaicam-staging-source-backup-pre-claim-flow-20260911T224358Z.tar.gz`,
   SHA-256 `6d52356d7c6b5ca763515550363e8710b5e530be1a5432abff6fd41e36dfc346`.
4. `app/`, `requirements.txt`, `requirements-cpu.txt`, `Dockerfile` were
   mirrored from the corrected golden artifact into `/opt/anyaicam-staging/`
   (`rsync -a --delete` for `app/`) — verified via `diff -rq` (exit 0,
   exact match). **This step is live and NOT reverted** (see below for
   why that's safe). `deploy/` and `storefront/` confirmed untouched
   (mtimes unchanged).
5. `docker compose build portal` succeeded, producing a new
   `deploy-portal:latest` image (manifest
   `sha256:0c4a3b7cebf8db5168fa851b98bc3bbe367f021bf19f17750f74838518048a57`).
   **This image was never deployed to any container.**
6. **Stopped here.** Attempting to pin the old running image
   (`sha256:8e31af166a1ef080509aea986f3668a177570b26002471d6d190d0f051603206`)
   as an explicit rollback tag failed: `docker tag` returned "No such
   image" — the image's top-level record was gone from `docker images
   -a` entirely (not even dangling), even though the container using it
   was still running fine. `docker commit` on the still-running
   container was tried as a fallback and *also* failed: "NotFound:
   content digest ...: not found." **Root cause: this host's Docker uses
   a containerd-snapshotter image store (`Storage Driver: overlayfs`,
   `driver-type: io.containerd.snapshotter.v1`), which appears to
   garbage-collect an image's content-store blobs once its tag is
   reassigned by a new build — even while a running container still
   holds a live lease on that image's already-materialized layers (which
   is why the container keeps running/serving correctly regardless).**

**Durable finding for any future deployment attempt on this exact
host**: the previously-assumed rollback mechanism ("tag the old image
before rebuilding, swap back if needed") **does not work** here once
`docker compose build` has run against the same tag. Neither `docker
tag` nor `docker commit` can rescue it afterward — the image must be
pinned (tagged or committed) **before** the build that would replace its
tag runs, not after. A future attempt should either pin the running
image *before* any build step, or use a genuinely separate
container/tag (blue-green: run the new code under a different
name/port, verify, then swap Caddy's upstream) so the running old
container is never dependent on the image store's tag bookkeeping for
its own rollback safety.

**Why leaving `/opt/anyaicam-staging/app/` on the golden source (step 4
above) is safe to leave as-is despite stopping**: the running container
was built from the OLD source at an earlier point in time and has that
old code baked into its own image layers — Docker containers are
self-contained once built; changing files on the host filesystem after
a container is built and running does not affect it. `docker ps`/`/health`/
`claim/begin` (404) all confirm the live service is still running the
old code, completely independent of what's currently sitting in the
host's `app/` directory. Reverting that directory back to the old
source would itself be an additional write action, not a "stop" — left
as verified golden source, ready for the next attempt to simply
continue from Step 5 (rollback-image-pinning) onward, once a
rollback-safe mechanism is agreed for this host.

**State to resume from, next session**: pre-cutover DB and host-source
backups exist and are verified (see above). Host `app/`/`requirements*.txt`/
`Dockerfile` already reflect the golden commit `4ade2352b1ea6da9c56a339650773c01879c0b98`.
A built (but undeployed) golden image exists as `deploy-portal:latest`.
The live container is still running the old, pre-claim-flow code,
completely unaffected. Do not repeat the git-archive-without-
`core.autocrlf=false` mistake — the corrected artifact/procedure is
recorded above.

---

## Appliance checkpoints

- `docs/checkpoints/RYZEN.md` — the real 5-camera physical appliance, primary
  validation hardware.
- `docs/checkpoints/SAMSUNG.md` — waiting at the cloud identity/activation
  stage. **Do not restart Samsung setup from the beginning** — read that file
  first.
