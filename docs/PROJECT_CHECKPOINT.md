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
below. **RC3 fixed that and passed `validate.sh` with 0 failures, but
that validator itself had a blind spot: it never checked whether
`anyaicam-agent.service` was actually *active*, only *enabled* — which
let RC3's own agent silently crash-loop 600+ times, undetected, the
entire time it was "PASSED."** See "RC3 on Ryzen — PASSED" and "RC4 on
Ryzen — the agent-service crash loop" below for the full history. **RC4
fixes both the validator blind spot and the underlying agent lifecycle
bug it was hiding, and has been deployed to Ryzen (repair-path install)
and passed the now-more-complete `validate.sh` with 0 failures** — see
"RC4 on Ryzen — PASSED" below. RC4 is the first golden build to pass
validation while the agent is both enabled *and* stably active.
**Claim/activation and camera discovery have NOT been performed yet** —
do not treat those as done.

```
GOLDEN BUILD: golden-foundation-rc4 — commit 087e455
  (full sha 087e45587856e7525210d0f9498c3add644d7658)
Built: 2026-09-11, from `reconcile/golden-foundation-20260911` worktree
  `AnyAiCam-VMS-reconciliation`, using the corrected
  `git -c core.autocrlf=false` protection (already built into
  installer/build_release_installer.py's own run_git() helper).
VMS container image: source-identical to RC2/RC3 -- `git diff --stat
  4ade235 087e455 -- app/ Dockerfile requirements.txt requirements-cpu.txt`
  is empty; this lineage only changed `appliance-agent/` and `installer/`.
  Rebuilt anyway for a fresh digest (Docker's manifest hash includes
  build metadata/timestamps even for a 100%-cache-hit `COPY ./app /app`
  layer, so the digest differs from RC2/RC3's despite identical content --
  confirmed via direct file-hash comparison, not assumed):
  digest sha256:476ff7dd964b3ab6f1591697448a869edab365f5959fb6a2732eb9a2034c7cf9
  tags: anyaicam-vms:golden-rc4, anyaicam-vms:087e455
appliance-agent package (anyaicam-appliance-agent 0.1.0) -- rebuilt
  because service.py changed (25e2fc1):
  wheel sha256: da7a5aabe60ffeb958cbd05652af066b6678f59cbf8432e978254c77d9a31dbe
  sdist sha256: 89f83260708ac8d0e2a3c85fb92cd6196fa4ff3ef4fb33a46295404342b9a32e
Installer artifact (the actual thing deployed — built via
  installer/build_release_installer.py --vms-commit 087e45587856e7525210d0f9498c3add644d7658):
  anyaicam-appliance-installer-1.1.0-vms-087e45587856.tar.gz
  artifact sha256: 706e29e7d8d777ae8a27ea4c8a3b04543401d2e43761d7704b5c4fe76024ba82
  embedded source sha256: 31e6a7bff70d49afdda64cc17145334ed5ffbbd549fa0133d4cc51d62775010c
Content-fidelity verified directly (not just trusted): extracted
  app/appliance_claims.py, Dockerfile, appliance-agent/scripts/uninstall.sh,
  installer/uninstall.sh, installer/validate.sh, and
  appliance-agent/anyaicam_agent/service.py all hash-matched `git show`
  of the exact commit, byte for byte.
Tested: appliance-agent/tests -- 483 passed (478 baseline + 5 new for
  the activation-wait fix), same 3 pre-existing Windows-platform-only
  failures. installer/tests/run_tests.sh -- 73 passed (69 + 4 new for
  the is-active validator fix), same 11 pre-existing unrelated failures.
  installer/tests/test_build_release_installer.py -- 12 passed, 1
  pre-existing skip. Local RC4 VMS image smoke test: /health and
  /version clean.
Deployed to: Ryzen (2026-09-11), repair-path install over the existing
  RC3 install (detect_install_state() correctly reported 5/5 markers ->
  existing). validate.sh PASSED: 0 failures, including the new
  anyaicam-agent.service is-active check -- see "RC4 on Ryzen — PASSED"
  below for the full physical-hardware result.

Prior build (RC3, source defects fixed, but validate.sh had an
undetected blind spot that masked a real agent crash loop — see
"RC3 on Ryzen — PASSED" and "RC4 on Ryzen" below):
GOLDEN BUILD: golden-foundation-rc3 — commit 4ade235
  (full sha 4ade2352b1ea6da9c56a339650773c01879c0b98)
VMS container image digest: sha256:6ff38cc2c81f6398ce6b95abf42ce5056a8f9eec5a67258dd1f2cb06e9f930af
  (tags anyaicam-vms:golden-rc2/golden-rc3/34d9d5c/4ade235 all equivalent
  in content, identical to RC2's)
Installer artifact: anyaicam-appliance-installer-1.1.0-vms-4ade2352b1ea.tar.gz
  artifact sha256: f8797d627a580363f2f1807c9a7d9b91f29c73df84a4348b30fee49afc56e5c7
Deployed to: Ryzen (2026-09-11), repair-path install over the existing
  RC2 install. validate.sh PASSED 0 failures at the time -- **later found
  to have been an incomplete check**: the agent was silently
  crash-looping 600+ times the entire time, undetected. Superseded by
  RC4 above; no longer the current live software on Ryzen.

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

**Ryzen's current real state at the time:** RC3 installed and running,
`validate.sh` clean. **Still unclaimed, zero cameras** — no claim/
activation and no camera discovery have been performed. No runtime
patches applied at any point in the RC1→RC2→RC3 sequence. **This state
did not stay accurate** — see "RC4 on Ryzen — the agent-service crash
loop" immediately below.

---

## RC4 on Ryzen — the agent-service crash loop, root-caused, fixed, and PASSED (2026-09-11)

Before attempting Ryzen's claim, a read-only check that had never been
run before (`systemctl show anyaicam-agent.service -p NRestarts`)
revealed the agent unit crash-looping — **600+ restarts**, continuous
since the RC3 install completed. RC3's own `validate.sh` had reported 0
failures the entire time; it simply never checked whether the agent was
*active*, only *enabled* (unlike the VMS unit, which already checked
both). Two independent, real root causes, found in this order:

**1. Stale systemd drop-in.** `/etc/systemd/system/anyaicam-agent.service.d/vms-paths.conf`,
dated **2026-08-19** — nearly a month before this reconciliation branch
existed — from an unrelated local-dev session, bind-mounting
`/home/alejandro-mata/projects/AnyAICam/{app/static/hls,recordings}`
onto `/var/lib/anyaicam/vms/{hls,recordings}`. One bind source no longer
existed. Confirmed by reading every uninstall code path directly:
neither `installer/uninstall.sh` nor `appliance-agent/scripts/uninstall.sh`
ever removed drop-in *directories*, only base unit *files* — so this
survived the full `--purge-all` + RC1/RC2/RC3 reinstall cycle untouched
and silently reattached to each fresh unit. Confirmed isolated (no other
`.service.d` for any other `anyaicam-*` unit, no other file anywhere
under `/etc/systemd/system/` referencing these paths). **Fixed in
`88c87a1`** (both uninstall scripts, both VMS and agent units, for the
general defect class) and removed from Ryzen directly (one-time manual
cleanup of pre-existing cruft that predates any golden source, not a
patch to golden software): `sudo rm -rf
/etc/systemd/system/anyaicam-agent.service.d && sudo systemctl
daemon-reload`.

**2. After removing the drop-in, the agent kept crash-looping** — a
second, different, genuinely pre-existing lifecycle defect in
`service.py` itself, unrelated to the drop-in: `run()` raised
`RuntimeError('Appliance is not activated...')` unconditionally whenever
no credential existed yet, and the unit's `Restart=always`/
`RestartSec=10` turned that into a permanent loop. `install.sh` enables
and starts this unit unconditionally, *before* claim ever happens —
"installed but not yet claimed" is the FIRST real state of every fresh
appliance, and exactly the state `validate.sh` runs in. **This defect
predates the entire reconciliation effort** — always present, masked
first by the drop-in's own unrelated crash, never surfaced because
nothing checked `is-active` until this same session. **Fixed in
`25e2fc1`**: `run()` now calls `_await_activation()`, which returns
immediately if already credentialed (zero change to the normal case) or
polls `credential.json` directly every 10s, logging once, until
activation completes — no crash, no restart, no change to
`Restart=always` (simply never triggered by this condition again).
**Also confirmed `55fa281`'s own `validate.sh` is-active fix (made
*before* this second defect was found) needed no adjustment** — it was
correct in intent from the start; it only became achievable once
`service.py` stopped treating "not yet claimed" as fatal.

**RC4 (`087e455`) bundles all three fixes and was verified end-to-end on
real Ryzen hardware:**
- Pre-install: appliance identity confirmed `637ad320-daaa-436e-89c9-70a84f4f54a9`,
  `credential.json`/`agent.json`/`cameras.json` all confirmed absent
  (genuinely unclaimed, zero cameras), VMS `/health`/`/version`/`/ready`
  captured as baseline.
- Repair-path install (`detect_install_state()` → `existing`, 5/5
  markers, identity/state preserved — no wipe, no new identity
  generated).
- **Journal shows the exact transition, byte-for-byte matching the
  source fix**: the last old-code crash at `19:53:28`
  (`status=1/FAILURE`), the RC4 process starting at `19:53:38` and
  immediately logging `"Appliance is not activated yet; waiting for
  anyaicam-setup (interactive or --claim) to complete..."` — the literal
  string from `25e2fc1` — then **zero restarts since**, confirmed stable
  across multiple activation-poll intervals (`NRestarts` unchanged,
  `ActiveState=active`/`SubState=running`/`ExecMainStatus=0` throughout).
- `sudo bash validate.sh` → **PASSED, 0 failures**, including the new
  `anyaicam-agent.service is active` check — the first golden build to
  pass validation while the agent is both enabled *and* stably active,
  on real hardware, genuinely unclaimed.
- `/health` 200, `/version` reports exact commit `087e45587856...`,
  `cloud_id: null` (still unclaimed) — matches baseline. `/ready` 503
  correctly (`self_test.ok: true`, 0 critical — only the same 3
  pre-existing harmless warnings as before). No AWS/cloud/Motion Cloud
  flags enabled (`aws_region_configured` etc. all still `false`,
  unchanged from baseline). Stale drop-in directory confirmed absent;
  `systemctl cat` shows only the clean base unit, no drop-in merged.
- **No runtime patches at any point** — every fix went through
  BUG FOUND → FIX SOURCE → REGRESSION TEST → COMMIT → BUILD → DEPLOY →
  VERIFY; the only direct action taken on Ryzen itself was the one-time
  removal of the pre-existing (non-golden) stale drop-in, explicitly
  authorized as cleanup of cruft that predates this project's own
  source, not a patch to it.

**Ryzen's current real state:** RC4 installed and running, `validate.sh`
clean (agent enabled+active, not just enabled). **Still unclaimed, zero
cameras** — no claim/activation and no camera discovery have been
performed; both remain explicitly deferred pending separate
authorization. See `docs/checkpoints/RYZEN.md` for further detail.

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

## Staging blue-green design + rehearsal — GO (2026-09-11)

Designed and rehearsed a blue-green traffic-switch mechanism that avoids
depending on the image store staying stable (the exact thing that
stopped the previous attempt). **Entirely read-only investigation +
disposable-DB rehearsal — live BLUE/Caddy/storefront/DB never touched.**

### Architecture

- **BLUE** = the currently-running `anyaicam-staging-portal` container,
  DNS-resolvable on the `deploy_default` bridge network as `portal`
  (its network alias — matches Caddy's current `dial: "portal:8000"`).
  Never removed, never rebuilt in this design.
- **GREEN** = a new, separate container, `portal-green`, attached to the
  *same* `deploy_default` network (so Caddy can reach it once switched)
  but with **no host port published on that network** — only a
  loopback-only host port (`127.0.0.1:18300`) for private testing.
  Never externally reachable through Caddy until explicitly switched.
- **Traffic switch mechanism — Caddy's Admin API, not the Caddyfile.**
  Confirmed live on `127.0.0.1:2019` inside the caddy container (my
  first probe via `wget localhost:2019` failed only because busybox
  wget tried `::1` first; `127.0.0.1` explicitly works). The exact
  reverse-proxy upstream lives at this JSON path in the running config
  (derived by parsing a live `GET /config/`, not guessed):
  `apps.http.servers.srv0.routes[0].handle[0].routes[0].handle[3].upstreams[0].dial`,
  currently `"portal:8000"`. **A `PATCH` to that one path changes the
  live upstream in memory only — the persistent `Caddyfile` on disk is
  never touched, and no `caddy reload`/restart is needed.** This also
  means an in-memory switch is inherently self-healing: if the admin
  API path is ever abandoned or caddy is restarted/reloaded for any
  other reason, it reverts to the Caddyfile's own `{$APP_UPSTREAM:vms:8000}`
  default — i.e., whatever the compose file's `APP_UPSTREAM` env var
  says (currently `portal:8000`, matching BLUE) — never silently stuck
  pointing at a GREEN that no longer exists.

### SQLite concurrency — the constraint that shapes the real cutover sequence

Confirmed two hard facts, not assumptions:
- The live DB (`ANYAICAM_PARTNER_DB=/app/data/staging.db`) uses
  `journal_mode: delete` (SQLite's default rollback journal), **not
  WAL** — only one writer is ever safe at a time; a second process
  holding the same file open risks lock contention/corruption.
- `live_relay_idle_sweep_worker()` (`app/live_relay_idle_sweep.py`),
  which starts unconditionally for `RUNTIME_ROLE in {cloud, combined}`,
  performs real DB writes every 10 seconds (`IDLE_SWEEP_INTERVAL_SECONDS`)
  regardless of whether the container is receiving any HTTP traffic.

**Conclusion, stated plainly rather than glossed over**: BLUE and GREEN
must never both hold the live `staging.db` file open for writes at the
same time — not even briefly, not even with zero incoming HTTP traffic,
because BLUE's own background worker writes independently of traffic.
A literal zero-downtime "both fully running against the same live file"
design is not safe with this app's current default journal mode. The
responsible design instead achieves **near-zero downtime** (a few
seconds) while fully eliminating the concurrent-writer risk: **BLUE is
briefly `docker stop`ped (never removed/rebuilt/retagged) immediately
before GREEN is started against the real live DB file**, then Caddy is
switched to GREEN. "BLUE remains running and immediately recoverable"
is satisfied as "BLUE's container/image are never destroyed or
replaced, and `docker start` brings it back instantly" — which is also
exactly what avoids the previous attempt's image-store failure mode,
since no image is ever re-tagged or rebuilt in this design at all.

### Rehearsal performed (disposable copy only, per requirement 5)

- Disposable DB copy: `~/blue-green-rehearsal/green-disposable.db`,
  SHA-256 `7a997d95b72bff3a7838967dbcc29479243dd037047c231bb8fe4b35ada416ce`
  (copied from the already-verified pre-cutover backup).
- GREEN started from the already-built `deploy-portal:latest`
  (`4ade2352b1ea6da9c56a339650773c01879c0b98`), attached to
  `deploy_default`, env copied from live's non-secret config with a
  throwaway `ANYAICAM_APP_SECRETS` and `ANYAICAM_PARTNER_DB` pointed at
  the disposable file only.
- **Verified privately** (via `127.0.0.1:18300`, never through Caddy):
  `/health` 200; `/version` clean; `/ready` 503 with `self_test.ok:
  true`/`configuration_valid: true` (expected — no AWS configured,
  matching live reality); claim route genuinely registered (422 on an
  empty body, not 404); `claim/begin` with a real UUIDv4 returned a real
  `claim_session_id`/`claim_code`.
- **DB integrity/visibility**: `PRAGMA integrity_check: ok`; reference
  row counts (`appliances:3, customers:3, partner_users:4, cameras:15`)
  unchanged from baseline; only the one intentional `appliance_claims`
  test row present.
- **No unintended workers**: only `live_relay_idle_sweep_task` started
  (correctly cloud/combined-role-gated); no edge-only worker.
- **BLUE confirmed unaffected throughout**: `/health` 200, `claim/begin`
  still 404 (proving BLUE never changed), uptime continuous.
- **Caddy confirmed untouched throughout**: the admin API path above
  still reads `"portal:8000"` after the full rehearsal — no PATCH was
  ever sent to it in this rehearsal, per "do not execute the live
  traffic switch."
- Rehearsal container (`portal-green`) stopped and removed afterward;
  the disposable DB copy and env file remain at
  `~/blue-green-rehearsal/` for reference.

### Exact live cutover procedure (NOT executed — for review)

```bash
# 1. Fresh pre-cutover DB backup + row-count baseline (same method as before)
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo python3 -c "
import sqlite3
srcconn = sqlite3.connect('file:/var/lib/anyaicam-staging/db/staging.db?mode=ro', uri=True)
dstconn = sqlite3.connect(f'/var/lib/anyaicam-staging/db/staging-pre-bluegreen-backup-${TS}.db')
srcconn.backup(dstconn); dstconn.close(); srcconn.close()
"
sudo sha256sum /var/lib/anyaicam-staging/db/staging-pre-bluegreen-backup-${TS}.db

# 2. Build GREEN's real env (live non-secret values + throwaway secret --
#    same scrubbing approach as the rehearsal, but pointed at the REAL DB)
sudo bash ~/build_rehearsal_env.sh   # or hand-adapt; sets ANYAICAM_PARTNER_DB
sed -i 's|ANYAICAM_PARTNER_DB=.*|ANYAICAM_PARTNER_DB=/app/data/staging.db|' ~/blue-green-rehearsal/green.env
sed -i '/^ANYAICAM_APP_SECRETS=/d' ~/blue-green-rehearsal/green.env
sudo grep '^ANYAICAM_APP_SECRETS=' /etc/anyaicam-staging/vms-staging.env >> ~/blue-green-rehearsal/green.env  # use the REAL secret for the real cutover, not the rehearsal throwaway

# 3. Stop BLUE (preserve, never remove) -- brief downtime window starts
docker stop anyaicam-staging-portal

# 4. Start GREEN against the REAL live DB file
docker run -d --name portal-green --network deploy_default \
  --env-file ~/blue-green-rehearsal/green.env \
  -v /var/lib/anyaicam-staging/db:/app/data \
  -v /var/lib/anyaicam-staging/recordings:/app/recordings \
  -v /var/lib/anyaicam-staging/hls:/app/static/hls \
  -v /var/lib/anyaicam-staging/data-config:/opt/anyaicam/data/config \
  deploy-portal:latest

# 5. Verify GREEN privately before switching traffic (no host port needed --
#    exec into caddy or use `docker exec portal-green` for a loopback check)
sleep 8
docker exec anyaicam-staging-caddy wget -qO- --header='Host: portal-staging.anyaicam.com' http://portal-green:8000/health
docker exec anyaicam-staging-caddy wget -qO- --header='Host: portal-staging.anyaicam.com' http://portal-green:8000/version

# 6. Switch traffic -- Caddy admin API PATCH, Caddyfile untouched
docker exec anyaicam-staging-caddy wget -q --method=PATCH \
  --header='Content-Type: application/json' \
  --body-data='"portal-green:8000"' \
  -O- http://127.0.0.1:2019/config/apps/http/servers/srv0/routes/0/handle/0/routes/0/handle/3/upstreams/0/dial
# downtime window ends here

# 7. Post-switch verification through the real public path
curl -s -H "Host: portal-staging.anyaicam.com" -w "\nHTTP_STATUS:%{http_code}\n" https://portal-staging.anyaicam.com/health
curl -s -H "Host: portal-staging.anyaicam.com" https://portal-staging.anyaicam.com/version; echo
DEVICE_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
curl -s -H "Host: portal-staging.anyaicam.com" -H "Content-Type: application/json" -X POST \
  -d "{\"device_id\":\"$DEVICE_ID\"}" -w "\nHTTP_STATUS:%{http_code}\n" \
  https://portal-staging.anyaicam.com/api/appliance/claim/begin

# 8. DB integrity/row-count re-check against the now-live-again real file
sudo python3 ~/inspect_staging_schema.py /var/lib/anyaicam-staging/db/staging.db
```

### Rollback (immediate, from GREEN back to still-preserved BLUE)

```bash
# Traffic first
docker exec anyaicam-staging-caddy wget -q --method=PATCH \
  --header='Content-Type: application/json' \
  --body-data='"portal:8000"' \
  -O- http://127.0.0.1:2019/config/apps/http/servers/srv0/routes/0/handle/0/routes/0/handle/3/upstreams/0/dial
# Then stop GREEN (it's the one holding the live DB file now) and restart BLUE
docker stop portal-green && docker rm portal-green
docker start anyaicam-staging-portal
curl -s -H "Host: portal-staging.anyaicam.com" -w "\nHTTP_STATUS:%{http_code}\n" https://portal-staging.anyaicam.com/health
```
Rollback triggers: any of the same conditions from the earlier
cutover-attempt plan (health ≠ 200, claim/begin 404 or 5xx, row-count
drift beyond the one intentional test claim, integrity_check ≠ ok, new
container unhealthy within ~2 minutes).

### GO / NO-GO

**GO**, with one disclosed, deliberate deviation from a literal
zero-downtime ask: a few seconds of BLUE downtime while GREEN takes over
the live DB file, required by SQLite's non-WAL journal mode and
`live_relay_idle_sweep_worker`'s unconditional periodic writes. This is
the responsible tradeoff — the alternative (both running against the
same file simultaneously) is a real corruption risk, not a rehearsal
formality. Not yet executed; awaiting explicit GO for the live traffic
switch.

---

## Staging live cutover — EXECUTED AND VERIFIED SUCCESSFUL (2026-09-11)

**`anyaicam-staging` is now serving the golden claim-flow source in
production.** Cutover timestamp: downtime window
`2026-09-11T23:04:33Z`–`2026-09-11T23:07:07Z` (~2m34s — longer than the
rehearsed "few seconds" estimate; see "unexpected condition" below for
the exact cause, which was found and resolved live, not worked around).

### Result summary

| Item | Value |
|---|---|
| Deployed commit | `4ade2352b1ea6da9c56a339650773c01879c0b98` |
| BLUE (preserved) | `anyaicam-staging-portal`, `Exited (0)`, image `8e31af166a1ef080509aea986f3668a177570b26002471d6d190d0f051603206` intact — **never removed, rebuilt, or retagged** |
| GREEN (live) | `portal-green`, running, image `deploy-portal:latest` (`0c4a3b7cebf8db5168fa851b98bc3bbe367f021bf19f17750f74838518048a57`) |
| Caddy upstream before | `"portal:8000"` (BLUE) |
| Caddy upstream after | `"portal-green:8000"` (GREEN) — via Admin API `PATCH`, **persistent Caddyfile untouched**, no reload/restart |
| `/health` (external, post-switch) | 200, `hostname` matches GREEN's container ID |
| `/version` (external) | clean; `cloud_id: "AIC-C90CF0C9"` (real, persisted identity data — expected, matches BLUE) |
| `/ready` (external) | 503 — correctly: `self_test.ok: true`, `configuration_valid: true` (0 critical); `cloud_foundation_ready: false` only because no AWS is configured, unrelated to this deploy |
| `claim/begin` (external, real UUIDv4) | 200, real `claim_session_id`/`claim_code` returned |
| DB integrity | `PRAGMA integrity_check: ok`, both immediately pre- and post-cutover |
| Row-count deltas | `appliances/customers/partner_users/cameras`: **zero drift** (3/3/4/15, unchanged). `appliance_claims`: **+1**, exactly the one successful `claim/begin` test call (two earlier empty-body validation probes returned 422 and created no rows) |
| Post-cutover observation (~20s) | GREEN: 0 restarts, no error/exception/lock/traceback in logs, still healthy |
| Rollback readiness | BLUE fully intact and stoppable→startable; rollback = PATCH Caddy back to `"portal:8000"`, stop GREEN, `docker start anyaicam-staging-portal` |
| Ryzen | **Untouched** — no SSH session opened to Ryzen at any point in this cutover |
| Samsung | **Untouched** — no SSH session opened to Samsung at any point in this cutover |
| Motion Cloud / AWS flags | **Not enabled** — `aws_region_configured/database_configured/s3_configured/secrets_manager_configured` all still `false`, exactly as before |

### Unexpected condition encountered and resolved live (not worked around)

The rehearsed rollback/switch commands used `wget --method=PATCH`, but
the **caddy:2-alpine image's busybox wget does not support arbitrary
HTTP methods** (`unrecognized option: method=PATCH`) — this was never
caught during rehearsal because "do not execute the live traffic
switch" correctly meant the PATCH itself was never actually sent until
now. The failed wget attempt sent nothing and changed nothing (Caddy's
upstream was confirmed still `"portal:8000"` immediately after) — but
since BLUE had already been stopped for the DB handoff at that point,
**this is the reason for the ~2m34s downtime window** rather than a
few seconds: the site was genuinely down (Caddy pointed at a stopped
BLUE) while this was diagnosed. Resolution: `curl` (confirmed present
in the same container image, unlike a full HTTP-methods-capable wget)
sent the identical PATCH successfully on the very next attempt. No
runtime patch, no improvisation beyond substituting a working HTTP
client for the same already-approved, already-rehearsed request —
consistent with "do not improvise runtime patches" (which this reads as
covering the *application*, not incidental tooling substitution for an
already-approved control-plane call). **For any future Caddy Admin API
call on this host, use `curl` inside the caddy container, not `wget`.**

### State to resume from

GREEN is now the production container for `anyaicam-staging`. BLUE
remains fully intact as an instant rollback target. The Ryzen claim was
explicitly deferred pending review of this cutover — **not yet
authorized, do not proceed without a separate explicit go-ahead.**
Samsung remains untouched and mid-setup per its own checkpoint.

---

## 2026-09-12: `green.env` config-drift defect — recreating GREEN silently re-pointed the live portal at a disposable rehearsal database

**Not a source defect. An operational/runbook defect in this checkpoint's own recorded blue-green procedure.**

Two follow-up staging changes were made after the live cutover above, each requiring GREEN to be recreated (stop/rm/`docker run`) so the running process would pick up a new environment variable:

1. Added `ANYAICAM_CLAIM_FLOW_SECRET_KEY` (a real customer claim confirmation was failing 503 with "Claim confirmation is temporarily unavailable" — `encrypt_claim_flow_secret()` fails closed, by design, when this key is unset; it had simply never been provisioned for staging). Generated via the container's own `cryptography.fernet.Fernet.generate_key()`, appended by name only (value never displayed) to both `/etc/anyaicam-staging/vms-staging.env` and `~/blue-green-rehearsal/green.env`.
2. Recreating GREEN from `green.env` — following this doc's own documented recreation command (`docker run ... --env-file ~/blue-green-rehearsal/green.env ...`) — **broke customer/partner login and session validation**: real login started failing with a generic "email or password is incorrect", and an in-flight browser session got a spurious 401 immediately after the recreation, cascading into a "CSRF validation failed" on the next sign-in attempt.

Root cause, confirmed by direct read-only inspection: `green.env`'s `ANYAICAM_PARTNER_DB` line had reverted to `/rehearsal/green-disposable.db` (the rehearsal-phase throwaway database baked into the `deploy-portal:latest` image, containing exactly one unrelated `partner_users` row) instead of `/app/data/staging.db` (the real, live database). **This doc's own recorded cutover procedure correctly sed-fixes `ANYAICAM_PARTNER_DB` in `green.env` in place** (see the "RC... Staging live cutover" commands above) — but a second file, `~/blue-green-rehearsal/green-live.env`, was found on the host holding the same correct fixed-up content under a different name, undocumented anywhere in this checkpoint. The most consistent explanation: `build_rehearsal_env.sh` (step 1 of the documented procedure) unconditionally regenerates `green.env` from rehearsal defaults every time it's run, and at some point after the real cutover it (or an equivalent manual re-run) was invoked again — for an unrelated later rehearsal/validation pass — without the follow-up `sed`/`ANYAICAM_APP_SECRETS` fix-up steps being reapplied afterward, silently reverting the *file on disk* back to rehearsal values while the *already-running* container (started before that regeneration) kept working fine on its original, correct environment — masking the drift until the next recreation.

**The defect class**: this checkpoint's runbook treats `green.env` as a single file safely reusable across both rehearsal and live purposes, sed-patched in place. It is not safe to reuse: regenerating it for any reason (even an unrelated rehearsal) invisibly invalidates the live container's next recreation, with no diff/check step in the documented procedure to catch it before `docker run` picks up the stale values. Confirmed no source code involved — `app/appliance_protocol.py`, `app/partner_db.py`, `app/partner_portal.py` all behaved exactly as designed against whatever database they were pointed at.

**Fixed for now**: `green.env`'s `ANYAICAM_PARTNER_DB` corrected back to `/app/data/staging.db` and GREEN recreated a second time. Verified after recreation: `ANYAICAM_PARTNER_DB=/app/data/staging.db` and `ANYAICAM_CLAIM_FLOW_SECRET_KEY` (by name only) both present in the container's environment; image digest unchanged (`sha256:0c4a3b7ce...048a57`, still the golden cutover image, no rebuild); `/health` 200 internally and externally; `/version` unchanged (`cloud_id: AIC-C90CF0C9`, `aws_region: null`, no AWS/Motion Cloud flags); the real `partner_users` row for the customer account (`account_status: active`, `approved: 1`, unchanged since its `2026-09-11T06:34:39` creation) and all 4 real `partner_users` rows / 30 `user_sessions` rows visible again through the container; `appliance_claims` rows for Ryzen's device (`637ad320-...`) unchanged — no claim ever reached `claimed`/`completed`, zero partial writes across the whole incident.

**Not yet fixed — recommended safeguard for whoever next touches this**: stop treating `green.env` as the one file both the rehearsal generator and the live-fix procedure write to. Either (a) update this doc's recreation commands to use `green-live.env` explicitly and never regenerate it via `build_rehearsal_env.sh` again, or (b) add a mandatory pre-flight check before any future `docker run --env-file ... portal-green` — grep the file about to be used for `ANYAICAM_PARTNER_DB=/app/data/staging.db` (and that `ANYAICAM_CLAIM_FLOW_SECRET_KEY` is present) and refuse to proceed if either check fails. `green-live.env` itself is now *also* stale in the other direction — it has the correct DB path but predates the `ANYAICAM_CLAIM_FLOW_SECRET_KEY` addition, so it is not safe to use as-is either without first appending that key. Neither file should be trusted blindly; whichever is used next must be diffed against both the real running container's current environment and `/etc/anyaicam-staging/vms-staging.env` before use.

---

## 2026-09-12: Ryzen claim completed server-side, stranded locally — three real defects, source-fixed, NOT yet deployed

A real, independent Ryzen claim (device_id `637ad320-daaa-436e-89c9-70a84f4f54a9`, code `3254B9D3`) reached customer confirmation successfully and durably completed server-side, but local enrollment on Ryzen failed and left the appliance unactivated. Full read-only trace (staging DB + Ryzen journal, no sudo needed) found **three independent real defects**, all now fixed in source and unit/end-to-end tested. **Nothing has been deployed. Nothing has been cleaned up in staging's database. Ryzen has not been touched.** This section is the authoritative record of exactly what's fixed, what's still stranded, and what the next three phases (deploy → cleanup → verify → one new claim) need to do.

### Root cause 1 — cloud claim_complete() called a single-tenant mechanism it should never have touched

`appliance_claims.py`'s `claim_complete()` handler committed the claim's `appliance_claims`/`appliances`/`appliance_credentials` rows successfully, then unconditionally called `appliance_activation.persist_activation()` — a mechanism built for exactly one appliance to remember its own identity in a local file (`/app/recordings/appliance_identity.json`, `RUNTIME_ROLE` edge/combined's own concept of "this box's identity"). On the shared, multi-tenant `anyaicam-staging` cloud process, that file already held `cloud_id: AIC-C90CF0C9` from an unrelated activation test the day before (2026-09-11T07:23:02) — completely unrelated to Ryzen, but `persist_activation()` saw a different cloud_id already recorded and raised `ActivationConflict`, which `claim_complete()` turned into a 409 the caller received *after* its own database transaction had already durably committed. **Fixed** in `app/appliance_activation.py`/`app/appliance_claims.py`/`app/appliance_cloud.py`, commit `bd633a2`: a new `local_activation_tracking_applies()` gate (reads `ANYAICAM_RUNTIME_ROLE`, true only for edge/combined) now skips `persist_activation()` entirely for a cloud deployment, at both call sites (`claim_complete()` and `activate_appliance()`). Edge/combined behavior is completely unchanged and still fail-closed — proven by a new regression test.

### Why re-imaging or wiping Ryzen would not have fixed this

This defect lives entirely in **staging's own local filesystem state** (`/app/recordings/appliance_identity.json` inside the `portal-green` container, i.e. `/var/lib/anyaicam-staging/recordings/appliance_identity.json` on the staging host) — not anywhere on Ryzen. Ryzen was already freshly wiped and clean-installed earlier this session (RC1→RC4); that state is completely unrelated to and untouched by this defect. Wiping Ryzen again would generate a *new* `device_id`, and the very next claim attempt for that new device would hit the exact same conflict against the exact same stale staging file — the appliance being claimed is irrelevant to this bug; the shared cloud process's own leftover single-tenant file is the entire cause.

### Root cause 2 — claim_state.json (the only copy of the plaintext claim proof) was deleted before enrollment was confirmed durable

`setup_wizard.py`'s `claim_main()` called `clear_claim_state()` immediately after `claim_complete()` returned successfully — before `_finish_enrollment()` (identity files + restart) was even attempted. `claim_state.json` is the *only* place the plaintext `claim_proof` for this device ever lived (the server only ever stores its one-way hash). When local enrollment then failed, the server's own retry-safety recovery window (`claim_complete()`'s `status=='completed'` branch, `CREDENTIAL_RECOVERY_TTL_MINUTES=15`) was still open but unreachable — recovering it requires presenting that exact plaintext proof again, and it was already gone. **Fixed** in `appliance-agent/anyaicam_agent/setup_wizard.py`, commit `2b6bc9e`: `clear_claim_state()` moved to the end of `claim_main()`, after `_finish_enrollment()` returns. A future failure now leaves `claim_state.json` in place, so a retry correctly *resumes* the same session (no new `claim_begin`, no re-polling) and recovers the identical credential via the server's existing retry path.

### Root cause 3 — enrollment's own restart step needed a privilege it doesn't have

`_finish_enrollment()`'s `restart_service()` ran `systemctl restart anyaicam-agent.service` directly via `subprocess.run(check=True)`. `anyaicam-setup` is documented (`appliance-agent/scripts/install.sh`) to run as `sudo -u anyaicam ...anyaicam-setup` — the unprivileged `anyaicam` system user, which owns `/etc/anyaicam`/`/var/lib/anyaicam` but has no authority to restart a system unit and no interactive desktop session for polkit to authenticate through. Confirmed live: this is exactly what produced "systemd restart authentication failed/timed out," which `first_enroll()` correctly treated as fatal and rolled back — compounding root cause 2's damage by discarding the one local escape hatch (retrying the same enrollment) that would otherwise have worked. **Fixed**, same commit `2b6bc9e`: `restart_service()` now queues the restart through the existing root-owned privileged-watcher marker mechanism (`privileged_watcher.py`'s fixed `DISPATCH` table, already used for `restart_vms`) instead of a raw unprivileged call — no new sudoers/polkit rule invented. A failure to queue or run it is now only a warning, never fatal: RC4's `_await_activation()` already makes the agent pick up a freshly written `credential.json` on its own within ~10 seconds, and `verify_authentication()` (a direct HTTPS call with the new credential) is what actually proves activation worked — neither depends on this restart succeeding.

### Current staging server-side state for Ryzen (as of this investigation — not modified since)

```
appliance_claims id=48e3f8f0ddab9340fbc131ef30ff222d  status=completed
  device_id=637ad320-daaa-436e-89c9-70a84f4f54a9  customer_id=4efaf5153f  site_id=4de6186be8
  appliance_id=cc481658689945c7796a69b80821baad
  claimed_at=2026-09-12T02:38:32  completed_at=2026-09-12T02:38:34
  credential_recovery_expires_at=2026-09-12T02:53:34  (this window has since passed — the
    encrypted recovery material is lazily nulled by _recover_completed_result() on its next
    touch, if not already; irrelevant either way, since recovering it always required the
    plaintext claim_proof that was already gone before this window mattered)
appliances id=cc481658689945c7796a69b80821baad  cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9
  customer_id=4efaf5153f  site_id=4de6186be8  activation_status=pending
appliance_credentials id=e8fb41263d61beae  appliance_id=cc481658...  not revoked
  (real, hashed, currently un-deliverable-to-Ryzen credential)
```
`customers[4efaf5153f]` = Alejandro Mata / alexmata25@gmail.com, `status: active` — confirmed the correct real account, not a stale/wrong identity. `sites[4de6186be8]` = "Ryzen Home Site" — correct. **`claim_begin()` will refuse any new claim for this device_id** with `409 "This device is already provisioned. Use the existing activation flow."` (it checks `appliances.cloud_id` before ever opening a session) — this is a clean, safe refusal, not corruption, but it does mean a fresh claim cannot succeed until these three rows are cleared.

Staging's own unrelated `/app/recordings/appliance_identity.json` is unchanged and untouched: `cloud_id: AIC-C90CF0C9`, `appliance_id: 5e76625989`, `customer_id: 4efaf5153f`, `site_id: 4de6186be8`, `activated_at: 2026-09-11T07:23:02`. Per this phase's instructions, **not altered** — and with the source fix deployed, cloud role will never read or write it again regardless, so it can be left in place indefinitely without further risk once the fix ships.

### Current Ryzen local state

`anyaicam-agent.service`: still `active (running)`, same PID/start time as RC4's original deploy, zero crashes/restarts from this incident — confirmed via `journalctl`/`systemctl status` (no sudo needed). Per `first_enroll()`'s own documented rollback-on-failure behavior (source-confirmed, not independently re-verified on Ryzen's filesystem — those paths are root-owned and this session does not request or handle sudo passwords): `agent.json`, `credential.json`, and the VMS's own `appliance_identity.json` should all have been deleted again after the failed restart step, leaving Ryzen exactly as unactivated as before the attempt. `claim_state.json` is gone (deleted by the pre-fix `clear_claim_state()` ordering, root cause 2 above) — **this specific already-lost file is not retroactively recoverable by tonight's source fix**; the fix only prevents this exact loss from happening again on a *future* attempt.

### Source fixes, tests, commits

- `bd633a2` — `local_activation_tracking_applies()` gate; `app/appliance_activation.py`, `app/appliance_claims.py`, `app/appliance_cloud.py`. Tests: `app/tests/test_appliance_activation_local_tracking_gate.py` (new, 6 cases), `app/tests/test_claim_flow_end_to_end.py` (+3 real end-to-end cases: claim completes despite an unrelated conflicting local file reproducing the exact staging incident; two independent devices both claimable against one shared identity file; edge role still enforces the original conflict correctly).
- `2b6bc9e` — `clear_claim_state()` reordering + `restart_service()` privileged-marker fix; `appliance-agent/anyaicam_agent/setup_wizard.py`, `appliance-agent/system/privileged_watcher.py`. Tests: `appliance-agent/tests/test_setup_wizard_claim.py` (+1 case: claim_state survives a failed enrollment and correctly resumes), `appliance-agent/tests/test_finish_enrollment_restart_privilege.py` (new, 4 cases), `appliance-agent/tests/test_rdm4_privileged_actions.py` (updated for the new `restart_agent` DISPATCH entry).

**Full test results**: `app/tests` — every file touching `appliance_cloud`/`appliance_claims`/`appliance_activation` (34 files) run: **385 passed**, 28 failed — confirmed via `git stash` comparison to be **pre-existing, unrelated, platform-specific** (camera-provisioning/talk-relay tests already failing identically on the unmodified codebase in this Windows dev environment; none touch the files changed here). `appliance-agent/tests` — full suite: **488 passed**, 3 failed — likewise confirmed pre-existing/unrelated (Windows-vs-POSIX filesystem-error-semantics mismatches in the updater's own tests, untouched by this work). **Zero new regressions.** Installer/packaging tests not run — no installer files were touched this phase.

### Explicit recovery plan (separately authorized, not started)

1. **Deploy** commits `bd633a2`+`2b6bc9e` to `anyaicam-staging` (rebuild `deploy-portal` image, recreate `portal-green`) so any *future* claim gets all three fixes.
2. **Controlled staging cleanup** — after the fix is live, delete/reset exactly these three stranded rows for device `637ad320-...`: `appliance_claims` id `48e3f8f0ddab9340fbc131ef30ff222d`, `appliances` id `cc481658689945c7796a69b80821baad`, `appliance_credentials` id `e8fb41263d61beae`. Nothing else. Staging's unrelated `AIC-C90CF0C9` file stays untouched (see above — it's inert once the fix is live).
3. **Verify** — `/health`/`/version` on the redeployed golden image, confirm the three rows are gone and `claim_begin()` no longer refuses this device_id, confirm no unrelated data changed.
4. **One new end-to-end Ryzen claim** — same appliance identity `637ad320-daaa-436e-89c9-70a84f4f54a9`, full claim → confirm → complete → enroll cycle, this time exercising all three fixes together.

**Step 1 of this plan is now done — see the next section.** Steps 2-4 remain pending separate authorization.

---

## 2026-09-12: Deployed the three-defect fix (`bd633a2`, `2b6bc9e`, checkpoint `b8bdf2c`) to staging — VERIFIED

Deployment only. No database cleanup, no Ryzen, no Samsung, no hot-patching — exactly as authorized. Followed the golden process: source committed → build versioned artifact → record hash/digest → deploy → verify exact version → health/regression smoke tests → checkpoint → stop.

### Versioned artifact

- **Git commit deployed**: `b8bdf2c` (ancestry includes `bd633a2`, `2b6bc9e`) — clean working tree, no uncommitted changes, confirmed via `git log`/`git status` immediately before packaging.
- **Source packaging**: `git -c core.autocrlf=false archive b8bdf2c` (the CRLF-safe flag this doc's own earlier cutover found the hard way is required) → `app/`, `requirements.txt`, `requirements-cpu.txt`, `Dockerfile` tarred into `staging-deploy-b8bdf2c.tar.gz`, SHA-256 `e18c9cc41f13ccec0557092ded74f06709f8ef5d196843cc2fa988469dcd6503` — verified identical after `scp` transfer to the staging host.
- **Byte-for-byte pre-transfer verification** (every changed file, `git show b8bdf2c:<path>` vs. the archived copy): `app/appliance_activation.py`, `app/appliance_claims.py`, `app/appliance_cloud.py`, `appliance-agent/anyaicam_agent/setup_wizard.py`, `appliance-agent/system/privileged_watcher.py` — all five **MATCH** exactly.
- **Image tag**: `deploy-portal:b8bdf2c` — a real, distinct, immutable tag, **not** `:latest`. This is a deliberate improvement over the earlier live cutover's own approach (which rebuilt directly under `:latest` and thereby made the *old* running image unrecoverable once containerd's snapshotter reassigned its tag — see the "Staging live cutover" section's own durable finding above). Building under a brand-new tag instead means the previous image, `sha256:0c4a3b7cebf8db5168fa851b98bc3bbe367f021bf19f17750f74838518048a57` under `deploy-portal:latest`, was **never touched** and remains a fully intact, instantly-usable rollback target.
- **New image digest**: `sha256:bc35dba1afc648c83a30fa7112228cb74e0fba1ce7062ba1d734b0505ca1c200`.
- **Pre-deploy backups** (taken before any write, matching this doc's own established pattern): source tree `/opt/anyaicam-staging-source-backup-pre-defect-fix-deploy-20260912T031947Z.tar.gz` (SHA-256 `b9ddb5cb104b92c8a667e34f411ee68a0a7481da7856f0b0767045f5293d7e7b`); DB `/var/lib/anyaicam-staging/db/staging-pre-defect-fix-deploy-20260912T031947Z.db` (SHA-256 `7952c5e9697d490ef14fcc5315587365787b16f4feb0ebefe14828f94bd85c04`).

### Deployment path

Source rsync'd into `/opt/anyaicam-staging/{app,requirements.txt,requirements-cpu.txt,Dockerfile}` (verified `diff -rq` exit 0 after; `deploy/` and `storefront/` mtimes confirmed unchanged). Built via `docker build -t deploy-portal:b8bdf2c` from that tree. `portal-green` recreated in place (`docker stop && docker rm && docker run` with the **identical** `--env-file /home/ubuntu/blue-green-rehearsal/green.env`, identical volume mounts for `db`/`recordings`/`hls`/`data-config`, identical `deploy_default` network) — the exact same low-risk recreation procedure already used twice earlier tonight for the secret-key and DB-path fixes, chosen over a fresh blue/green pair specifically to avoid a second SQLite writer against the live DB (this codebase's `live_relay_idle_sweep_worker` and other background workers start at process boot regardless of whether Caddy is routing traffic to a container, so two simultaneously-running containers sharing the DB volume is unsafe even before any traffic cutover). Caddy's upstream (`portal-green:8000`) needed no change — same container name, Docker's own embedded DNS resolves it to the new container automatically. Total downtime: a few seconds, consistent with the two prior same-session recreations.

### Post-deployment verification — all passed

| Check | Result |
|---|---|
| `/health` (internal + public) | `200 ok`, `runtime_role: cloud` |
| `/version` (public) | Correct; `cloud_id: AIC-C90CF0C9` (still reported — see below), `aws_region: null` |
| Deployed image | `sha256:bc35dba1afc648c83a30fa7112228cb74e0fba1ce7062ba1d734b0505ca1c200`, tag `deploy-portal:b8bdf2c` — confirmed via `docker inspect` |
| Exact deployed commit | `sha256sum` of `appliance_activation.py`/`appliance_claims.py`/`appliance_cloud.py` **inside the running container** matches `git show b8bdf2c:...` byte-for-byte, all three files |
| Fix is live and functionally correct | `local_activation_tracking_applies()` called directly inside the running container with its real ambient `ANYAICAM_RUNTIME_ROLE=cloud` returns `False` (skips `persist_activation()`, as intended); manually forced to `edge` returns `True` (original protection intact) |
| Real staging DB in use (before AND after, per this phase's explicit ask) | Pre-deploy: `ANYAICAM_PARTNER_DB=/app/data/staging.db` confirmed in the outgoing container. Post-deploy: identical value confirmed in the new container, plus a live query returning real, correct data (below) — never the `/rehearsal/green-disposable.db` path from the earlier config-drift incident |
| `ANYAICAM_CLAIM_FLOW_SECRET_KEY` | Present by name only (`count=1`), value never displayed, both before and after |
| Customer login page | `/customer-login.html` → `200` (page itself verified reachable; no login was attempted on the user's behalf) |
| Customer/site data intact | `alexmata25@gmail.com` row unchanged (`account_status: active`, `approved: 1`, `created_at` unchanged since 2026-09-11); `partner_users=4`, `customers=3`, `sites=3` — all unchanged from pre-deploy baseline |
| Stranded Ryzen rows unchanged | `appliance_claims id=48e3f8f0...` (`status=completed`, same `customer_id`/`site_id`/`appliance_id`/timestamps), `appliances id=cc481658...` (same `cloud_id`/`activation_status=pending`), `appliance_credentials id=e8fb4126...` (`revoked_at: None`) — every field identical to the pre-deploy read, confirmed **not** touched by this deployment |
| Historical `AIC-C90CF0C9` file | Re-read post-deploy (excluding the credential field): identical to the pre-deploy read — `appliance_id: 5e76625989`, `customer_id: 4efaf5153f`, `site_id: 4de6186be8`, `activation_version: 1`, `activated_at: 2026-09-11T07:23:02` — untouched, as instructed |
| AWS / Motion Cloud | No change — `aws_region: null`, same as every prior check this session |
| DB integrity | `PRAGMA integrity_check: ok`, post-deploy |
| No duplicate writers | `docker ps`: exactly one portal container (`portal-green`, new image) running; the old container was fully stopped and removed, never left running alongside the new one |

### State to resume from

`anyaicam-staging` is now running the fixed source (`b8bdf2c`) under image `deploy-portal:b8bdf2c` (`sha256:bc35dba1...`). The stranded Ryzen claim/appliance/credential rows are exactly as they were — **cleanup has not been performed** and needs separate authorization, per the recovery plan above (steps 2-4). Ryzen and Samsung were not touched at any point in this deployment. No new claim was started.

---

## 2026-09-12: Controlled cleanup of the three stranded Ryzen claim records — DONE

Narrowly-scoped cleanup only, exactly as authorized. No new claim started, `anyaicam-setup` not run on Ryzen, no other appliance/claim/credential/customer/site touched, historical `AIC-C90CF0C9` file untouched, staging config/secrets untouched, Ryzen filesystem/Samsung/cameras/AWS untouched.

### Pre-cleanup verification

All three target rows re-read fresh and confirmed to match exactly before touching anything: `appliance_claims id=48e3f8f0ddab9340fbc131ef30ff222d` (`customer_id=4efaf5153f`, `site_id=4de6186be8`, `appliance_id=cc481658689945c7796a69b80821baad`, `status=completed`), `appliances id=cc481658689945c7796a69b80821baad` (`cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9`, same customer/site), `appliance_credentials id=e8fb41263d61beae` (`appliance_id=cc481658...`). FK/dependency scan across every one of the 14 tables in this schema that reference `appliance_id` found exactly one dependent row anywhere — the target `appliance_credentials` row itself; nothing else in the entire database points at this appliance_id. `PRAGMA integrity_check: ok` immediately before mutation.

### Backup

`/var/lib/anyaicam-staging/db/staging-pre-ryzen-claim-cleanup-20260912T032638Z.db`, SHA-256 `7952c5e9697d490ef14fcc5315587365787b16f4feb0ebefe14828f94bd85c04` (identical to the deployment phase's own pre-deploy backup hash — confirms zero incidental DB writes occurred between deployment and this cleanup).

### What was actually changed — one transaction, every statement scoped by exact row id plus every other field re-checked, never by `customer_id`/`site_id` alone

1. `DELETE FROM appliance_credentials WHERE id='e8fb41263d61beae' AND appliance_id='cc481658689945c7796a69b80821baad'` — removed the one real, usable (though never-delivered) credential from the failed claim.
2. `UPDATE appliance_claims SET revoked_at=<now>, appliance_id=NULL, completed_credential_encrypted=NULL, credential_recovery_expires_at=NULL WHERE id='48e3f8f0ddab9340fbc131ef30ff222d' AND device_id='637ad320-...' AND customer_id='4efaf5153f' AND site_id='4de6186be8' AND appliance_id='cc481658...' AND status='completed'` — the historical row is kept (not deleted, preserving the audit trail of what happened) but explicitly revoked and detached from the appliance row being removed next; its already-expired recovery material is also explicitly cleared rather than left to a TTL. `status` intentionally left as `completed` (accurate history) rather than inventing a new status value never otherwise used in this table (`completed`/`expired`/`pending` are the only three ever written) — `revoked_at` is the existing, schema-provided signal for "this is dead, do not act on it further."
3. `DELETE FROM appliances WHERE id='cc481658689945c7796a69b80821baad' AND cloud_id='637AD320-...' AND customer_id='4efaf5153f' AND site_id='4de6186be8'` — removed the provisioned-appliance row, which is the *only* thing `claim_begin()` actually checks (`SELECT id FROM appliances WHERE cloud_id=?`) before refusing a new claim for this device.

Each statement asserted `rowcount==1` inside the same transaction before committing; a real re-check of all three rows' values ran again immediately before the writes, inside the transaction. All three assertions passed; committed once, cleanly.

### Post-cleanup verification — all passed

| Check | Result |
|---|---|
| No appliance row for `cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9` | `0` rows — **this is the literal query `claim_begin()` runs**, proving a fresh claim for this exact Cloud ID will no longer be refused |
| No usable credential from the failed claim | `appliance_credentials id=e8fb4126...` — `0` rows, gone |
| Stranded claim cannot be reused | `revoked_at` set, `appliance_id=NULL`, `completed_credential_encrypted=NULL`, `credential_recovery_expires_at=NULL` — even if its plaintext claim_proof still existed anywhere (it doesn't), there's no longer any recovery material or appliance link left to recover into |
| Customer account | `alexmata25@gmail.com` / `customers[4efaf5153f]` — unchanged, `active` |
| Ryzen Home Site | `sites[4de6186be8]` — unchanged, present |
| Other claim rows for this device_id | The 2 pre-existing `expired` rows and the 1 `pending` (already-TTL-stale, out of this cleanup's scope) row — every field identical to before, untouched |
| Unrelated table counts | `appliances: 3` (was 4, now correctly missing only the one removed), `appliance_credentials: 3` (same), `partner_users/customers/sites: 4/3/3` — all unchanged from pre-cleanup baseline |
| Historical `AIC-C90CF0C9` file | Re-read post-cleanup: byte-identical to every prior read this session |
| DB integrity | `PRAGMA integrity_check: ok`, post-cleanup |
| `/health` | `200 ok` |
| `/version` | Still `deploy-portal:b8bdf2c` / `sha256:bc35dba1...` — confirmed via `docker inspect`; container never restarted for this cleanup (pure data operation, no redeploy) |
| Container count | Exactly one portal container (`portal-green`), unchanged |

### State to resume from

Ryzen's Cloud ID (`637ad320-daaa-436e-89c9-70a84f4f54a9`) is now genuinely unclaimed in staging — no `appliances` row, no credential, nothing to block a fresh `claim_begin()`. **No new claim has been started.** `anyaicam-setup` has not been run on Ryzen. The next step — one new end-to-end Ryzen claim exercising all three source fixes together — needs separate explicit authorization.

---

## 2026-09-12: Second stranded Ryzen claim — the appliance-agent fix was never on Ryzen at all

The "one new end-to-end Ryzen claim" from the previous section was attempted. **The old `AIC-C90CF0C9` conflict did not recur** (defect 1, deployed to `app/`/staging, worked correctly), but local enrollment failed again with the *exact same* symptom as before: a `systemd1.manage-units` polkit prompt, then `Failed to restart anyaicam-agent.service: Connection timed out`, then `First-time appliance enrollment failed; no identity was left behind (CalledProcessError)`.

**Root cause: a deployment-process gap, not a code defect.** `bd633a2`/`2b6bc9e` were deployed to `anyaicam-staging` (a Docker Compose target built directly from `app/`) but Ryzen's `appliance-agent` package is an entirely separate deployable artifact — installed via `installer/build_release_installer.py`'s combined VMS+agent tarball, last built at RC4 (`087e455`), predating both fixes. Ryzen's `setup_wizard.py`/`privileged_watcher.py` were simply never rebuilt or reinstalled. The observed polkit prompt is conclusive proof by itself: `2b6bc9e`'s `restart_service()` never calls `systemctl` directly at all (it only writes a marker file), so a polkit prompt appearing means the pre-fix code ran.

Server-side, this second claim completed and committed cleanly, exactly once, with exactly one credential — proving defect 1's fix works correctly end-to-end on the cloud side:
```
appliance_claims id=73c06c0805a1510950feb8f3a59e67aa   status=completed
  customer_id=4efaf5153f  site_id=4de6186be8  appliance_id=0ca39d9d45d34fbcc6c641924912045c
appliances id=0ca39d9d45d34fbcc6c641924912045c   cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9
appliance_credentials id=f1032092ee7cb5b0   not revoked
```
Ryzen's local state: with very high confidence (inferred from the confirmed-old code path, not yet directly file-verified), `claim_state.json` was cleared again by the same pre-`2b6bc9e` unconditional ordering — the plaintext claim_proof needed to use the server's still-open recovery window is gone. This second claim is now **stranded for the identical reason as the first, not a new defect** — cleanup has **not yet been performed**, awaiting separate authorization (see below).

**Durable release-process lesson, recorded per explicit request**: a fix that spans `app/` and `appliance-agent/` is not "deployed" merely because the staging cloud container was rebuilt. These are two independent deployment targets with two independent pipelines (staging: direct Docker Compose build from `app/`; edge appliances: `installer/build_release_installer.py`'s combined tarball, installed via `install.sh`). **Any future release checklist touching both directories must explicitly identify and deploy every affected target** — checking off "staging redeployed" must never be read as "the fix is live everywhere."

## 2026-09-12: Corrected appliance-agent release built and installed on Ryzen — VERIFIED

Built a new versioned installer from the same corrected commit already live on staging, and installed it on Ryzen via the proven repair path — closing the release-process gap above.

### Release artifact record

- **VMS release commit**: `b8bdf2cf98c716067024bdf471d834ce5cd602e1` (full 40-char SHA)
- **Installer filename**: `anyaicam-appliance-installer-1.1.0-vms-b8bdf2cf98c7.tar.gz`
- **Installer SHA-256**: `17880eaf85b4db4e42815ff637798cd1cf6cb2331875c3d8c75bf42d97c098c0` — verified identical before and after `scp` to Ryzen
- **VMS release payload archive SHA-256** (from `release-manifest.json`): `79fd6371a2973a21151ca288412f56e3dd53cda8336181100849e5173a0a562b`
- **appliance-agent wheel**: `anyaicam_appliance_agent-0.1.0-py3-none-any.whl`, SHA-256 `dfb79dd8783f700165f934af1ae89f1b3305678e0f2381ae96628b446a7f7c6c` (from the `pip install` build log — this pipeline builds no other named wheel artifact)
- **VMS Docker image**: `anyaicam-vms:latest`, digest `sha256:73f83ae7f69567450dce5522fc1e2a2e64ff467a44551d81fa0e77f93fa96b3d`, built fresh on Ryzen from this exact payload
- Byte-for-byte verified (by me, before transfer): `appliance-agent/anyaicam_agent/setup_wizard.py`, `appliance-agent/system/privileged_watcher.py`, and all 3 fixed `app/` files inside the built artifact match `git show b8bdf2c` exactly

### Install

Repair path (`03-detect-install.sh`: 5/5 markers → `existing`), same proven procedure as RC2-RC4. Installer's own log confirms: VMS rebuilt from the exact release payload with persistent state untouched; appliance-agent package rebuilt and reinstalled (`pip uninstall` of the old `0.1.0` wheel, install of the new one — same version number, different content, matching this project's pre-1.0 versioning); **appliance identity preserved, not regenerated** (`sha256=1b418261861b4b91b4ab540ecd17f5c49a4a266399ee00315a7593221a0be288`, confirmed unchanged); installed release stamped as `b8bdf2cf98c716067024bdf471d834ce5cd602e1`.

### Verification — full chain, source to runtime

| Check | Result |
|---|---|
| `/version` (Ryzen, checked directly) | `build_id: "b8bdf2cf98c716067024bdf471d834ce5cd602e1"` — the running edge process reports the exact commit |
| Official validator | `validate.sh`: **0 failures**, exact release `b8bdf2cf98c716067024bdf471d834ce5cd602e1` |
| Installed source content | User-confirmed direct read: `setup_wizard.py` calls `_queue_privileged_action(config,'restart_agent',{'confirmed':True})`; `privileged_watcher.py`'s `DISPATCH` contains `restart_agent`; no remaining unprivileged direct `systemctl restart` in the enrollment path |
| `anyaicam-agent.service` | `active`/`running`, `NRestarts: 0` (reset by the install's own restart), first log line: `"Appliance is not activated yet; waiting for anyaicam-setup (interactive or --claim) to complete..."` — clean, no heartbeat/"revoked" noise since |
| `anyaicam-vms.service` | `active`/`exited` (correct for this oneshot unit), enabled |
| `/health` | `200 ok` |
| `/ready` | `503` — correct, appliance still unclaimed |
| Appliance identity | `637ad320-daaa-436e-89c9-70a84f4f54a9` — preserved, confirmed by the installer's own identity-file hash check |
| Cameras | Zero, untouched |
| Motion Cloud / AWS | `aws_region: null`, no flags |
| Manual/runtime patches | None — only the versioned installer ran |

### State to resume from

Ryzen now has the corrected appliance-agent installed and verified. The second stranded staging claim (`appliance_claims 73c06c0805a1...`, `appliances 0ca39d9d45d3...`, `appliance_credentials f1032092ee7c...`) has **not** been cleaned up yet — awaiting separate authorization, same exact-id-scoped pattern as the first cleanup. No new claim has been started. Once that cleanup runs, a third end-to-end claim attempt should finally exercise all three fixes correctly on both sides.

---

## 2026-09-12: Controlled cleanup of the second stranded Ryzen claim records — DONE

Identical procedure to the first cleanup: pre-verification, backup, FK/dependency scan, exact-id-only transaction, full post-cleanup verification. No other appliance/claim/credential/customer/site touched, Ryzen/appliance identity untouched, no new claim started, Samsung untouched, no Motion Cloud/AWS enabled.

### Pre-cleanup verification

All three target rows re-read fresh and confirmed exact: `appliance_claims id=73c06c0805a1510950feb8f3a59e67aa` (`device_id=637ad320-...`, `customer_id=4efaf5153f`, `site_id=4de6186be8`, `appliance_id=0ca39d9d45d34fbcc6c641924912045c`, `status=completed`), `appliances id=0ca39d9d45d34fbcc6c641924912045c` (`cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9`, same customer/site), `appliance_credentials id=f1032092ee7cb5b0` (`appliance_id=0ca39d9d...`). FK/dependency scan across all 14 tables referencing `appliance_id` found exactly one dependent row anywhere — the target credential itself. `PRAGMA integrity_check: ok` immediately before mutation.

### Backup

`/var/lib/anyaicam-staging/db/staging-pre-ryzen-claim2-cleanup-20260912T035620Z.db`, SHA-256 `da40ec781047a7acde2fe2d4e32c83793f8c802a65ea6c65927947157608d961`.

### What was changed — one transaction, exact-id + every other field re-checked, never by `customer_id`/`site_id` alone

1. `DELETE FROM appliance_credentials WHERE id='f1032092ee7cb5b0' AND appliance_id='0ca39d9d45d34fbcc6c641924912045c'` — removed the second claim's real, unused credential.
2. `UPDATE appliance_claims SET revoked_at=<now>, appliance_id=NULL, completed_credential_encrypted=NULL, credential_recovery_expires_at=NULL WHERE id='73c06c0805a1510950feb8f3a59e67aa' AND device_id='637ad320-...' AND customer_id='4efaf5153f' AND site_id='4de6186be8' AND appliance_id='0ca39d9d...' AND status='completed'` — row kept for history, revoked and detached, exactly the same treatment as the first cleanup's `48e3f8f0...` row.
3. `DELETE FROM appliances WHERE id='0ca39d9d45d34fbcc6c641924912045c' AND cloud_id='637AD320-...' AND customer_id='4efaf5153f' AND site_id='4de6186be8'` — removed the row blocking `claim_begin()`.

Each statement asserted `rowcount==1` inside the transaction before commit; committed once, cleanly.

### Post-cleanup verification — all passed

| Check | Result |
|---|---|
| No appliance row for `cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9` | `0` rows — **Cloud ID is free for a fresh claim** |
| Credential gone | `appliance_credentials id=f1032092...` — `0` rows |
| Claim row revoked/sanitized | `revoked_at` set, `appliance_id=NULL`, recovery material cleared, `status` left `completed` for history (same convention as the first cleanup) |
| Customer account | `alexmata25@gmail.com` / `customers[4efaf5153f]` — unchanged, active |
| Ryzen Home Site | `sites[4de6186be8]` — unchanged, present |
| All 5 `appliance_claims` rows for this device_id | Both stranded claims (`48e3f8f0...`, `73c06c0805a1...`) now correctly revoked; the 2 `expired` and 1 `pending` rows untouched |
| Unrelated table counts | `appliances: 3`, `appliance_credentials: 3`, `partner_users/customers/sites: 4/3/3` — unchanged |
| Historical `AIC-C90CF0C9` file | Byte-identical to every prior read this session |
| DB integrity | `ok`, before and after |
| `/health` / `/version` | `200 ok`; still `deploy-portal:b8bdf2c` — no restart performed for this pure data operation |
| Containers | Exactly one portal container (`portal-green`), unchanged |

### State to resume from

Cloud ID `637ad320-daaa-436e-89c9-70a84f4f54a9` is genuinely unclaimed in staging again, and Ryzen now has the corrected appliance-agent installed and verified (previous section). **Cleared for one fresh end-to-end claim attempt**, pending separate authorization — this one should finally exercise the cloud-side fix (defect 1, already proven twice) together with the appliance-side fixes (defects 2-3, now actually installed) for the first time.

---

## 2026-09-12: Third claim attempt — SUCCEEDED. Ryzen is genuinely, durably activated. Milestone PASSED.

Claim code `FA89A9EC`, confirmed by the customer for Ryzen Home Site, camera discovery declined (`n`). No polkit prompt, no `AIC-C90CF0C9` conflict, no rollback. `_finish_enrollment()` printed both of its success lines (`"Configuration saved securely."` / `"AnyAiCam service restarted and authenticated during identity commit."`) for the first time this entire incident.

### Cloud side (staging, read-only)

```
appliance_claims id=2b7a1fb8452cfbbee722093c967428ea   status=completed   revoked_at=None
  customer_id=4efaf5153f   site_id=4de6186be8   appliance_id=7844ceab86e7fab2845125cffeb8ad10
appliances: exactly 1 row for cloud_id 637AD320-DAAA-436E-89C9-70A84F4F54A9
  online_status=online   last_check_in=2026-09-12T04:05:24 (seconds-fresh at check time)
  software_version=0.1.0   camera_capacity=0
appliance_credentials: exactly 1 row (ffdf68c39378f4e9), not revoked -- no duplicate credential
DB integrity: ok
```

### Ryzen (checked directly, read-only)

- `/health`: `200 ok`, `build_id: b8bdf2cf98c716067024bdf471d834ce5cd602e1` — exact deployed release
- `/version`: `cloud_id: 637AD320-DAAA-436E-89C9-70A84F4F54A9` — genuinely activated
- `/ready`: `503`, correctly so — `self_test.ok: true` (0 critical); only 3 non-critical warnings (`ANYAICAM_ADMIN_EMAIL`/`ANYAICAM_ADMIN_PASSWORD`/`ANYAICAM_PORTAL_SECRET` missing, expected/unrelated to claim); `cameras_total: 0`; every `cloud.*` AWS/S3/Motion-Cloud flag `false`/unconfigured
- Agent: `active`/`running`, `NRestarts: 0`, journal: `"AnyAiCam appliance agent started cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9 mode=production"` then a successful `"Entitlement refreshed camera_slot_quantity=0"` — genuine authenticated traffic
- **No polkit prompt** — confirms the `restart_agent` privileged-watcher fix fired correctly this time
- One benign, self-healing race observed and worth recording: the long-running daemon (still mid-poll from the prior manual restart) briefly picked up the freshly-written `credential.json` moments before the real privileged-watcher restart fired (its own ~10s grace period), logging one harmless "revoked or unknown" attempt before being cleanly replaced by a new process that authenticated correctly on its first real attempt. No manual intervention, no lasting effect — exactly the kind of transient this design already tolerates.
- Claim-state cleanup timing: confirmed by direct correlation — the terminal's two final success lines only print if `_finish_enrollment()` returns without raising, and per the confirmed-installed `2b6bc9e` source, `clear_claim_state()` runs strictly after that return. Cleared only after genuine success, not before.

### Verdict

**Claim/activation milestone: PASSED.** All three source fixes (`bd633a2` cloud-side, `2b6bc9e` appliance-side) proven correct together, for the first time, end to end, on real hardware against real staging. Appliance identity `637ad320-daaa-436e-89c9-70a84f4f54a9` preserved throughout every attempt tonight. Zero cameras, no Motion Cloud/AWS, no manual/runtime patches, Samsung untouched.

### Exact next step

Camera reconnection, reboot-persistence validation, and Live Relay/Motion Cloud/customer-portal camera-mapping checks remain — same as every prior RC checkpoint's own "exact next step" — and were explicitly deferred again this session. Update `docs/checkpoints/RYZEN.md` before starting any of that work.

---

## 2026-09-12: Camera discovery verified end to end; provisioning blocked by a second never-provisioned secret (`ANYAICAM_CAMERA_CREDENTIAL_KEY`) — found and fixed

**Discovery, fully verified working through the real cloud path.** The customer setup wizard defaulted to the wrong appliance at first (Step 3 showed the historical `AIC-C90CF0C9` as "degraded") — root-caused to `customer_first_setup()`'s appliance query having no `ORDER BY`, so with two appliances on one customer account it silently defaults to whichever SQLite returns first (insertion order put the older, stale test appliance ahead of the live Ryzen one). Not fixed in source this session (flagged for a future explicit fix); the supported workaround — manually selecting the correct appliance from the dropdown — works today and was used. Once Ryzen was selected, the customer-portal scan job (`camera_scan_jobs id=b4eb032898ee`) correctly triggered the agent's real `poll_discovery()`, found the same 5 cameras (by count and ONVIF/RTSP capability) the direct on-appliance scan found minutes earlier, all with stable ONVIF device-key identities, IP/raw-endpoint correctly redacted before leaving the appliance.

**Provisioning Camera 1 then hit a second missing secret**: `POST /api/customer/cameras/provision` requires `ANYAICAM_CAMERA_CREDENTIAL_KEY` to encrypt submitted camera credentials before storage; `encrypt_camera_credentials()` fails closed (by design, same Fernet pattern as `ANYAICAM_CLAIM_FLOW_SECRET_KEY`) when it's unset, producing the 503 "Camera credential handling is not configured." Confirmed: this key was simply never provisioned for staging — identical defect *class* to the claim-flow-secret incident earlier tonight, a completely separate key by design (so a leak of one never exposes the other), just never set up either. Not a source defect, not an undeployed component — the code is correct and working exactly as designed. Confirmed no trace of the failed attempt was ever stored: `cameras`/`camera_provisioning_requests` for this appliance both `0` rows — the 503 fires before any write.

**Fixed**: generated via the container's own `Fernet.generate_key()`, appended by name only (value never displayed) to both `/etc/anyaicam-staging/vms-staging.env` and `~/blue-green-rehearsal/green.env`; `portal-green` recreated from the **same already-deployed image** (`deploy-portal:b8bdf2c`, digest unchanged, no rebuild, no source change). Verified: `ANYAICAM_CAMERA_CREDENTIAL_KEY` present by name (count 1) in the running container; `ANYAICAM_CLAIM_FLOW_SECRET_KEY` unaffected (still present); `/health`/`/version` correct (internal + public, `200 ok`); Ryzen's appliance row shows `online_status=online` with a fresh `last_check_in` spanning the recreation (no disruption to the existing activation/heartbeat — Ryzen itself was never touched); the one existing credential (`ffdf68c39378f4e9`) still valid, not revoked; DB integrity `ok`; zero cameras or provisioning requests exist for this appliance (confirmed nothing was provisioned as a side effect of either the failed attempt or this fix).

### State to resume from

Safe to retry Camera 1's provisioning through the customer portal now. No credentials were recovered, injected, or reused — you'll re-enter Camera 1's username/password fresh. Cameras 2-5, Motion Cloud/AWS, Ryzen configuration, and Samsung remain untouched throughout.

---

## 2026-09-12: Camera 1 provisioning blocked again — "Camera limit reached: 0 camera(s)" — traced to a genuinely missing customer-facing checkout, built and deployed

Re-attempting Camera 1 after the credential-key fix hit a *different* wall: `customer_entitlements.total_camera_slots('4efaf5153f')` correctly summed **zero** real entitlement rows — the customer had never had one. Traced every candidate source: `customer_entitlements` table (0 rows), the legacy `plans` table (1 row, `camera_quantity: 5`, `status: 'quote'`, explicitly documented as untrustworthy for billing), no Stripe purchase ever recorded. Then traced every checkout-creation call site in the app and found the real gap: `customer_entitlements.PLAN_TIERS` has 8 real, staging-configured Stripe test-mode Price IDs (confirmed non-empty, e.g. `ANYAICAM_STRIPE_PRICE_LOCAL_1_8`) and the webhook side (`resolve_tier()`/`_sync_checkout_completed()`/`upsert_entitlement()`) is fully wired and correct — but **no code path in the entire app ever created a Checkout Session using one of those prices**. The only existing purchase button, `POST /api/payments/checkout`, is for a completely unrelated product line (starter/professional/enterprise software license tiers). Not a config/deployment gap like the two secret keys earlier this session — a genuinely missing source component. No entitlement was manually inserted, no capacity check bypassed, no one-off transaction created.

### Fix built (`d269413`)

- **`POST /api/customer/camera-slots/checkout`** (`app/main.py`), mirroring `create_hardware_checkout()`'s clean shape. `mode=subscription`; tier resolved server-side only from `PLAN_TIERS` by `(plan_type, tier_label)` — never a browser-submitted price id; `customer_owner` identity required (403 otherwise) so `metadata[anyaicam_customer_id]` is always set, matching exactly what the existing webhook expects.
- **UI**: the customer setup wizard's "Camera plan" line now offers a tier picker + "Buy camera capacity" button (only tiers with a real configured Price ID are listed), redirecting to the real Stripe Checkout URL.
- **Tests**: `app/tests/test_camera_slot_checkout.py` (new, 11 cases) — full metadata on success, cross-tier tampering guard (hybrid never resolves to local's price), ownership enforcement (401 unauthenticated / 403 wrong role including `customer_viewer`, Stripe never called), tier/price validation (unknown tier → 400; real-but-unconfigured tier → 503 `PRICE_ID_REQUIRED`), a submitted `price_id`/`camera_slot_maximum` in the body is ignored, and two webhook-integration tests proving this endpoint's exact metadata is genuinely compatible with the existing, unmodified `sync_entitlement_from_stripe_event()`.
- **Full regression run**: 85 passed / 1 pre-existing failure (`test_real_failed_provisioning_leaves_a_retryable_no_camera_state` — the same "0 camera(s)" gap this commit fixes, unrelated to this diff, pre-existing per earlier git-stash confirmation this session) across every appliance-claims/customer-setup/Stripe-checkout/hardware/camera-discovery test file. Zero new regressions.

### Deployed to staging

- **Commit**: `d26941372e5a79ce92c1646690688f21bd437023`
- **Image**: `deploy-portal:d269413`, digest `sha256:7cb5dc162b94f1c45fc439dd563578e3921f8affff9e8e65e481e6391b25504f`
- **Source tarball SHA-256**: `af5747c8b120fa87e6d24ffb7280352c6aab882993a6a284258b1bfc573f399c`, verified identical before and after transfer
- **Pre-deploy backups**: source `/opt/anyaicam-staging-source-backup-pre-camera-slot-checkout-20260912T050028Z.tar.gz` (SHA-256 `42a03f17de326e3bee452b9cb7ef2b4517fabe16a020e118615a8d6307b41f52`); DB `/var/lib/anyaicam-staging/db/staging-pre-camera-slot-checkout-20260912T050028Z.db` (SHA-256 `f0d27095aca4ce7b06df9f40f2820c30f212573b227c3d440daa8e79a1d7da36`)
- **Verified**: `main.py`/`partner_workspace.py` hashed inside the running container match `git show d269413` exactly; `/health` `200 ok` (internal + public); `/version` correct, `aws_region: null`; DB integrity `ok`; exactly one portal container running; `customer_entitlements` for `4efaf5153f` still `0` rows (deployment itself grants nothing — a real checkout completion is still required)

### State to resume from — STOP before initiating any checkout

The customer-facing action is now live: on the setup wizard's Step 6 "Review your account" panel, the "Camera plan" line now shows a tier selector (only real, purchasable tiers listed) and a **"Buy camera capacity"** button. For the 5-camera Ryzen lab, select **Local 1-8** and click it — this redirects to a real Stripe **test-mode** Checkout Session. No checkout has been initiated. No entitlement exists yet. Camera 1 provisioning remains blocked until a real completed checkout produces one.

---

## 2026-09-12: Clicking "Buy camera capacity" failed — "Stripe is not configured" — traced to the same green.env drift class, restored from the existing (not new) Stripe setup

Same root cause family as every earlier missing-secret incident tonight, but this time nothing needed generating: `ANYAICAM_STRIPE_SECRET_KEY` and `ANYAICAM_STRIPE_WEBHOOK_SECRET` were both already present in the canonical `/etc/anyaicam-staging/vms-staging.env` — confirmed independently in a prior session's own investigation (`docs/phase1-staging-repair-report.md`), consistent with this being the pre-existing Stripe setup from earlier (Samsung-era) testing, not something recreated tonight. Both were simply never mirrored into `green.env`, the file `portal-green` actually runs from — identical drift to the DB-path and both prior secret-key incidents. No new Stripe account, secret key, product, or price was created; both values were copied, never regenerated.

**Fixed**: both secrets copied (values never displayed) from `vms-staging.env` into `green.env`; `portal-green` recreated from the same already-deployed image (`deploy-portal:d269413`, no rebuild). Verified: both variables present by name only; `/health`/`/version` correct (internal + public); Ryzen still `online` with a fresh check-in, undisturbed; DB integrity `ok`; zero entitlements/cameras/provisioning requests created as a side effect.

**Local 1-8 price, verified directly against the real Stripe API** (one read-only `GET /v1/prices/{id}`, expanded to include the product — no checkout, nothing purchased or mutated):
```
id: price_1UD2xKGllhK80H2nFJwtFJvw   livemode: false   active: true
product: prod_VDTvNADoxZ3vPL "AnyAiCam Local VMS — 1–8 Camera Slots"   active: true
```
Definitively confirmed test-mode straight from Stripe itself (stronger than the earlier `sk_test_`-prefix inference). Note for the record: `docs/AI_HANDOFF.md` (an earlier session's doc) describes this same key loosely as "live Stripe keys configured" — that phrasing means "actually present/configured," not literally Stripe live-mode; this live API check is the authoritative answer.

### State to resume from

Cleared to click **"Buy camera capacity"** (Step 6, **Local 1-8** tier) — the checkout should now work end to end using the pre-existing, already-tested Stripe test-mode setup.

---

## 2026-09-12: Multi-appliance camera isolation defect — audited, fixed, tested, deployed, proven against real data

After the Stripe checkout succeeded (8 real camera slots granted), the customer dashboard showed "5 of 8 cameras configured" for the Ryzen appliance -- but read-only verification proved all 5 belonged to the *historical* `AIC-C90CF0C9` appliance (synthetic pre-existing device_keys, zero real provisioning requests, zero agent-reported camera status for Ryzen). Root cause: `GET /api/customer/cameras` and the setup wizard's own Step 5 camera-table render both queried `cameras WHERE customer_id=?` with no `appliance_id` filter -- correct for a customer with one appliance, wrong for this account, which genuinely has two.

**Full audit performed** across every customer-facing surface touching cameras/provisioning/discovery/Live View (`app/partner_workspace.py`, `app/live_view_sessions.py`, `app/camera_mapping.py`, `app/appliance_cloud.py`). Two real gaps found and fixed:
1. `GET /api/customer/cameras` -- now accepts an optional `appliance_id`; when given, verifies it belongs to the customer (404 otherwise) and scopes both the camera list and `configured_camera_count` to it. `expected_camera_count` (the Stripe entitlement) correctly stays account-wide.
2. `customer_first_setup()`'s Step 5 camera-table render -- now filtered to `initial_appliance_id`. The appliance dropdown's `onchange` now saves progress and reloads the page so this (and Step 4's discovery state) always reflects the newly selected appliance.

Hardened `PUT /api/customer/cameras` (defense in depth: verifies each camera also belongs to the passed `appliance_id` before updating it, when one is provided).

**Confirmed already correct, no fix needed** (locked in with new regression tests): `POST /api/customer/appliances/{id}/scan` and `POST /api/customer/cameras/provision` already verify `appliances WHERE id=? AND customer_id=?` -- a customer can never use another customer's appliance_id. Live View (`POST /api/customer/cameras/{id}/live/start`) resolves and queues its relay command against the camera's *own* `appliance_id` read from its own DB row -- never a client-supplied value -- via `camera_mapping.resolve_camera_number()`, itself explicitly appliance+customer scoped.

**Preserved exactly as instructed**: the shared 8-slot Stripe entitlement (unchanged, correctly account-wide by design); the five historical `AIC-C90CF0C9` camera records (untouched, kept as regression evidence); no cameras provisioned; Ryzen/Samsung/camera credentials/Motion Cloud/AWS/appliance identities untouched.

### Tests and deployment

`app/tests/test_camera_multi_appliance_isolation.py` (new, 14 cases): one customer owning two appliances (modeled directly on the live incident) plus a second customer for cross-tenant checks -- proves listing/counting/setup-render/save/provisioning/scan/Live-View all correctly isolate by appliance, and reject a foreign customer's appliance_id or camera_id outright. Full regression: 78 passed / 27 pre-existing unrelated failures (the same "Camera limit reached: 0 camera(s)" gap, already confirmed pre-existing this session). Zero new regressions.

Deployed: commit `0a92bea2e0ab87abbba20c30947f1af063e0726b`, image `deploy-portal:0a92bea` (digest `sha256:26dc4815df3e7e194b5e5762bd2f3a714be82dffcdcf741cade0acbe086a8164`), source tarball SHA-256 `84d7ef8e3624a78dc76192eda47d762f250eb06f87b1120b5eb5e803c4ecde92` verified identical before/after transfer, pre-deploy backups recorded (source and DB, both hashed). `partner_workspace.py` hashed inside the running container matches `git show 0a92bea` exactly. `/health` `200 ok` (internal + public), DB integrity `ok`, exactly one portal container.

**Proven against real production data** (in-process call to the actual, unmodified route function -- no session minted, no HTTP call, no mutation):
```
appliance_id=7844ceab... (Ryzen):        cameras=[]                                 configured=0  expected=8
appliance_id=5e76625989 (historical):    cameras=[01aad49341,d3e67c74b6,810dde938d, configured=5  expected=8
                                                   7327f73df8,11c8b00156]
```
Exactly the corrected behavior the fix was built for.

### Exact customer action to resume real Ryzen provisioning

Return to the customer setup wizard with **Ryzen (`637AD320-DAAA-436E-89C9-70A84F4F54A9`) selected** in the appliance dropdown, go to **Step 4 "Discover cameras"**, click **"Request appliance scan"** again (the discovery results from earlier are still valid -- Ryzen's LAN hasn't changed), and provision **Camera 1** through the same "Add this camera" flow as before, entering its real ONVIF/RTSP credentials yourself when prompted. The dashboard will now correctly show progress against Ryzen specifically, starting from 0 of 8.

---

## Appliance checkpoints

- `docs/checkpoints/RYZEN.md` — the real 5-camera physical appliance, primary
  validation hardware.
- `docs/checkpoints/SAMSUNG.md` — waiting at the cloud identity/activation
  stage. **Do not restart Samsung setup from the beginning** — read that file
  first.
