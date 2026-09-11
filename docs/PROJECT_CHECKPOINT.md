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
validation** — see "RC1 → RC2 on Ryzen" below. **RC2 fixes that defect and
has been built + locally smoke-tested but NOT yet deployed to Ryzen** (or
anywhere else — not staging, not Samsung). Do not treat RC2 as deployed
until a specific deploy record says otherwise, added below.

```
GOLDEN BUILD: golden-foundation-rc2 — commit 34d9d5c
  (full sha 34d9d5ca96b674f0d4a7212bbf7996d52f063fa0)
Built: 2026-09-11, from `reconcile/golden-foundation-20260911` worktree
  `AnyAiCam-VMS-reconciliation`, via the repo's own `Dockerfile` (the same
  one deploy/docker-compose.*.example.yml reference — NOT Dockerfile.production,
  which nothing in this repo actually deploys with).
VMS container image digest (local build, not yet pushed to a registry):
  sha256:6ff38cc2c81f6398ce6b95abf42ce5056a8f9eec5a67258dd1f2cb06e9f930af
  tags: anyaicam-vms:golden-rc2, anyaicam-vms:34d9d5c
Installer artifact (the actual thing to deploy — built via
  installer/build_release_installer.py --vms-commit 34d9d5ca96b674f0d4a7212bbf7996d52f063fa0):
  anyaicam-appliance-installer-1.1.0-vms-34d9d5ca96b6.tar.gz
  artifact sha256: 8912bcd17041cad1ad0cbf813a5dcb148b23deb2dd1e0c7b1da17893a3283566
  embedded source sha256: b04b976979f41baa6f27e8e1d4ed670745981f45c7f3b3a626110a29656bc51d
Local smoke test (Windows dev machine, disposable container, NOT Ryzen):
  ANYAICAM_RUNTIME_ROLE=edge + ANYAICAM_ENV=production + zero cameras ->
  self_test.ok=true, configuration_valid 0 critical (was 6). ready=false
  still, correctly, because recording_workers=0 (edge role legitimately
  requires recording>0) -- expected until cameras are discovered.

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

**Not yet re-deployed to Ryzen.** RC2 (`34d9d5c`) is built and locally
verified only. Camera discovery/claim on Ryzen was never reached (blocked
by the RC1 failure) — still pending.

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

## Appliance checkpoints

- `docs/checkpoints/RYZEN.md` — the real 5-camera physical appliance, primary
  validation hardware.
- `docs/checkpoints/SAMSUNG.md` — waiting at the cloud identity/activation
  stage. **Do not restart Samsung setup from the beginning** — read that file
  first.
