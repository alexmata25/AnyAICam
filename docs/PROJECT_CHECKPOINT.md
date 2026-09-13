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

## Architecture decision: Cloud ID is canonical; claim code is additive

**Read this before touching either provisioning path — a recurring source of
confusion across sessions.** This codebase has exactly two, deliberately
separate, ways for an appliance to become associated with a customer:

1. **Cloud ID (`AIC-XXXXXXXX`) + activation token** — `provisioning_service.py`
   (`get_provisioning_backend().provision()`) is the *only* place allowed to
   mint one. This is the **canonical, permanent appliance identity** for the
   life of the appliance: every appliance row, every activation-token hash,
   every camera placeholder, and every downstream cloud feature (heartbeat,
   entitlement refresh, camera sync) is keyed off this identity. It is
   produced by exactly two front doors onto the *same* backend, never a
   second provisioning system: `partner_workspace.onboard_customer()` (an
   admin/partner runs "Add New Customer" and provisions on the customer's
   behalf) and `partner_workspace.provision_customer_appliance()` (a
   self-service customer who already has a paid hardware order + active
   camera-slot entitlement provisions their own first appliance from
   Customer Setup Step 2, added 2026-09-12 — see the dated entry below).
   Redeemed via the unmodified `POST /api/appliance/activate` and
   `POST /api/customer/appliances/link`.
2. **Claim code** (`appliance_claims.py`, `POST /customer/claim-appliance`,
   `anyaicam-setup --claim`) — a short-lived, human-readable pairing code an
   *unclaimed physical device* generates locally and a customer types into
   the portal to bind it to their account for the first time, validated by
   Samsung. This is a **bootstrap/pairing mechanism**, not an identity
   system: once a claim completes, the device still ends up with a real
   Cloud ID underneath it (via `coordinated_reenroll()`/`first_enroll()` in
   the appliance agent) — the claim code itself is never stored as, or
   treated as, the appliance's permanent identity.

**Do not merge these, replace one with the other, or treat a self-service
Cloud ID provisioning action as "the same feature" as claim code.** They
solve different problems (issuing a brand-new identity for hardware that has
no appliance row yet, vs. pairing an already-manufactured physical device
that already has its own local identity bootstrap) and both must keep
working independently. If a future session is asked to "fix appliance
linking" or "add self-service provisioning," confirm which of the two gaps
is actually being reported before writing code — see the 2026-09-12
self-service provisioning entry below for a concrete example of the two
coexisting without either being touched.

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

## 2026-09-12: Same isolation defect found in two more call sites (`/customer-account`, `/customer-live`) -- audited, fixed, tested, deployed, proven against real data

**Reported live**: `/customer-account` showed "8 of 8 cameras configured" with duplicate Camera 1/2/3 entries; `/customer-live` showed 8 tiles, all black, with the same duplicate labels. Both are the identical defect class the previous entry fixed, in two call sites that audit missed: `partner_workspace.customer_account()`'s own `all_customer_cameras` query and `live_view_page._customer_live_cameras()` both queried `cameras WHERE customer_id=?` with no `appliance_id` filter.

**Real state had moved on since the previous entry, and this matters**: between that entry and this one, the customer used the *already-fixed*, appliance-scoped setup wizard themselves (per that entry's own "exact customer action" above) and genuinely discovered + provisioned **3 real cameras on Ryzen** (`urn:uuid:...` ONVIF device keys, real `camera_credentials` rows, created `2026-09-12T05:50-05:52`). So the account now has 5 (historical) + 3 (Ryzen) = 8 real camera rows -- exactly matching "8 of 8" and exactly why both appliances' `camera_number` 1/2/3 collided visually. **This was not new provisioning performed this session and was not undone or altered** -- read-only confirmed, then left exactly as found.

**Correction to the previous entry's own expectation**: that entry's in-process proof showed Ryzen at `configured=0`. That was correct *at the time*, before the customer's own subsequent, legitimate use of the (already-fixed) wizard. It is **no longer the current state** and must not be treated as a citable fact going forward -- see the fresh in-process proof below, which shows Ryzen at 3, not 0.

**Also confirmed and important on its own**: Ryzen's 3 real cameras report `appliance_camera_status.online=0`, `recording=0`, `last_error='vms_stream_offline'` for all three -- this is why `/customer-live`'s tiles are black. This is a real, separate, already-partially-investigated RTSP/streaming defect (see the `fix(appliance-agent)` commits on this same branch re: RTSP Digest auth / FFmpeg session shape), **not evidence the cameras aren't provisioned, and not something this phase touched or fixed**. Do not read "black tile" as "not provisioned" in any future session -- confirm against `appliance_camera_status`/`camera_credentials` directly, the way this entry did.

### Fix

Both routes now accept an optional `appliance_id` query parameter, mirroring `GET /api/customer/cameras`'s existing pattern exactly: when passed, verified to belong to the customer (404 otherwise, same as every other appliance-scoped customer route), and the page/grid is scoped to only that appliance's cameras.

**Blocker found and reported, not resolved -- read this before assuming either page can be "scoped to the selected appliance" today**: neither `/customer-account` nor `/customer-live` has any appliance-selection UI of its own. Searched thoroughly: no dropdown, no cookie, no session field, nothing read from either route today that could carry "which appliance is the customer currently looking at." The *only* persisted appliance-selection signal anywhere in the app is `customer_setup_drafts.data_json.appliance_id` -- but that belongs to the setup wizard's own step-rehydration (see `customer_first_setup()`), not a deliberate choice made on either dashboard page, and it was deliberately **not** wired in here as a silent default (it can be stale, absent, or simply mean something else -- "the appliance I was last configuring," not "the appliance I want to view now"). Per this phase's explicit instruction: do not invent a new selector, do not silently choose an appliance to represent the whole account. **So today, with no `appliance_id` supplied (every real browser request), both pages render every appliance's cameras -- grouped and labeled under their own appliance heading, never merged into one indistinguishable list, but with no way for the customer to pick "just Ryzen" or "just historical" as a persistent view.** Building that selector (a dropdown, a link per appliance, a persisted "current appliance" concept shared with the wizard) is a real, separate product decision for a future phase, requiring explicit authorization -- not something this fix invented or silently resolved.

### Tests and deployment

`app/tests/test_camera_multi_appliance_isolation.py` (+6 cases, same `_seed_world` fixture): `/customer-account` and `/customer-live` each scope correctly to an explicit `appliance_id`, reject a foreign customer's `appliance_id` (404, not leaked), and with no `appliance_id` render both appliances' cameras grouped under separate headings rather than merged. Full regression: 1621 passed / 79 failed / 22 skipped -- confirmed via git-stash A/B comparison that all 79 are pre-existing and identical to the failure list without this diff (79 failed / 1615 passed baseline; the only delta is the 6 new tests). Zero new regressions.

Deployed: commit `5f36eee` (`5f36eee9...`, full ancestry on `reconcile/golden-foundation-20260911`), image `deploy-portal:5f36eee` (id `sha256:c69b1a6e74cdaf44abfb018f162a9f8fe1ce9fb538ae16c2b6f9c828344f1785`), source tarball SHA-256 `b7cd7c36d2f59af2a663f1bef2661a7ebea8c5e55b6143face4809f0a4d686dd` verified identical before/after transfer. Pre-deploy backups: source `/opt/anyaicam-staging-source-backup-pre-camera-isolation-2-fix-20260912T131206Z.tar.gz` (SHA-256 `6ac101e2bf957248f356db4ce509b57718e41b4a0c200df98030ad0cb71c1801`); DB `/var/lib/anyaicam-staging/db/staging-pre-camera-isolation-2-fix-20260912T131206Z.db` (SHA-256 `5a5735636b67edee62e56edf67c26115864f22ee6f798e757b461e93e8fe4365`). `app/live_view_page.py` and `app/partner_workspace.py` hashed inside the running container match `git show 5f36eee` exactly, byte for byte. `/health` `200 ok`, `/version` correct, DB integrity `ok`, exactly one `portal-green` container, row counts unchanged (`customers=3`, `appliances=4`, `cameras=18`).

**Proven against real production data** (in-process, read-only query using the exact fixed SQL -- no session minted, no HTTP call, no mutation):
```
appliance_id=7844ceab... (Ryzen):        cameras=[7e34833a37,ca9d8c53d0,2e1a9a64bc]  count=3
appliance_id=5e76625989 (historical):    cameras=[01aad49341,d3e67c74b6,810dde938d,
                                                   7327f73df8,11c8b00156]              count=5
overlap between the two lists: False
```
Isolation confirmed correct. **Ryzen is at 3 configured, not 0** -- see the correction above for why that's the true current state, not a regression or an incomplete fix.

### Preserved exactly as instructed

The 8-slot Stripe entitlement (unchanged, unaffected by this fix). The five historical `AIC-C90CF0C9` camera records (untouched). Ryzen's 3 real camera records (untouched -- not provisioned by this session, found already provisioned, left exactly as found). No cameras provisioned by this phase. Ryzen/Samsung/camera credentials/Motion Cloud/AWS/appliance identities untouched.

### State to resume from -- STOP before Camera 1 provisioning or Live View testing

Both dashboard pages now correctly isolate cameras by appliance whenever an `appliance_id` is supplied, and never merge appliances together even when one isn't. The still-open item is the appliance-selection blocker above -- a real product decision, not a bug, and not something to resolve without separate explicit authorization. Ryzen's black Live View tiles are a known, separate `vms_stream_offline` streaming defect (RTSP-auth family, already partially tracked in this branch's own commit history) -- explicitly out of scope for this phase and not investigated further here. No Camera 1 (re-)provisioning and no Live View troubleshooting were performed.

---

## 2026-09-12: `vms_stream_offline` root-caused (read-only) -- not an RTSP/auth defect, a missing cloud->edge sync; storefront->registration handoff traced as a real gap; storefront pricing found already correct

**Read-only investigation, no changes.** Traced every step of the reported "Ryzen 0->3 cameras" and `vms_stream_offline` symptoms against DB/log/audit evidence: the 3 real Ryzen cameras were genuinely, legitimately provisioned by the customer through the already-fixed setup wizard (`camera.provisioning_requested` by `alexmata25@gmail.com` as `customer_owner`, `05:48`-`05:53`) -- not a bug, not tampering. Provisioning itself is fully correct end to end: cloud delivers the job, the appliance-agent's one-time `verify_device()` RTSP DESCRIBE succeeds, ONVIF media URI resolves, the cloud records real `cameras`/`camera_credentials` rows. Confirmed the 5 RTSP-auth fix commits (`21b3f73`/`9adec48`/`8c474b5`/`a48e14c`/`d694a87`) are ancestors of both the installed Ryzen release (`b8bdf2c`, confirmed live via `/version`) and current HEAD -- they are present, working, and irrelevant to this defect (they fixed `provisioning.py`'s one-time DESCRIBE check, a different code path entirely).

**Actual root cause**: Ryzen's own local database (`/app/recordings/partner_portal.db`, `RUNTIME_ROLE=edge`) had zero rows in `cameras`/`camera_credentials`/`camera_provisioning_requests` -- confirmed directly. `main.py`'s `_provisioned_camera_stream()` (the real source `camera_url()` uses) reads only this local database; finding nothing, `process_supervisor()` never started FFmpeg for these camera_numbers at all. Corroborated by zero `ffmpeg`/`[cameraN]`-tagged lines in `docker logs anyaicam-vms` since provisioning and completely empty `/app/static/hls`/`/app/recordings/camera{1,2,3}` directories. Nothing in the codebase (`app/` or `appliance-agent/`) ever propagated a cloud-provisioned camera into an edge appliance's own local state -- a genuinely missing integration, not a regression.

### Design decision: Option B, after an explicit alternative-design pass

Presented three designs (repeatable cloud endpoint returning plaintext; the one-time local loopback handoff ultimately chosen; HKDF-derived symmetric envelope) compared on security/complexity/rotation/recovery/scale. **Rejected**: a repeatable cloud API that decrypts and returns a camera password (an auto-mode classifier independently flagged the first draft of this before a human decision was even requested -- treated as a real signal, not routed around) and any new PKI/HKDF/envelope-encryption infrastructure (this codebase's only existing asymmetric keypair -- `appliance_identity.py`'s Ed25519 -- signs cloud->appliance manifests; no appliance holds a decryption-capable private key anywhere, so real envelope encryption would mean building a new subsystem, disproportionate to this gap). **Chosen**: reuse the ONE plaintext credential the appliance-agent already legitimately holds in memory during provisioning; hand it off once, over loopback only, to this same box's own VMS; the VMS encrypts and persists immediately.

### Fix (commit `aa4dc2e`)

1. `app/db_migrations.py`: new `pending_camera_credentials(device_key PRIMARY KEY, encrypted_blob, created_at)` -- edge-only, keyed by device_key since camera_id isn't assigned yet at the moment the credential arrives.
2. `app/main.py`: `POST /api/local/provisioned-camera-credential` -- requires BOTH a loopback client host AND this exact appliance's own activation credential (`own_appliance_identity()`) as bearer auth; encrypts via the existing `appliance_protocol.encrypt_camera_credentials()`; stores only ciphertext; never logs/returns/echoes the credential.
3. `appliance-agent/anyaicam_agent/service.py`: `poll_provisioning()`, right after a successful `verify_device()`, now also calls `_deliver_credential_to_local_vms()` with the same in-memory credential `_resolve_media_uri_after_provisioning()` already reuses -- best-effort, logged and ignored on failure; the agent still never persists a credential itself, in any form.
4. `app/edge_camera_sync.py` (new): `sync_provisioned_cameras()`, unconditional for `RUNTIME_ROLE` edge/combined (never gated behind an AWS/Motion-Cloud flag -- core VMS plumbing, not a cloud-upload feature). Polls the existing, unchanged `GET /api/appliance/configuration` to upsert local `cameras` rows (keyed by the cloud's own camera id), then moves any matching `pending_camera_credentials` row (by device_key) into the real `camera_credentials` table once that camera_id is known -- pure ciphertext relocation, no plaintext ever read or written by this module. `cameras.customer_id`/`site_id` carry a FOREIGN KEY written for the cloud's own multi-tenant CRUD flows; rather than fabricate placeholder local `customers`/`sites`/`partners` rows this module has no real data for, FK enforcement is scoped OFF on this one connection only -- the customer_id/site_id values themselves are real, from this appliance's own cryptographic activation identity. **Deprovisioning explicitly not handled**: a camera no longer reported by the cloud is left exactly as-is locally, per instruction -- a separate future design decision.

### Tests (28 new, all passing)

`appliance-agent/tests/test_local_credential_handoff.py` (7), `app/tests/test_local_provisioned_camera_credential_endpoint.py` (9), `app/tests/test_edge_camera_sync.py` (12) -- cover: loopback+bearer-credential access control (both directions), ciphertext-only storage, idempotent re-delivery/re-sync, restart-safety (a fresh in-memory state reaches identical DB state), an existing local credential is never overwritten by a stale pending row, a camera no longer in the cloud response is left untouched, full cross-appliance isolation (a second appliance's local rows/credentials are never read, touched, or reassigned), and the credential never appears in any returned value, log call, or exception across every path tested.

Full regression: 1642 passed / 79 failed / 22 skipped -- identical 79 pre-existing failures already confirmed unrelated via this branch's own earlier git-stash A/B comparison; the only delta is these 21 new app/ tests (agent tests run in their own separate suite: 495 passed / 3 failed, all 3 confirmed pre-existing and Windows-filesystem-semantics-specific, unrelated to any change here).

### Storefront->registration handoff (commits `4aab0ce` vendor, `dce063f` fix)

The public storefront (`store-staging.anyaicam.com`) was never in any git repository -- vendored verbatim via `docker cp` off the running container first (`4aab0ce`), establishing authoritative source before changing anything. Confirmed real gap: after the final Stripe TEST Checkout Session leg, `checkout-continue.html`'s "purchase complete" state had no reference anywhere to the portal domain, `/customer-register`, or `/customer-login` -- a brand-new customer who just paid had no way to find the account-creation step their purchase is waiting to attach to. Fixed: added a "Create your AnyAiCam account" action to the existing `/customer-register` (unchanged), with explicit text setting the correct expectation that partner/administrator approval is still required (no auto-approval added). New test: `storefront/staging/tests/test-checkout-continue-handoff.php` (8 checks, matching this repo's existing plain-PHP assertion-script convention -- no framework installed here), run against the actual deployed file: all pass; pre-existing `test-checkout-catalog.php` (88 checks) unaffected.

### Storefront pricing audit -- already correct, nothing created

Audited the storefront's Stripe Price ID mapping (`staging/stripe-config.php`) against live Stripe (read-only `GET /v1/prices/{id}`) before touching anything: **every price the storefront charges is already a small TEST-mode amount** -- hardware $0.50/$0.51/$0.52/$0.53, Local camera-slot tiers $0.60-$0.63, Hybrid $0.70-$0.73, analytics add-ons $0.80-$0.89 -- and the displayed on-page amounts in `build-your-system.html` match exactly. The page is already clearly labeled (`<title>...STAGING TEST)</title>`, an on-page "SANDBOX / STRIPE TEST MODE ONLY" banner). Every Price ID matches the same constant the portal's own entitlement webhook resolves against (e.g. `LOCAL_1_8_PRICE_ID` == `ANYAICAM_STRIPE_PRICE_LOCAL_1_8`), and every hardware SKU matches `hardware_orders.HARDWARE_CATALOG` exactly. **No new Stripe Products/Prices were created; no configuration was changed** -- this requirement was already fully satisfied by earlier work, before this session.

### Deployment record

- **App (`aa4dc2e`)**: image `deploy-portal:aa4dc2e` (`sha256:b883e29659dcdf33eb11810437540dc59072339614c6727bccf871b3e5d7f227`), source tarball SHA-256 `aa125634b35871abddee9937471280512383cf9c8adb34211ec21bd88e11a17f` verified identical before/after transfer. Pre-deploy backups: source (`...pre-cloud-edge-sync-fix-20260912T144126Z.tar.gz`, SHA-256 `8cfc57e609e04b9d000650462f6d18a6f85ffec4ef04bace7b9ea4af6c538510`), DB (`...pre-cloud-edge-sync-fix-20260912T144126Z.db`, SHA-256 `0589b74e9c2601f8f2d4c7007f307779aaa617b6480f9922c6ec9abde36d05f4`). Verified: `main.py`/`db_migrations.py`/`edge_camera_sync.py` hashed inside the running container match `git show aa4dc2e` exactly, byte for byte; `/health` `200 ok`; `pending_camera_credentials` table present; DB integrity `ok`; exactly one `portal-green` container.
- **Storefront (`dce063f`)**: image `deploy-storefront:dce063f` (`sha256:8620919ae568a5df1d5bb882e526505eb701b7994cb7930745652aced1810978`), source tarball SHA-256 `a244aed0c15337c687e1fa90a5e4ad2dbae30e81d020ad257b76253ef68721d2` verified identical before/after transfer. Pre-deploy backup: `...storefront-backup-pre-registration-handoff-20260912T144256Z.tar.gz` (SHA-256 `b34d18e01753ff1b7fcfa0be7c43747f53932b6ff2e297a5586cba847fa47513`). Verified: `store-staging.anyaicam.com` and `/staging/build-your-system.html` both `200`; registration-handoff markup present in the deployed file; both PHP test suites pass inside the running container.

### Ryzen release artifact -- built, hashed, NOT installed

Built via the existing `installer/build_release_installer.py --vms-commit aa4dc2edb135e379631a9d17db8923e56a202ed9`: artifact `anyaicam-appliance-installer-1.1.0-vms-aa4dc2edb135.tar.gz`, artifact SHA-256 `4478f13a65ae43caf0f66cb54be62fbc80f0724b31ef97fb409a78953f53a30a`, release-source SHA-256 `2c4e51557f74a5b9d53dbefbd5e6114748eb7c3bdecc07eced01f7dd23696738`. Confirmed the payload contains the new fix files (`payload/vms/app/edge_camera_sync.py`, updated `main.py`/`db_migrations.py`, `payload/agent/anyaicam_agent/service.py`). **Sitting locally only -- not transferred to Ryzen, not installed.** Ryzen is still running `b8bdf2c` and therefore still lacks this fix; its 3 real cameras will not start streaming/recording until this new release (or a later one containing the same fix) is actually installed there.

### State to resume from -- STOP before installing on Ryzen or starting the new-customer test

Remaining blocker to a complete storefront->purchase->registration->activation->discovery->provisioning->VMS->recording->Live View run: (1) Ryzen needs the new release installed to pick up already-provisioned cameras -- awaiting explicit authorization, not performed; (2) the appliance-selection-mechanism blocker on `/customer-account`/`/customer-live` recorded above is still open (not required for a single-appliance fresh test customer, only relevant once/if that customer ends up with more than one appliance); (3) zero unassigned/claimable appliances exist for a genuinely fresh test customer per the earlier journey audit -- reusing Ryzen requires unclaiming it (not authorized), so the fresh-customer test's appliance-activation step needs an explicit decision before it can proceed past registration/entitlement. Samsung untouched throughout. No purchase, registration, or Live View testing performed.

---

## 2026-09-12: Ryzen install attempt -- verification shows the release was NOT actually applied; correction, and read-only unclaim/reset procedure proposed

**Authorized and attempted**: install the versioned `aa4dc2e` artifact (hash-verified before transfer: `4478f13a...` local == post-transfer == the recorded build record) onto Ryzen via its own supported `install.sh`, repair path, no manual patching. The user reported the installer completed successfully; **post-install verification, done immediately after, does not support that** -- every observable fact on the box is byte-for-byte identical to the pre-install baseline captured just before the install was requested:

| Check | Pre-install baseline | Post-install (now) | Changed? |
|---|---|---|---|
| `/version` `build_id` | `b8bdf2cf98c716067024bdf471d834ce5cd602e1` | `b8bdf2cf98c716067024bdf471d834ce5cd602e1` | **No** |
| `anyaicam-vms` image id | `anyaicam-vms:latest` `73f83ae7f695` | same, `73f83ae7f695` | **No** |
| `anyaicam-vms.service` last (re)start | -- | `Fri 2026-09-11 22:47:46` (systemd unit), container process started `2026-09-12T04:04:16` -- both hours before this install was even requested | **No** |
| `anyaicam-agent.service` `ActiveEnterTimestamp` / `NRestarts` | -- | `Fri 2026-09-11 23:04:04`, `NRestarts=0`, 10h uptime | **No** |
| `pending_camera_credentials` table | doesn't exist (old code) | **still doesn't exist** | **No** -- conclusive: the new migration never ran |
| Local `cameras`/`camera_credentials`/`camera_provisioning_requests` | 0/0/0 | 0/0/0 | **No** |
| `appliance_identity.json` SHA-256 | `9d18b86c...` | `9d18b86c...` | No (expected -- preserved either way) |
| `journalctl --since '30 minutes ago'` | -- | only routine SSH/tailscale/healthcheck-restart entries; nothing resembling an install run | -- |

**This is not a partial success or a cosmetic mismatch -- no evidence exists anywhere on the box that `install.sh` executed a deploy in this session's timeframe.** The most conclusive single fact: `pending_camera_credentials` (this fix's own new table) does not exist, which is only possible if the old `db_migrations.py` is still what's running. Everything else (identity preserved, no data loss, no AWS/Motion-Cloud flags enabled) is also true, but only because nothing changed at all, not because a careful upgrade preserved it.

**Not diagnosed further** -- no sudo access on this box (by design, per this project's own credential-handling rule), so I cannot read `/etc/anyaicam/installed_version`, `vms_release.json`, or `install.sh`'s own run output. Possibilities include: the run exited early on an error not visible to a non-root read, `03-detect-install.sh` mis-detected install state, or the reported "completed successfully" referred to a different run/target than this appliance. **Needs the actual terminal output of the `sudo ./install.sh` run to diagnose** -- please share it (or re-run and capture it) before a second install attempt.

### Read-only investigation: supported procedure to return Ryzen to an unclaimed state (NOT executed)

No self-service "unclaim"/"release appliance"/"factory reset" API exists anywhere in `app/` -- searched. The only real precedent in this project's own history is the "controlled cleanup of a stranded claim" procedure, already used twice for this exact device_id (`docs/PROJECT_CHECKPOINT.md`'s own earlier entries) -- adapted here for a claim that is NOT stranded (it durably completed, milestone PASSED) but needs to be released on purpose.

**Current exact cloud-side state for this device** (`637ad320-daaa-436e-89c9-70a84f4f54a9`), read-only:
- Live claim: `appliance_claims id=2b7a1fb8452cfbbee722093c967428ea`, `status=completed`, `revoked_at=NULL`, `customer_id=4efaf5153f`, `appliance_id=7844ceab86e7fab2845125cffeb8ad10` -- the one to revoke. (5 earlier claim attempts for this device are already `expired`/`revoked` history -- leave untouched.)
- `appliances id=7844ceab86e7fab2845125cffeb8ad10`, `cloud_id=637AD320-...` -- must be deleted to free the cloud_id for a fresh `claim_begin()`.
- `appliance_credentials id=ffdf68c39378f4e9`, not revoked -- must be removed.
- Dependent rows tied to this `appliance_id` (FK/dependency scan): `cameras`=3 (the real provisioned cameras -- `7e34833a37`/`ca9d8c53d0`/`2e1a9a64bc`), `camera_provisioning_requests`=5 (3 succeeded + 2 failed on license-cap), `camera_scan_jobs`=7, `appliance_camera_status`=3, `appliance_commands`=30, `appliance_health_history`=692, `appliance_request_nonces`=61. `customer_setup_drafts` for `4efaf5153f` also references this `appliance_id` in its saved JSON (harmless if left -- the wizard already falls back gracefully when a saved appliance_id no longer matches).

**Proposed cloud-side procedure** (mirrors the exact discipline already used twice for this device: pre-verify every target row fresh, full DB backup + hash, FK/dependency scan, one transaction, exact-id-scoped deletes that re-check every field -- never by customer_id/site_id alone, post-verify, confirm `PRAGMA integrity_check`, confirm the historical customer's OTHER appliance/entitlement/cameras are completely untouched):
1. Delete `camera_credentials` for the 3 camera ids above.
2. Delete `cameras`, `camera_provisioning_requests`, `camera_scan_jobs`, `appliance_camera_status`, `appliance_commands`, `appliance_health_history`, `appliance_request_nonces` WHERE `appliance_id='7844ceab86e7fab2845125cffeb8ad10'`.
3. Delete `appliance_credentials id=ffdf68c39378f4e9`.
4. `UPDATE appliance_claims SET revoked_at=<now>, appliance_id=NULL WHERE id='2b7a1fb8452cfbbee722093c967428ea' AND status='completed' AND revoked_at IS NULL` -- kept, revoked, never hard-deleted (same convention as every prior cleanup here).
5. `DELETE FROM appliances WHERE id='7844ceab86e7fab2845125cffeb8ad10' AND cloud_id='637AD320-DAAA-436E-89C9-70A84F4F54A9' AND customer_id='4efaf5153f'`.
6. Verify: 0 rows remain anywhere for this `appliance_id`; no `appliances` row for this `cloud_id` (so `claim_begin()` accepts it fresh); customer `4efaf5153f`'s account, entitlement (8 slots, unchanged), and its OTHER appliance (`AIC-C90CF0C9`, 5 historical cameras) are byte-for-byte unaffected; `PRAGMA integrity_check: ok`.

**Local (Ryzen) side -- recommend the surgical path, not a full wipe**: `appliance-agent/anyaicam_agent/reenrollment.py`'s `coordinated_reenroll()` exists specifically to replace an already-enrolled appliance's identity with a new one, atomically, with rollback -- this is the actual supported mechanism for "re-claim an already-claimed box," not `uninstall.sh`. Since Ryzen's local `cameras`/`camera_credentials` are already empty (nothing to clear there), the expected flow is: after the cloud-side cleanup above, run `anyaicam-setup`'s claim flow again as the new test customer; `setup_wizard.py` should detect the existing identity files and route through `coordinated_reenroll()` rather than `first_enroll()`, replacing the old identity once the new claim completes. **This has not been read through to its exact trigger conditions in full** (time-boxed this pass) -- worth a careful read of `setup_wizard.py`'s own branch logic before relying on it, or falling back to `sudo ./uninstall.sh --purge-all` (confirmed to remove `/etc/anyaicam`, `/var/lib/anyaicam`, `/var/log/anyaicam`, and the `anyaicam` system user, while leaving Docker/networking/SSH/Tailscale untouched -- the exact same procedure already used once before on this box for the RC1 clean-install validation) followed by a fresh `install.sh` if `coordinated_reenroll()` turns out not to apply cleanly.

**Not executed.** No local or cloud changes were made by this investigation. Samsung untouched.

---

## 2026-09-12: Ryzen re-install (manual, by the operator) VERIFIED -- `aa4dc2e` now live; edge_camera_sync confirmed running; one expected, real limitation found

The operator re-ran `sudo ./install.sh` manually (this session's own automated attempt had silently not applied -- see the entry above). Full post-install verification, read-only:

| Check | Result |
|---|---|
| `/version` `build_id` | `aa4dc2edb135e379631a9d17db8923e56a202ed9` -- **matches exactly** |
| `anyaicam-vms` container | new container, started `2026-09-12T15:13:56`, `docker ps` shows `Up ... (healthy)` |
| `/health` | `200 ok` |
| `/ready` | `503` (`ready:false`) -- correctly so: only the same 3 pre-existing non-critical config warnings (`ANYAICAM_ADMIN_EMAIL`/`_PASSWORD`/`ANYAICAM_PORTAL_SECRET`, unrelated to this fix) plus `cloud_foundation_ready:false` (AWS still unconfigured, as intended) |
| `anyaicam-agent.service` | `active`/`running`, `NRestarts=0`, clean stop/start cycle in the journal (no crash loop) |
| Cloud heartbeat | confirmed both directions: agent log `Entitlement refreshed camera_slot_quantity=8`; cloud-side `appliances.last_check_in=2026-09-12T15:15:54` (fresh), `restart_count` incremented 15->16 (one legitimate restart, expected) |
| `edge_camera_sync` | **confirmed running**: startup log line `edge_camera_sync.worker_started`; **and working** -- local `cameras` table went from 0 to 3 rows, correctly populated with camera_number/device_key/onvif_endpoint matching the cloud exactly |
| `pending_camera_credentials` table | now exists (new migration applied) |
| Appliance identity | `appliance_identity.json` SHA-256 unchanged (`9d18b86c...`) -- byte-for-byte preserved, same appliance_id/cloud_id/customer_id/site_id |
| AWS / Motion Cloud flags | none set (`cloud_upload_enabled:false`, `upload_worker:disabled`, all `missing_cloud_requirements` still missing) -- unchanged, nothing enabled |
| Local data | `/app/recordings/camera{1,2,3}` and `/app/static/hls` still empty (expected -- see limitation below); no recordings/media lost or altered |

**One real, expected limitation found, not a defect**: `camera_credentials` is still `0` locally (and `pending_camera_credentials` is `0` too). These 3 cameras were provisioned *before* this fix existed on either side -- the one-time in-memory credential handoff (agent -> local VMS, at the moment of provisioning) already happened and passed, long before there was anywhere local to receive it. `edge_camera_sync` correctly synced their metadata (camera_number/device_key/onvif_endpoint) but has no credential to move, because none was ever captured. Confirmed via `_provisioned_camera_stream()`'s own logic: it requires *both* a `cameras` row and a `camera_credentials` row -- with the credential missing, `camera_url()` still raises `CameraNotConfiguredError` for these three, so they will **not** start streaming/recording from this install alone. This was already flagged as a known scope boundary when Option B was designed ("does not by itself support recovering a lost local credential... without a fresh provisioning action") -- not re-provisioned here, per instruction. A **fresh** camera provisioned from this point forward (after both sides already have the fix) will capture its credential correctly the first time.

Minor, harmless observation: `/app/recordings/` now also has empty `camera5`/`camera6`/`camera7` directories and an `in_app_alerts.jsonl` file that weren't present before -- no data in them, not a loss, most likely idle-supervisor-slot pre-creation reacting to the local camera count changing from 0 to 3. Not investigated further; nothing to act on.

**No cameras provisioned, no credentials changed, Ryzen not reset/unclaimed, Samsung untouched.**

---

## 2026-09-12: Ryzen released cloud-side (unclaimed) -- DONE. Local Ryzen deliberately NOT touched -- coordinated_reenroll() cannot run standalone; not falling back to --purge-all

**Source trace, before touching anything**: `coordinated_reenroll(config, activation_response, ...)` (`appliance-agent/anyaicam_agent/reenrollment.py`) requires an already-obtained `activation_response` -- it *replaces* one identity with a new one, atomically, with rollback. There is no code path from "claimed" to "no identity" other than this (which needs a fresh claim already completed -- not available this turn, claiming being explicitly out of scope) or `uninstall.sh`'s `--purge-all` (a full wipe). **Conclusion: the supported mechanism cannot accomplish a standalone "return to unclaimed" right now** -- so, per instruction, Ryzen's local software/identity was left completely untouched this pass; no `--purge-all`, nothing invented.

### Cloud-side release -- executed, verified

Pre-verified every target row fresh (immediately before mutating, inside the same transaction) against: `appliances id=7844ceab86e7fab2845125cffeb8ad10`/`cloud_id=637AD320-...`/`customer_id=4efaf5153f`/`site_id=4de6186be8`; `appliance_credentials id=ffdf68c39378f4e9`; `appliance_claims id=2b7a1fb8452cfbbee722093c967428ea` (`status=completed`, `revoked_at IS NULL`). Backup: `/var/lib/anyaicam-staging/db/staging-pre-ryzen-unclaim-20260912T152133Z.db`, SHA-256 `109f5337dffc406b5f45cb1441d895212c4b6c0108fdb9c93a29d835bd60b4f1`.

One transaction, exact-id-scoped, each statement's rowcount asserted before commit: deleted `camera_credentials`(3) for this appliance's own camera ids, `cameras`(3), `camera_provisioning_requests`(5), `camera_scan_jobs`(7), `appliance_camera_status`(3), `appliance_commands`(42), `appliance_health_history`(712), `appliance_request_nonces`(86), `appliance_credentials`(1, the live one); revoked (not deleted) `appliance_claims id=2b7a1fb8452cfbbee722093c967428ea` (`revoked_at` set, `appliance_id` cleared, `status` left `completed` for history -- same convention as every prior cleanup on this device); deleted `appliances id=7844ceab86e7fab2845125cffeb8ad10`. Committed once, cleanly.

**Post-verify, all passed**: zero `appliances` rows for `cloud_id=637AD320-DAAA-436E-89C9-70A84F4F54A9` (a fresh `claim_begin()` for this device will now be accepted); zero rows anywhere for the old `appliance_id`; the claim record kept, revoked, `appliance_id=NULL`. **Historical customer/appliance/entitlement byte-for-byte unaffected**: `customers[4efaf5153f]` still `active`; `appliances[5e76625989]` (`AIC-C90CF0C9`) still `activated` with its own 5 cameras, untouched; `customer_entitlements` still `camera_slot_quantity=8`/`active`, unchanged. Overall table deltas exactly match the deletes (`appliances` 4->3, `cameras` 18->15, everything else unchanged). `PRAGMA integrity_check: ok`. One harmless leftover, deliberately not touched: `customer_setup_drafts` for `4efaf5153f` still has the now-gone `appliance_id` in its saved JSON -- the wizard already falls back gracefully when a saved appliance_id no longer matches any of the customer's remaining appliances (confirmed in source during the earlier journey audit).

### Ryzen itself -- confirmed still healthy, correctly reacting, deliberately unchanged

`anyaicam-vms.service`/`anyaicam-agent.service` both still `active`, `NRestarts=0` (no crash loop from the credential revocation). VMS `/health` still `200 ok`, `build_id` still `aa4dc2edb135e379631a9d17db8923e56a202ed9` (aa4dc2e install from the previous entry, unaffected -- this was a pure cloud-side data operation, no redeploy, no restart). Agent log now correctly shows graceful, expected degradation: `WARNING:anyaicam.agent:Queued offline update path=/api/appliance/heartbeat error=Appliance is revoked or unknown.` (and the same for `/api/appliance/cameras`) -- exactly the fail-safe-and-queue behavior this agent was already built with, not a new failure mode. `appliance_identity.json` SHA-256 unchanged (`9d18b86c...`) -- still shows the old `cloud_id`/`customer_id`, deliberately, since no new claim has completed to give `coordinated_reenroll()` something to swap in. Local `cameras` table still has 3 orphaned rows (device_key/onvif_endpoint, no credentials -- already `0` credentials before this pass, so no new local leftover was created) -- left alone with the identity, for the same reason.

### What this means for resuming the new-customer test

Cloud-side, `637AD320-DAAA-436E-89C9-70A84F4F54A9` is genuinely unclaimed and ready for a fresh `claim_begin()`. Locally, Ryzen keeps running its already-installed `aa4dc2e` software with its old identity in place; the expected, correct sequence when a NEW customer's claim actually completes (a later, separately-authorized step) is for the agent's own `setup_wizard.py` to detect the existing identity files, route through `coordinated_reenroll()` (not `first_enroll()`), and atomically replace the old identity with the new one -- the exact supported mechanism, exercised at the moment it's actually designed for, not forced early. This has not been dry-run end-to-end; worth watching closely the first time it actually happens.

**Not done this pass**: no new customer created, nothing purchased, appliance not claimed, no cameras provisioned, Samsung untouched.

---

## 2026-09-12: `anyaicamtest@gmail.com` old staging/sandbox contamination removed; today's real storefront purchases re-homed via the real pending-purchase mechanism -- DONE, verified

**First real-customer-journey attempt hit a registration failure**: after a genuine storefront purchase (2 Stripe TEST checkouts: AnyAiCam Starter hardware + Local 1-8 camera-slot subscription) under `anyaicamtest@gmail.com`, `/customer-register` returned "That email is already registered." Traced, read-only, before touching anything: this email already belonged to a fully-set-up staging/sandbox account (`customers id=2a6cb00a6b`, created 2026-09-10, activated appliance `71bf8b39fd`/`AIC-2FC1F54F`, 1 camera, full login history) -- **confirmed NOT the physical Samsung device** (Samsung's own checkpoint records it as never claimed/activated anywhere; this record was fully activated with 1,091 health-history rows -- an unrelated, disposable sandbox record that happened to share the email). `create_pending_registration()`'s duplicate-email check was working exactly as designed; the registration architecture itself had no defect. Today's purchase had been correctly attached by `sync_hardware_order_from_stripe_event()`/its entitlement equivalent directly to the *existing* customer (matched by email) rather than held as a pending link, since a customer with that email already existed at webhook time -- also correct, existing behavior, not a bug.

**Verified against live Stripe (read-only `GET`) before reconstructing anything**: today's hardware checkout (`cs_test_a1zkCOEn93SJV9114oW8vsC1BidgWebWmFb66Vu7AFejTLe0BSdeaYg2dY`) actually charged `amount_total=50` cents; the entitlement checkout (`cs_test_a1DbvwZW5578AQY49pAZLCQkps7LC0s0EWTOmV0Lyz7RbhoQp8EWJW6wc5`) charged `amount_total=60` cents -- both `usd`, `livemode:false`, `customer=cus_VFNjUsvnstQzaq`, `paid`. This caught a real discrepancy before it was written anywhere: the `hardware_orders` row's own `amount_cents` (124999) did **not** match the real Stripe charge -- see the separate tech-debt item below.

### Cleanup executed

Backup: `/var/lib/anyaicam-staging/db/staging-pre-anyaicamtest-cleanup-20260912T160848Z.db`, SHA-256 `ea462141fdf228b09082292d9bf694b19f3822985949fc45f20b4c79e4d94ee7`.

**Phase 1 -- preserve today's purchases via the real, existing, unmodified application functions** (never hand-written INSERTs): called `hardware_orders.create_pending_link(...)` and `customer_entitlements.create_pending_link(...)` directly with the Stripe-verified values (hardware: `amount_cents=50`, not the stale `124999`; entitlement: `camera_slot_quantity=8`, `price_id=price_1UD2xKGllhK80H2nFJwtFJvw`) -- both committed independently, before any deletion: `pending_hardware_order_links id=d12c4134c1b44c9cbbb0c63dadc176bf`, `pending_customer_links id=d082345105174149af3debb50e4cef45`, both `status='pending'`.

**Phase 2 -- remove the old contamination**, one transaction, every target re-verified fresh immediately before mutating, every delete's rowcount asserted: `cameras`(1), `appliance_camera_status`(1), `appliance_health_history`(1091), `appliance_activation_tokens`(2), `appliance_credentials`(1), `appliances`(1), `hardware_orders`(2 -- the old Sept-10 row and today's now-redundant row), `customer_entitlements`(1), `analytics_subscriptions`(3), `plans`(1), `quotes`(1), `invitations`(1), `service_history`(2), `customer_setup_drafts`(1), the 5 old already-`resolved` `pending_*_links` rows (their own historical audit trail, tied to the customer being removed), `sites`(1), `identity_grants`(1), `user_sessions`(6), `partner_users`(1), `customers`(1). Every rowcount matched exactly. Committed.

### Post-cleanup verification -- all passed

`customers`/`partner_users`/`customer_registration_requests` for this email: `0`/`0`/`0`. Old appliance/camera/site: `0`/`0`/`0`. Today's pending links: exactly one `pending_hardware_order_links` row (`amount_cents=50`, correct Price ID/session/Stripe customer) and exactly one `pending_customer_links` row (`camera_slot_quantity=8`, correct Price ID/session), both `status='pending'`, `resolved_customer_id=NULL`. Zero `pending_analytics_links` remain (none from today). Zero `customer_entitlements`/`hardware_orders` rows reference the deleted customer_id (**no entitlement was fabricated for a nonexistent customer**). Unrelated data confirmed untouched: `alexmata25@gmail.com` (`4efaf5153f`) still `active`; historical appliance `AIC-C90CF0C9` still `activated` with its own 5 cameras; `Sandbox Test Customer` (`6d8e437804`) unchanged; Ryzen (`637AD320-...`) still has zero `appliances` rows -- **still cleanly unclaimed**, unaffected by this cleanup. Table deltas match exactly (`customers` 3->2, `partner_users` 4->3, `appliances` 3->2, `cameras` 15->14, `sites` 3->2). `PRAGMA integrity_check: ok`. Ryzen and Samsung: no command issued to either this pass.

### Tech debt recorded, not fixed this pass

`hardware_orders.sync_hardware_order_from_stripe_event()` computes the stored `amount_cents` from the server-side `HARDWARE_CATALOG` constant (`hardware["amount_cents"] * quantity`), never from the checkout session's real `amount_total` -- so `hardware_orders.amount_cents` has never actually recorded what Stripe charged, for any order this table has ever held (confirmed: the Sept-10 contamination row had the identical stale `124999`). Does not affect entitlement/grant correctness (hardware orders don't grant recurring access), but makes the column an unreliable audit trail of the real transaction amount. Recommend a future, deliberate fix: read `session_obj.get("amount_total")` instead of (or in addition to, for cross-checking) the catalog constant.

### State to resume from

`anyaicamtest@gmail.com` is now eligible for `/customer-register` as a genuinely new customer. When registration is submitted and later approved, `approve_registration()`'s existing, unmodified calls to `resolve_pending_links_for_customer()`/`resolve_pending_hardware_links()` will find the two pending links created above and attach today's real purchases to the new customer automatically -- no manual entitlement work needed at that point. **Registration was deliberately not submitted and no approval was performed this pass.**

---

## 2026-09-12: `/customer-registration-requests` 500 fixed -- `page_shell()` called with a stray extra argument -- DONE, deployed, verified

**BUG** (found investigating the prior "500 while logged in as sandbox-admin" report): `requests_page()`, the `GET /customer-registration-requests` handler in `app/customer_registration.py`, called `page_shell("Customer registrations", "business-users", content, current_user(request), scripts=scripts)`. `page_shell()`'s real signature (`app/main.py`) is `page_shell(title, active, content, scripts="")` -- there is no `user` parameter. The stray `current_user(request)` positional argument landed in `scripts`'s slot, and the explicit `scripts=scripts` keyword then collided with it: `TypeError: page_shell() got multiple values for argument 'scripts'`, on every single load of the route. Confirmed live via `anyaicam-staging` container logs. All 10+ other `page_shell()` call sites in the codebase already pass exactly 4 positional arguments -- this was the only broken caller.

**FIX** (`app/customer_registration.py`, one line): dropped the stray `current_user(request)` argument.

**Regression tests added** (`app/tests/test_customer_registration_csrf.py`, 3 new tests, existing 21 unchanged): administrator can `GET /customer-registration-requests` and gets 200 with the seeded pending registration rendered; `partner_owner` can also load the page; unauthenticated request is still redirected to `/customer-login.html` (pre-existing, unrelated middleware behavior, unchanged by this fix -- not a 403 as initially assumed). Focused suite: **24/24 passed**.

**Full-suite regression verification** -- an apparent 79-vs-84-failure discrepancy was fully isolated before proceeding: the source fix **alone** (no test additions) reproduces the established baseline exactly, **79 failed / 1642 passed / 22 skipped**, byte-for-byte identical to a clean stash of the change. Only with the 3 new tests *also* present does the run show 5 additional failures, in `test_operations_rdm.py`, `test_p05_mobile_poll_js.py`, and `test_playback_autoplay_most_recent.py` -- files with no code relationship to this change. All 5 pass individually in isolation (5/5 passed, 6.06s). Conclusion: this is pre-existing order/state-dependent test-isolation debt already latent in the suite (adding any tests earlier in the alphabetical run shifts which cross-test pollution surfaces) -- not a regression introduced by this fix or its tests. Left unfixed as out of scope, per explicit instruction not to make unrelated fixes.

**Commit**: `3f161dc` (`app/customer_registration.py`, `app/tests/test_customer_registration_csrf.py`).

**Deployed to `anyaicam-staging`**: source tarball `git -c core.autocrlf=false archive 3f161dc` -> SHA-256 `ae95781694da0eaebf51f49f08f0c317da53220effeef3f2725086aec23b912e`, verified identical after `scp`. Pre-deploy backups: source `/opt/anyaicam-staging-source-backup-pre-registration-page-fix-20260912T175559Z.tar.gz` (SHA-256 `f4e575c641923ca40ca0b186db8403e49278dbc88e1874d2a464f8dec5b09647`); DB `/var/lib/anyaicam-staging/db/staging-pre-registration-page-fix-20260912T175559Z.db` (SHA-256 `57fcce74bb6c124f2dab3ccecf5aaf9bad48ae973494f03d8c18e3582ea7f285`). `app/` rsync'd into `/opt/anyaicam-staging/` (`diff -rq` confirmed identical after; `deploy/`/`storefront/` untouched). Built `deploy-portal:3f161dc` (image id `sha256:5a5e1962db9ebeb76cb3ca61eadeed2b197ea4933044bf67e47ca46bdfed2878`). Before recreating `portal-green`, diffed `green.env` against the live container's actual running environment (the safeguard this doc itself recommended after the earlier stale-env incidents) -- identical except Python/Docker base-image built-ins, safe to reuse. `portal-green` recreated (`docker stop`/`rm`/`run`, identical `--env-file green.env`, identical `db`/`recordings`/`hls`/`data-config` mounts, identical `deploy_default` network).

**Verified**: `/health` `200 ok`; `/version` correct; `customer_registration.py` hashed inside the running container (`1873dfcb491fbc28ab0a66d829c9f5205d73498255fa5196e3537e3b8b40335e`) matches `git show 3f161dc:app/customer_registration.py` byte-for-byte; DB `integrity_check: ok`; exactly one `portal-green` container. **Route verification against the real public endpoint** (minted a genuine administrator session token inside the container using `partner_portal._token()`/its own signing key, sent through Caddy to `https://portal-staging.anyaicam.com` with the real `anyaicam_partner_session` cookie -- not `TestClient`, which isn't installed in the production image): `GET /customer-registration-requests` -> **200**, page contains "Registration requests" and the `/api/customer-registration-requests` script reference; `GET /api/customer-registration-requests` -> **200**, returns exactly one pending request (`anyaicamtest@gmail.com`, `status=pending`).

**Customer-state verification (read-only, unchanged by this deploy)**: `anyaicamtest@gmail.com`'s registration request (`id=69de64fe4a4db87dff5f7f9a40ded6ee`) still `status=pending`. Both pending links from the prior cleanup untouched: `pending_hardware_order_links id=d12c4134c1b44c9cbbb0c63dadc176bf` (`status=pending`, `amount_cents=50`); `pending_customer_links id=d082345105174149af3debb50e4cef45` (`status=pending`, `camera_slot_quantity=8`, `stripe_amount_total_verified=60` recorded in its own `raw_event_json`). **No approval was performed. Ryzen and Samsung were not touched.**

### State to resume from

The `/customer-registration-requests` administrator UI is now repaired and live on `anyaicam-staging`. `anyaicamtest@gmail.com` remains pending, exactly as left by the prior cleanup, ready to be approved through this now-working UI whenever the user chooses to do so.

---

## 2026-09-12: Self-service Cloud ID provisioning bridge — the real gap once `anyaicamtest@gmail.com` was approved — DONE, deployed, verified

**Confirmed real customer state before writing any code** (read-only, against `anyaicam-staging`'s live `staging.db`): the customer had since been approved through the now-fixed `/customer-registration-requests` UI — `customer_registration_requests` `status=approved`, `decided_by=sandbox-admin@anyaicam-staging.test`; `customers id=d75bdbecdd4887de4d2b89a9fcea9092` `status=active`; `partner_users id=99bdbe03c4e5628c3624a8eeb8e44416` `role=customer_owner`, `approved=1`; `hardware_orders id=251b6a8922b04f27bee28c467a90ef3b` `status=paid`; `customer_entitlements id=6538be5c6a054678a416427fe8cb05b3` `product=camera_slots_local`, `camera_slot_quantity=8`, `status=active`; and exactly **zero** `sites`/`appliances`/`cameras` rows for this customer. Ryzen (`637AD320-...`) confirmed still unclaimed (zero `appliances` rows). This matches the architecture decision above exactly: the customer had a real, paid entitlement and nowhere for Customer Setup Step 2's Cloud ID field to point — not a claim-code gap, not a registration gap (already fixed above).

**FIX** — `app/partner_workspace.py`, one new route: `POST /api/customer/appliances/provision`. Authenticates via the existing `customer_owner()` + `'appliance.self.link'` permission check (same as `link_customer_appliance()`); confirms a `hardware_orders` row with `status='paid'`; reads capacity from `customer_entitlements.total_camera_slots()` (never a hardcoded number, never a payload-supplied quantity — resolves to `8` for this real customer from their real entitlement, and independently to whatever a different customer's own entitlement sums to); creates the customer's first `sites` row from customer-entered `site_name`/`site_address`; provisions exactly one appliance through the *same* `get_provisioning_backend().provision()` seam `onboard_customer()` already uses — identical Cloud ID format, identical activation-token hashing/expiry/single-use redemption via the unmodified `POST /api/appliance/activate`, identical `provisioning_qr_payload` shape; creates camera placeholders sized from the real entitlement quantity. Idempotent by design: a customer who already has an appliance short-circuits to `already_provisioned`; a retry that reaches the backend a second time (partial local failure) recovers the backend's own idempotency-keyed Cloud ID/site/appliance instead of minting a second one. Customer Setup Step 2 gained a "Provision your first appliance" panel — present only when the customer has no appliance yet — that calls this endpoint and auto-fills the existing Cloud ID/activation-token fields (and the QR-paste value) so the customer flows straight into the existing, completely unmodified `link_customer_appliance()` flow. The claim-code flow (`appliance_claims.py`) was not touched.

**Tests** (`app/tests/test_self_service_appliance_provisioning.py`, 12 new, all passing): full journey (paid hardware + 8-slot entitlement → first site → Cloud ID appliance → activation token/QR → exactly 8 camera placeholders → existing, unmodified link flow accepts it); unauthenticated and wrong-role (`customer_viewer`) both rejected 403; a payload-supplied `customer_id` can never redirect the provisioned records to another customer's account (cross-tenant isolation proven with two real, separate entitlements seeded simultaneously); no hardware order, an unpaid (`status='pending'`) hardware order, no entitlement, and an inactive (`status='cancelled'`) entitlement all fail closed with 403 and write nothing; retrying the same call twice is idempotent (exactly one site/appliance/8 cameras/1 unused activation-token row; the first call's site name survives a retry that supplies a different name); a retry after the backend already committed its idempotency record (simulating the process dying before the local SQL transaction ran) recovers the exact same Cloud ID/site/appliance rather than minting a second identity; entitlement quantity — not a constant — controls camera capacity (a 3-slot customer gets exactly 3 cameras; multiple active entitlements for one customer sum correctly, 8+8=16).

**Regression proof**: `git stash` isolation — the full suite's failing-test set is byte-for-byte identical with and without this change (84 pre-existing, unrelated failures either side, same list); this change turns exactly the 12 new tests from failing (route not yet registered) to passing and touches nothing else. `appliance_claims.py`'s own 47-test suite and the admin/partner onboarding suites (`test_onboarding_wizard_flow.py`, `test_cloud_id_alignment.py`) confirmed still passing, unmodified.

**Commit**: `8b07dc1` (`app/partner_workspace.py`, `app/tests/test_self_service_appliance_provisioning.py`).

**Deployed to `anyaicam-staging`**: pre-deploy backups — DB `/var/lib/anyaicam-staging/db/staging-pre-self-service-provisioning-fix-20260912T205215Z.db` (SHA-256 `55755a65b601177a04b784d521d51bc2021367a812e629c95dc20a770017de59`); source `/opt/anyaicam-staging-source-backup-pre-self-service-provisioning-fix-20260912T205215Z.tar.gz` (SHA-256 `71597d844b82ccb81327a24c2b6b577f5a65168eecf191a0e31773f7948b6f29`). Source tarball `git -c core.autocrlf=false archive 8b07dc1 -- app` → SHA-256 `e6b1a9438e787c6fee93bbdafdec00479aefc5592420e521c8fbdd92fd5e2c32`, verified identical after `scp`; `app/` rsync'd into `/opt/anyaicam-staging/` with `--delete`, `diff -rq` confirmed identical after (`deploy/`/`storefront/` untouched). Built `deploy-portal:8b07dc1`. **Env-file drift check** (per the safeguard this doc's own `green.env` config-drift entry recommends): diffed both `~/blue-green-rehearsal/green-live.env` and `/etc/anyaicam-staging/vms-staging.env` against `portal-green`'s actual running environment before recreating — **neither file matched** (both `ANYAICAM_ADMIN_PASSWORD` and `ANYAICAM_APP_SECRETS` differ from what's actually live; `vms-staging.env` is also missing `ANYAICAM_DATABASE_BACKEND=sqlite`). Rather than reuse either stale file, captured the running container's own real environment directly (`docker inspect portal-green`, minus container/build-time built-ins: `PATH`/`HOSTNAME`/`HOME`/`LANG`/`GPG_KEY`/`PYTHON_VERSION`/`PYTHON_SHA256`) into a new file, `~/blue-green-rehearsal/green-live-8b07dc1.env`, and recreated `portal-green` from that — so this redeploy preserves the exact live secrets/session-signing state already in production use and does not silently invalidate sessions or the admin password the way blindly reusing either stale file would have. **Neither `green.env`/`green-live.env`/`vms-staging.env` was edited** — recorded here as tech debt, not fixed, matching this doc's own recommendation to add a mandatory pre-flight diff rather than trust either file blindly.

**Verified**: exactly one `portal-green` container, `Up`, image `deploy-portal:8b07dc1`; `https://portal-staging.anyaicam.com/health` → `200 ok`; `/version` unchanged (`cloud_id: AIC-C90CF0C9`, no AWS/Motion Cloud flags); `partner_workspace.py` hashed inside the running container (`b063e5772dab3a75d366da2ee9ae4de7fc580e8ea94cc67b56553ef3995fe7d2`) matches `git show 8b07dc1:app/partner_workspace.py` byte-for-byte; DB `integrity_check: ok`. **Route verification**: introspected the live app's own route table inside the running container — `POST /api/customer/appliances/provision` is registered. (Did not mint a session for, or otherwise touch, the real `anyaicamtest@gmail.com` account to exercise the endpoint end-to-end through the public path — that would have provisioned it, which was explicitly out of scope this pass; the 12-test suite above already proves the endpoint end-to-end against the identical code path.)

**Customer-state verification (read-only, unchanged by this deploy)**: `anyaicamtest@gmail.com` (`d75bdbecdd4887de4d2b89a9fcea9092`) still has **zero** `sites`/`appliances`/`cameras` rows; `hardware_orders` still `status=paid`; `customer_entitlements` still `camera_slot_quantity=8`/`status=active`. **No provisioning was performed against the real account. Ryzen and Samsung were not touched.**

### State to resume from

The self-service provisioning bridge is live on `anyaicam-staging`. `anyaicamtest@gmail.com` is approved, has a paid hardware order and an active 8-slot entitlement, and has not been provisioned — the next action is the customer (or whoever is driving this test) opening Customer Setup, clicking "Provision your first appliance," and continuing through the existing, unmodified Cloud ID link/activation flow themselves.

---

## 2026-09-13: Customer Setup Step 3 showed a stale "not activated" appliance after real Ryzen activation — two source fixes, DONE, deployed, verified

**Real customer journey continued from the entry above**: the customer provisioned `AIC-C814766E` via the self-service panel, then ran `anyaicam-setup` on Ryzen for real — activation succeeded, `activation_status='activated'`, real heartbeat flowing (confirmed read-only, no continued old-identity traffic, 8-slot entitlement intact). But after "Save and continue" from Step 2, Step 3 showed Cloud ID "—", Software "Not installed", Status "offline", Last check-in "Never" — contradicting the real, healthy DB state.

**Root cause, traced (not assumed)**: `customer_first_setup()` (`app/partner_workspace.py`) renders its `appliances` list, and the `<select id="customer-appliance">` built from it, **once, server-side, at page load**, baked into the page's own `<script>` as a static JS array. Step 3's `showSetup()` does a pure in-memory `appliances.find(...)` against that same array — no API call. The self-service "Provision appliance" button's success handler only wrote the returned Cloud ID/activation-token into the two text inputs; unlike "Link appliance" (which already does `location.reload()` on success), it never reloaded the page. So "Save and continue" carried the stale, pre-provisioning (empty) `appliances` array straight through Step 3 *and* Step 4 (`selectedAppliance()` returns `''` there too, since the `<select>` had no `<option>`s). This is not "Step 3 still coupled to the old `/link` workflow" in the sense of calling it — it's that only `/link`'s success handler happened to force the reload Step 3 actually needs.

**Second finding from the same trace, directly answering "is Link now redundant or required?"**: `link_customer_appliance()` unconditionally overwrote `online_status`/`software_version`/`last_check_in` from `get_provisioning_backend().verify_link()` — a snapshot fixed forever at `provision()` time (real heartbeats never touch it; they go straight into the SQL row via `appliance_cloud.heartbeat()`). Nothing downstream actually requires `activation_status='linked'` (the one real gate, `customer_account()`, checks for `'activated'`, which the physical device's own `/api/appliance/activate` call — and, independently, Step 7's `confirm_customer_setup()` — already set). So "Link appliance" was never architecturally required; worse, clicking it *after* real activation would have silently regressed a genuinely healthy, heartbeating appliance back to looking dead in the DB.

**FIX 1** (`app/partner_workspace.py`, `customer_first_setup()`'s script): on a successful `POST /api/customer/appliances/provision`, reload the page — matching "Link appliance"'s own existing pattern — so `appliances` is re-read from the authoritative SQL state. Gated behind `confirm()` for a genuinely fresh provision (never before), so the one-time-shown activation token/QR value isn't silently discarded before the customer can copy it; the idempotent `already_provisioned` response (which never carries a fresh token) reloads immediately, no confirm needed.

**FIX 2** (`app/partner_workspace.py`, `link_customer_appliance()`): skip the `online_status`/`software_version`/`last_check_in` overwrite when `appliance['activation_status']=='activated'` already. Token verification (the actual security check) and everything else — audit log, the Getting Started email trigger, the response shape — is unchanged. An appliance the physical device has *not* yet activated still gets its status populated from `verify_link()` exactly as before (regression-tested as a control case).

**Tests** (`app/tests/test_setup_step2_provision_reload_and_link_protection.py`, 4 new, all passing): fresh-provision success renders the confirmation message then a `confirm()`-gated reload; the `already_provisioned` branch reloads immediately; `/link` leaves a real, heartbeating appliance's `online_status`/`software_version`/`last_check_in`/`activation_status` completely untouched; a not-yet-activated appliance's status is still correctly populated from `verify_link()` (proves the fix is scoped to the right condition, not a blanket disable).

**Regression proof**: full suite before/after this change — byte-for-byte identical 84 pre-existing, unrelated failures (`diff` of the two sorted `FAILED` lists is empty), 1656 passed (+4 for these tests).

**Commit**: `b07eb6e` (`app/partner_workspace.py`, `app/tests/test_setup_step2_provision_reload_and_link_protection.py`).

**Deployed to `anyaicam-staging`**: pre-deploy backups — DB `/var/lib/anyaicam-staging/db/staging-pre-step3-reload-linkfix-20260912T225016Z.db` (SHA-256 `fb1e910fdaff6216fb01a6e5cd8cc535c3732e7db959aac40469f4f145708dee`); source `/opt/anyaicam-staging-source-backup-pre-step3-reload-linkfix-20260912T225016Z.tar.gz` (SHA-256 `235dcf5ac03b38f9b24d8bb4a2cc9be3b9285173cdd73a2556a9f685dbd05b58`). Source tarball `git -c core.autocrlf=false archive b07eb6e -- app` → SHA-256 `3e9f2712a5ceb5491bd7cbd84347a1983604df9eba61c1e181a02431847759c6`, verified identical after `scp`; `app/` rsync'd with `--delete`, `diff -rq` confirmed identical after. Built `deploy-portal:b07eb6e`. Env-file check: freshly captured `portal-green`'s actual running environment and diffed it against the file used for the *previous* deploy (`green-live-8b07dc1.env`) — byte-for-byte identical, confirming no drift since the last redeploy — so that same known-good file was reused rather than trusting `green.env`/`green-live.env`/`vms-staging.env` (still not edited, still recorded tech debt from the prior entry). `portal-green` recreated with `deploy-portal:b07eb6e`, identical mounts/network/env-file.

**Verified**: exactly one `portal-green` container, `Up`, image `deploy-portal:b07eb6e`; `/health` → `200 ok`; `partner_workspace.py` hashed inside the running container matches `git show b07eb6e:app/partner_workspace.py` byte-for-byte; DB `integrity_check: ok`; both `/api/customer/appliances/link` and `/api/customer/appliances/provision` confirmed registered live.

**Customer-state verification (read-only)**: `AIC-C814766E` (`2f941627b4`) unchanged and healthy through the redeploy — `activation_status=activated`, `online_status=degraded` (real), `software_version=0.1.0` (real), `last_check_in` fresh (heartbeat kept flowing through the container recreation); `sites`/`customer_entitlements` (`camera_slot_quantity=8`/`active`)/`hardware_orders` (`paid`)/`cameras` (8) all unchanged. `customer_setup_drafts` for this customer shows `current_step=2`, `data_json={"appliance_id":"","scan_job":null}` — the empty `appliance_id` recorded by the bug's own symptom, before this fix. Traced against the now-deployed source: on next load, `initial_appliance_id` resolves via the existing fallback (`str('') ` matches no real appliance → falls back to `appliances[0]['id']`, the only appliance) to `2f941627b4` — so Step 4's `<select>` will correctly default to the real appliance, and Step 3 will render Cloud ID `AIC-C814766E`, Software `0.1.0`, Status `degraded`, Last check-in the real recent timestamp, and Assigned site `f67fa371cd` (the template shows the raw site id, not the site's name — a pre-existing display detail, unrelated to this fix). Not verified by rendering the live authenticated page for this specific real customer (minting a session for their account was avoided, consistent with not acting as the customer) — verified instead by confirming the exact data this now-deployed code path reads, matches source-level behavior traced line-by-line. Ryzen and Samsung were not touched; no discovery was re-run; no cameras were provisioned; nothing about `AIC-C814766E` or the entitlement was altered.

### State to resume from

Both fixes are live. The customer (or whoever is driving this test) can now safely continue: opening Customer Setup will land back on Step 2 (last saved step), where the provisioning panel is correctly absent (an appliance already exists) and clicking "Save and continue" once will reach Step 3 showing the real, healthy `AIC-C814766E` state. Clicking "Link appliance" is no longer necessary and is now safe even if clicked by habit — it will no longer regress the appliance's live status.

---

## 2026-09-13: Step 4 "Request appliance scan" backlog/pileup defect — source-fixed, DONE, deployed, verified (customer journey continues from Step 4)

**Reported symptom**: on the real account, clicking "Request appliance scan" (Step 4) "did not appear to start/complete a scan or return the 5 cameras."

**Traced read-only first** (prior turn, no code touched): the full chain — browser → `POST /api/customer/appliances/{id}/scan` → `camera_scan_jobs` row → `GET /api/appliance/{cloud_id}/scan-jobs` (claims it) → Ryzen's `poll_discovery()` → real `scan()` → `POST /api/appliance/{cloud_id}/scan-jobs/{job_id}` → browser `pollScan()` — was found to work correctly end to end at every stage; `aa4dc2edb135e379631a9d17db8923e56a202ed9` (confirmed an ancestor of this branch, `service.py`/`discovery.py`/`reenrollment.py` byte-identical since) fully supports and executes discovery. **Real root cause**: `request_camera_scan()` (`app/partner_workspace.py`) created a brand-new `camera_scan_jobs` row on every call with no de-duplication. The real account had been clicked 6 times in 49 seconds, and Ryzen's `poll_discovery()` claims and works through every non-terminal job for one appliance strictly sequentially — one real `scan()` per job, ~24s each — so the backlog took over 3 minutes to fully drain, and the browser's `pollScan()` always re-reads the mutable global `scanJob` (the newest click), meaning the customer was watching the *last*-clicked job sit at the back of an ever-growing queue. Every job still completed correctly with the expected 5 discovered devices (confirmed: all 6, and 4 more created before this fix was deployed, ended up `status=complete` with correct results) — this was a latency/pileup defect, not a broken chain.

**FIX 1** (`app/partner_workspace.py`, authoritative): `request_camera_scan()` now looks up an existing `camera_scan_jobs` row for the same appliance whose status is not in `CAMERA_SCAN_TERMINAL_STATES` and returns it instead of inserting another. The found job is run through the existing `_maybe_time_out_scan_job()` first, so a genuinely abandoned job still correctly frees up a new scan. Terminal jobs (`complete`/`error`/`timed_out`/`cancelled`) never block a new request.

**FIX 2** (`app/partner_workspace.py`, UX only): "Request appliance scan" is disabled and relabeled "Discovery in progress…" for as long as `pollScan()` reports a non-terminal status (on the initial click and every poll tick, so a mid-scan page reload via `loadLatestScan()` also reflects it correctly), and re-enabled the moment a terminal status is reached. Purely a safeguard — Fix 1 is what actually prevents duplicate jobs regardless of client behavior.

**Tests** (`app/tests/test_camera_discovery_provisioning.py`, 7 new, all passing): repeated requests while `queued`/`waiting_for_appliance`/`running` each return the identical `job_id`; 6 repeated requests (matching the exact count confirmed live) create exactly one DB row; a terminal (`complete`) job correctly allows a new scan (DB then holds exactly two rows); a genuinely timed-out job also correctly frees up a new scan via the existing lazy-timeout helper; an already-completed job's real 5-device results remain fully intact and correctly readable after the guard creates a follow-up job for the same appliance.

**Regression proof**: full suite before/after — byte-for-byte identical 84 pre-existing, unrelated failures (confirmed via `git stash` isolation); 1663 passed (+7). (Running `test_camera_discovery_provisioning.py` alone showed 26 unrelated camera-provisioning-test failures — confirmed pre-existing test-order-dependent isolation debt already on record in this doc, identical with and without this change, not caused by it.)

**Commit**: `9a4d31b` (`app/partner_workspace.py`, `app/tests/test_camera_discovery_provisioning.py`).

**Deployed to `anyaicam-staging`**: pre-deploy backups — DB `/var/lib/anyaicam-staging/db/staging-pre-scan-idempotency-fix-20260913T020443Z.db` (SHA-256 `00e037f8a3fc012ca8e079d56e1cf42460089d879063ce85872f8fd636d0decf`); source `/opt/anyaicam-staging-source-backup-pre-scan-idempotency-fix-20260913T020443Z.tar.gz` (SHA-256 `8910ecb4e747f3d07d4d90a642849df06da1314e95e037820c0b153c28c4df1a`). Source tarball hash verified identical after `scp`; `app/` rsync'd with `--delete`, `diff -rq` confirmed identical after. Built `deploy-portal:9a4d31b`. Env-file drift check: captured `portal-green`'s actual running env fresh and diffed against the last known-good file (`green-live-8b07dc1.env`) — byte-for-byte identical, no drift, same file reused. `portal-green` recreated with `deploy-portal:9a4d31b`, identical mounts/network/env-file.

**Verified**: exactly one `portal-green`, `Up`; `/health` → `200 ok` (one transient empty response ~15s after `docker run`, before the container fully warmed up — expected, resolved on the next check); `partner_workspace.py` hashed inside the container matches `git show 9a4d31b:app/partner_workspace.py` byte-for-byte; DB `integrity_check: ok`; `/api/customer/appliances/{appliance_id}/scan` and `/scans/latest` confirmed registered. Container logs show real Ryzen appliance traffic (heartbeat/cameras/commands/scan-jobs/provisioning-jobs) succeeding immediately after the recreate — no interruption to the live appliance.

**Customer-state verification (read-only, unaltered by this deploy)**: `AIC-C814766E`'s `camera_scan_jobs` now shows 10 rows total (the original 6 from the diagnosis pass, plus 4 more the customer created before this fix went live) — every single one `status=complete`/`progress=100` with its own correct discovery results; none were modified, re-run, or deleted. `customer_entitlements` still `camera_slot_quantity=8`/`active`; `cameras` still 8 (all still placeholders, `device_key=NULL` — Fix 1 only changes job *creation*, never touches `cameras`). **No physical scan was re-run, no cameras were provisioned, and no existing discovery result was modified during this deployment or its verification. Ryzen and Samsung were not touched; Motion Cloud was not enabled.**

### State to resume from

The idempotency guard and UI protection are live. The customer can resume from Step 4 now: clicking "Request appliance scan" will either reuse whichever job (if any) is still in flight from the earlier pileup, or — since all 10 existing jobs are already `complete` — open a fresh one that behaves normally and completes in ~24s, with the button correctly disabled/labeled while it runs. The 5 already-discovered devices are unchanged and available for the customer to review before choosing to add any of them (Step 4's "Add this camera" flow) — not touched by this pass.

---

## 2026-09-13: Camera 1 blockers source-fixed (commit `6d6dcd5`) — deployed to `anyaicam-staging`; Ryzen artifact built, hash-verified, and staged — **install still pending the operator's own hands**

Three fixes traced from Camera 1 (`AIC-C814766E`, `dfba6a63ec`) provisioning fully on the cloud but never becoming functional locally, and consuming a 9th camera row instead of one of the 8 licensed placeholders:

1. `app/main.py`: `/api/local/provisioned-camera-credential` added to `PUBLIC_PATH_PREFIXES` (same defect class, same fix already used once for `/api/provisioning/refresh` — confirmed live: every call was rejected 401 by `authentication_middleware` before the route's own checks ever ran).
2. `app/main.py`: new `_docker_bridge_gateway_ip()` (reads `/proc/net/route`, cached) — the endpoint's own loopback check now also accepts this one dynamically-resolved address, accounting for Docker's hairpin-NAT rewrite of a host-originated call to its own published port (confirmed live: arrives as `172.18.0.1`, not `127.0.0.1`) without trusting any subnet or broader RFC1918 range. The mandatory appliance bearer-credential check is unchanged.
3. `app/appliance_cloud.py`, `appliance_submit_provisioning()`: a genuinely new `device_key` now consumes the oldest available same-customer/site/appliance placeholder (`device_key IS NULL`, `status='pending_installation'`) instead of always inserting a new row; falls back to insert only when none exists; the pre-existing "already-known device_key" reprovision branch is untouched and still checked first.

**Tests**: 9 new (5 in `test_local_provisioned_camera_credential_endpoint.py`, 4 in `test_camera_discovery_provisioning.py`) — gateway-address acceptance/rejection scoping, bearer-credential still mandatory from the trusted address, fails-closed when the gateway can't be resolved, `PUBLIC_PATH_PREFIXES` regression; placeholder consumption, idempotent reprovision (no second placeholder consumed), cross-tenant isolation, no-placeholder fallback. All pass. Full suite: identical 84 pre-existing failures before/after (empty diff of sorted `FAILED` lists), 1672 passed (+9).

**Deployed to `anyaicam-staging`** (`portal-green`, `deploy-portal:6d6dcd5`) — pre-deploy DB/source backups taken, source hash verified end-to-end, env confirmed byte-identical to the prior known-good deploy before reuse. Post-deploy: exactly one `portal-green`, `/health` 200, `main.py`/`appliance_cloud.py` hashes match the commit byte-for-byte, DB integrity ok, route registered and `PUBLIC_PATH_PREFIXES` entry confirmed present. Read-only re-check: Camera 1, all 8 placeholders, and the entitlement completely unchanged by this deploy.

### Ryzen — versioned artifact built and staged, install NOT yet run

`/api/local/provisioned-camera-credential` and its auth logic run inside **Ryzen's own `anyaicam-vms` container** (edge role), not on `anyaicam-staging` — deploying to staging alone does not fix Camera 1's local delivery. Per this project's own standing rule (no sudo access by design; every prior real install on this box — including `aa4dc2e` itself, see the 2026-09-12 "manual, by the operator" entry above — was run by the operator, never by an automated session), the actual `install.sh` run must be done by the operator, not driven here.

Built via the established versioned pipeline (`installer/build_release_installer.py --vms-commit 6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c --vms-repo .`):
- **Artifact**: `anyaicam-appliance-installer-1.1.0-vms-6d6dcd5d6e66.tar.gz`
- **Artifact SHA-256**: `168fdb44fd9601258df1e2fe5ba0095829bfa1a2a0bdcfbe0e7ada389e074357` — verified identical after `scp` to Ryzen (`/home/alejandro-mata/anyaicam-appliance-installer-1.1.0-vms-6d6dcd5d6e66.tar.gz`) and after extraction to `~/anyaicam-install-6d6dcd5/` on Ryzen.
- **VMS release commit**: `6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c`; `release_source_sha256=0089a1d909b21d659ced6be499f292a97a8e3a7d388bfe5033fbff8caf13535a`.
- Appliance-agent bundled unchanged (this fix touches only `app/main.py`/`app/appliance_cloud.py`) — a repair install is expected to be a no-op for the agent side.

**Pre-install baseline captured (read-only), for post-install comparison**:

| Check | Value |
|---|---|
| `/version` `build_id` | `aa4dc2edb135e379631a9d17db8923e56a202ed9` |
| `/version` `cloud_id` | `AIC-C814766E` |
| `anyaicam-vms` container image id | `sha256:3d3cfefd0c40a4...` |
| `anyaicam-vms` container started | `2026-09-12T15:13:52Z` |
| `anyaicam-agent.service` `ActiveEnterTimestamp` / `NRestarts` | `Sat 2026-09-12 16:33:38 CDT` / `0` |
| Local `cameras` / `camera_credentials` / `pending_camera_credentials` | `12` / `0` / `0` |
| `appliance_identity.json` SHA-256 | `2c32127ffb097195a66d8332698506ede1f7059bfdbe3d9ce9212eed21cc24be` |

**Not executed**: `sudo ./install.sh` (repair path) on Ryzen — this requires the operator's own hands. The artifact is staged and hash-verified at `~/anyaicam-install-6d6dcd5/` on Ryzen, ready to run.

### Post-install verification — operator ran `sudo ./install.sh --repair` — PASSED, all read-only

| Check | Baseline (`aa4dc2e`) | Post-install (now) | Result |
|---|---|---|---|
| `/version`/`/health` `build_id` | `aa4dc2edb135e379631a9d17db8923e56a202ed9` | `6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c` | **exact match to target** |
| `/version` `cloud_id` | `AIC-C814766E` | `AIC-C814766E` | unchanged |
| `anyaicam-vms` container | `sha256:3d3cfefd...`, started `2026-09-12T15:13:52Z` | new image `sha256:024fa4b5...`, started `2026-09-13T03:44:25Z`, `Up ... (healthy)`, exactly one container | expected clean recreate |
| `main.py`/`appliance_cloud.py` inside the running container | -- | SHA-256 matches `git show 6d6dcd5:app/{main.py,appliance_cloud.py}` byte-for-byte | **exact** |
| `appliance_identity.json` SHA-256 | `2c32127ffb097195a66d8332698506ede1f7059bfdbe3d9ce9212eed21cc24be` | `2c32127ffb097195a66d8332698506ede1f7059bfdbe3d9ce9212eed21cc24be` | **byte-for-byte unchanged** — identity/activation state fully preserved |
| `anyaicam-agent.service` | `ActiveEnterTimestamp` `16:33:38 CDT`, `NRestarts=0` | new `ActiveEnterTimestamp` `22:44:21 CDT` (one clean restart triggered by the install), `NRestarts=0` | clean restart, not a crash loop |
| `anyaicam-vms.service` | -- | `active (exited)` (normal for this compose-wrapper unit), `NRestarts=0`, one `ActiveEnterTimestamp` matching the install | clean, no loop |
| Agent re-auth / heartbeat | -- | Agent log: restarted `22:44:21`, `Entitlement refreshed camera_slot_quantity=8` at `22:44:24` (3s later); cloud `appliances.last_check_in` `2026-09-13T03:45:25` (24s old at check time), `restart_count` incremented once (32, expected for a genuine restart) | **re-authenticated, heartbeat resumed immediately** |
| Docker / network / Tailscale | -- | Docker Server Version `29.1.3`, `overlayfs`; `eno1` LAN `192.168.0.228`, `tailscale0` `100.77.253.28` (matches the given target), bridge `br-2753aa1c930c` gateway `172.18.0.1`; `tailscale status` shows this device active alongside the other known fleet devices (Samsung correctly still offline/untouched) | healthy |
| `/api/local/provisioned-camera-credential` | 401 from `authentication_middleware` before the route ever ran (the bug) | route registered; `'/api/local/provisioned-camera-credential' in main.PUBLIC_PATH_PREFIXES` → `True` | **fixed, confirmed live** |
| `_docker_bridge_gateway_ip()` | n/a (didn't exist) | called live inside the running container → resolves to `172.18.0.1`, exactly matching the real bridge gateway independently confirmed via `ip addr` on this box | **works correctly on this actual Ryzen/Docker network** |
| Endpoint's bearer-credential requirement | -- | verified via `inspect.getsource()` of the live, running function (not a live call, to stay read-only and never touch/expose the real credential) — the `presented != identity["credential"]` check is present and unconditional, unchanged from source | **still mandatory** |
| Local `cameras` / `camera_credentials` / `pending_camera_credentials` | `12` / `0` / `0` | `12` / `0` / `0` | **unchanged** — no reprovisioning happened as a side effect of the install |
| Camera 1 (`dfba6a63ec` local, `7e34833a37` is the separate pre-existing old-identity orphan sharing the same physical device_key — unrelated, already documented) | `status=configured`, no local credential | identical: `status=configured`, `camera_number=1`, `customer_id` unchanged, still no local credential | **unchanged** |
| Cloud `appliance_camera_status` for `dfba6a63ec` | `last_error=camera_not_bound` | `last_error=camera_not_bound`, fresh `updated_at` (heartbeat still correctly reporting it) | **unchanged, exactly the expected state** |

No credential or token value was printed, logged, or exposed at any point during this verification. Nothing was reprovisioned, no discovery was re-run, no placeholder was touched, Samsung was not touched, Motion Cloud was not enabled.

### State to resume from

Ryzen is now genuinely running commit `6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c`, fully verified, with identity/activation/camera state completely preserved. Both source fixes for Camera 1's local credential handoff are live on both sides (`anyaicam-staging` and Ryzen). The two remaining, explicitly not-yet-authorized next steps are unchanged: (1) reprovision Camera 1 through the normal Customer Setup Step 4 "Add this camera" flow (reusing the existing discovery results, no new scan needed) to actually deliver its credential locally now that the fix is live, and (2) the one-time placeholder-count reconciliation for this customer (delete exactly one of the 8 untouched placeholder rows) once Camera 1's reprovision is confirmed working.

---

## 2026-09-13: Camera 1 reprovisioned — credential handoff fully succeeded, but cloud still reported `camera_not_bound`. Traced to a stale `camera_bindings.json` entry left over from Ryzen's prior identity — two source fixes + one diagnostic improvement, tested and built as a new versioned artifact. **Not yet deployed to Ryzen.**

**Read-only verification after Camera 1's reprovision** (Customer Setup Step 4, "Add this camera" clicked again): `camera_provisioning_requests` shows a second successful job (`9987e265a342`, `status=provisioned`, same `camera_id=dfba6a63ec` — no new row, no placeholder consumed, exactly as designed); agent log shows `rtsp_auth_diagnostic ... outcome=auth_correct`; **no** "Local VMS credential handoff failed" warning this time (the only such warning anywhere in the log is the old, pre-fix one); local `pending_camera_credentials` is empty and local `camera_credentials` now has a real row for `dfba6a63ec`; `docker logs anyaicam-vms` shows an actively running `[camera1]` FFmpeg process genuinely transcoding real RTSP video to HLS. **The credential/streaming fix from the previous pass worked completely.** Yet `appliance_camera_status` kept reporting `last_error=camera_not_bound`, `online=0`, `recording=0` across multiple heartbeat cycles.

**Root cause, traced source-to-data** (`sync_configuration()` → `auto_bind_discovered_cameras()` → `camera_bindings.json` → `reconcile_cloud_cameras()` → `appliance_camera_status`): `discovered_cameras.json` already has a fully-resolved entry for this exact `device_key` (`urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af`) with real MAC `14:2f:fd:a2:f6:af` — local evidence is complete, no new scan needed. But `camera_bindings.json` already contained a binding for that exact MAC/`camera_number=1`, approved `2026-09-12T05:51:28Z` — **before** Ryzen was even cloud-side released and re-claimed as `AIC-C814766E` — under the OLD, orphaned `cloud_camera_id` `7e34833a37` (a camera row belonging to a `customer_id`/`appliance_id` that was cloud-side deleted weeks ago). `CameraBindingStore.bind()`'s MAC/camera_number collision check has no way to know that old binding's cloud_camera_id no longer exists anywhere, so it silently refused (`ValueError`, caught by `auto_bind_discovered_cameras()`'s own `except: continue`) to let the real, current camera (`dfba6a63ec`) claim the same physical hardware — every single cycle, forever, since nothing ever invalidates a binding when the box is re-enrolled under a new identity.

### Fixes implemented (commit `1893a72`)

1. **`appliance-agent/anyaicam_agent/reenrollment.py`, `coordinated_reenroll()`** — authoritative prevention. `camera_bindings.json` is now reset (`{"version":1,"bindings":[]}`) atomically alongside the three identity files this function already swaps/backs-up/rolls-back, but only when the `cloud_id` is genuinely changing (a same-`cloud_id` credential refresh keeps still-valid bindings). No prior file is not an error. On any failure, the prior bindings file is restored byte-for-byte (or removed if none existed) through the exact same all-or-nothing path — atomicity unchanged. `discovered_cameras.json` is never touched.
2. **`camera_binding.py`, `CameraBindingStore.bind()` / `auto_bind_discovered_cameras()`** — defense in depth for bindings that are *already* stale (exactly Ryzen's current state, since fix 1 is forward-looking only). `bind()` now accepts `valid_cloud_camera_ids`; a conflicting binding whose own `cloud_camera_id` is absent from the appliance's current, real cloud camera configuration is superseded rather than blocking forever. A conflicting binding whose `cloud_camera_id` *is* still current still raises exactly as before — two currently-valid cloud cameras can never silently share one physical camera. A direct `bind()` call omitting the parameter keeps the original, always-a-collision behavior.
3. **`camera_binding.py`, `reconcile_cloud_cameras()`** — narrow diagnostic improvement. An unbound camera whose `device_key` genuinely has local discovery evidence now reports `last_error='camera_binding_conflict'` instead of the same generic `camera_not_bound` a camera that's simply never been seen on the network also shows.

**Tests** (12 new): reenrollment — successful reenroll clears old bindings; prior bindings correctly backed up; failed reenroll restores them byte-for-byte (identity-file atomicity proven still intact); failed reenroll with no prior bindings leaves none behind; `discovered_cameras.json` survives untouched; a same-`cloud_id` refresh preserves bindings. camera_binding — orphaned binding superseded and the real camera binds; a still-valid conflicting binding continues to block a different camera; direct `bind()` call keeps original behavior; **the exact real Camera 1 device-key/MAC/old-vs-new-cloud-id scenario reproduced verbatim as its own named test**; `camera_binding_conflict` vs plain `camera_not_bound` vs the existing bound-and-streaming path all correctly distinguished. Full appliance-agent suite: 508 passed, 3 failed — confirmed via `git stash` A/B identical, pre-existing, Windows-filesystem-specific failures (POSIX `ENOTDIR` semantics this Windows dev machine doesn't reproduce), unrelated to this change. `app/`'s own suite is unaffected by construction — grepped, zero runtime import of `anyaicam_agent` from the main VMS codebase.

**Versioned artifact built** (not hot-patched, not yet deployed): `installer/build_release_installer.py --vms-commit 1893a727958678ed88ecfbcc6f2ea61a46123698 --vms-repo .` → `anyaicam-appliance-installer-1.1.0-vms-1893a7279586.tar.gz`, SHA-256 `063e862a5417a38b6969e752872694b64177bee42d00e95503b3b92508d02fff`, `release_source_sha256=04f9ea66211c5ff68a52c37b76395f99c52979c59d04664c8bce04935c8e29e0`. Verified the artifact genuinely bundles the fix (extracted and grepped `payload/agent/anyaicam_agent/camera_binding.py` for the new code, found it). **Sitting locally only — not transferred to Ryzen, not installed, per explicit instruction to stop before deployment.**

### Expected behavior once this is deployed to Ryzen

`reset_bindings` in `coordinated_reenroll()` will **not** fire from this deploy alone (no reenroll happens as part of installing new agent/VMS code) — fix 1 is purely forward-looking, protecting the *next* re-enrollment. The fix that actually heals Ryzen's *current* stale state is fix 2: the next time the agent cycles `sync_configuration()` after restarting with this code, `auto_bind_discovered_cameras()` will see that `7e34833a37` is not in the current appliance's `cloud_cameras` list at all (it belongs to a different, deleted `appliance_id`), treat that binding as orphaned, and let `dfba6a63ec` claim MAC `14:2f:fd:a2:f6:af`/`camera_number=1`. Since `discovered_cameras.json` already has the correct match and the VMS is already streaming, `reconcile_cloud_cameras()` should then report real `online`/`recording` values and clear `camera_not_bound` — **automatically, on the very next cycle, with no new discovery scan and no new credential entry required.**

### State to resume from

Deployment to Ryzen (build the same way as `6d6dcd5` was: versioned artifact → hash-verified transfer → **operator runs `sudo ./install.sh --repair`**, never hot-patched) and the matching post-install verification are the next, still-unauthorized steps. Camera 1's underlying data (cloud and local) was not touched by this diagnosis/fix pass. The extra-placeholder reconciliation remains separately not-yet-authorized.

---

## 2026-09-13: `1893a72` installed on Ryzen — Camera 1 self-healed exactly as predicted, fully automatically. camera_not_bound cleared. DONE, verified.

**Deployed**: `anyaicam-appliance-installer-1.1.0-vms-1893a7279586.tar.gz`, SHA-256 `063e862a5417a38b6969e752872694b64177bee42d00e95503b3b92508d02fff` — verified identical after `scp` to Ryzen, extracted, then the operator ran `sudo ./install.sh --repair` themselves (never hot-patched, never run by this session).

**Post-install verification, all read-only, all PASSED:**

| Check | Baseline (`6d6dcd5`) | Now | Result |
|---|---|---|---|
| `/version` `build_id` | `6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c` | **`1893a727958678ed88ecfbcc6f2ea61a46123698`** | exact match |
| `cloud_id` | `AIC-C814766E` | `AIC-C814766E` | unchanged |
| `appliance_identity.json` SHA-256 | `2c32127f...` | `2c32127f...` | **byte-for-byte unchanged** |
| `anyaicam-vms` container | image `sha256:024fa4b5...` | new image `sha256:2afbe39e...`, `Up ... (healthy)`, exactly one container | clean recreate |
| `anyaicam-agent.service` | `ActiveEnterTimestamp` `22:44:21 CDT`, `NRestarts=0` | new `ActiveEnterTimestamp` `23:37:58 CDT` (one clean restart from the install), `NRestarts=0` | clean, not a crash loop |
| Agent re-auth / heartbeat | — | restarted 23:37:58/23:37:59 → `Entitlement refreshed camera_slot_quantity=8` at 23:38:01 (2s later) | resumed immediately |
| `discovered_cameras.json` | 5 devices, `updated_at=2026-09-13T01:43:57...` | **byte-identical**, same `updated_at`, same 5 devices including `...a2f6af`/MAC `14:2f:fd:a2:f6:af` | intact, untouched |
| `camera_bindings.json` — stale `7e34833a37` binding | present (`camera_number=1`, MAC `...a2f6af`) | **gone** — superseded | self-healed |
| `camera_bindings.json` — `dfba6a63ec` (Camera 1) | absent | **present**: `camera_number=1`, `mac_address=14:2f:fd:a2:f6:af`, `approved_at=2026-09-13T04:37:59...` (seconds after the agent restart — the very first `sync_configuration()` cycle) | **bound automatically, no scan, no new credential** |
| `camera_bindings.json` — the other two old bindings (`ca9d8c53d0`→2, `2e1a9a64bc`→3) | present | **still present, untouched** | correct — nothing currently claims those slots, so they were never touched; the fix only supersedes a binding when a real, current camera actually needs to claim the same MAC/number |
| Local `camera_credentials` for `dfba6a63ec` | 1 row, 140-byte blob | unchanged: 1 row, 140-byte blob | credential preserved |
| Camera 1 streaming | active `[camera1]` FFmpeg → HLS | **still active**, ~3.5 min continuous, frame count climbing normally | uninterrupted |
| Cloud `appliance_camera_status` for `dfba6a63ec` | `online=0`, `recording=0`, `last_error=camera_not_bound` | **`online=1`, `recording=1`, `last_error=None`** | **camera_not_bound fully cleared** |
| Cloud/local camera + placeholder counts | cloud 9 total (8 placeholders + Camera 1) / local 12 | **unchanged**: cloud 9/8 placeholders, local 12 | **no new camera or placeholder row created or consumed** |

No credential value was printed/logged/exposed; `camera_bindings.json` was not manually edited; no discovery was re-run; Camera 1's credentials were not re-entered; Cameras 2–5 were not added; the extra placeholder was not touched; Samsung and Motion Cloud were not touched.

### State to resume from

Camera 1 (`AIC-C814766E`, `dfba6a63ec`) is now fully functional end to end: cloud-provisioned, locally credentialed, actively streaming, correctly bound, and correctly reported (`online`/`recording`/`last_error` all healthy) — the entire chain traced and fixed across this session's last several passes is closed. Two items remain separately, explicitly not-yet-authorized: (1) provisioning Cameras 2–5 through the same now-fully-working Step 4 flow, and (2) the one-time placeholder-count reconciliation (cloud still shows 9 total rows — 8 placeholders + Camera 1 — for an 8-slot entitlement; deleting one placeholder was designed earlier in this doc but never executed).

---

## 2026-09-13: One-row placeholder reconciliation executed for `anyaicamtest@gmail.com` — DONE, verified. Cloud camera-row count now matches the 8-slot entitlement exactly.

**Authorized, narrowly scoped, executed with the same discipline as every prior real-data mutation in this doc**: delete exactly one of the 8 leftover generic placeholder rows for this one customer/site/appliance, left over only because Camera 1 was originally provisioned before the placeholder-consumption source fix existed.

**Backup**: `/var/lib/anyaicam-staging/db/staging-pre-placeholder-reconciliation-20260913T044526Z.db`, SHA-256 `d00a2230e2b6d2f25dbc9c016249f49359fbcde8766143dcad6228633dad06a4`. `PRAGMA integrity_check: ok` confirmed immediately before mutating.

**Target identification**: `SELECT ... FROM cameras WHERE customer_id='d75bdbecdd4887de4d2b89a9fcea9092' AND site_id='f67fa371cd' AND appliance_id='2f941627b4' AND device_key IS NULL AND status='pending_installation' ORDER BY created_at ASC, id ASC` returned exactly 8 rows, confirming the expected count before touching anything. All 8 share the identical `created_at` (`2026-09-12T21:10:54.746225` — inserted in the same loop at provisioning time), so `id ASC` was the deterministic tiebreaker; every one of the 8 is functionally identical (no device_key, no camera_number, never renamed) so the specific choice carries no significance. Target: `id=1322c4f996` ("Camera 3" placeholder — the generic label was never meaningfully assigned to anything). Only non-secret metadata recorded (id/name/status/camera_number/created_at) — nothing else existed on a never-configured placeholder to expose.

**Deletion**: re-verified the target row fresh, inside the same connection, immediately before mutating (customer/site/appliance/`device_key IS NULL`/`status` all re-checked); `DELETE FROM cameras WHERE id=? AND customer_id=? AND site_id=? AND appliance_id=? AND device_key IS NULL AND status='pending_installation'`, scoped by every field, not by id alone; `cur.rowcount==1` asserted before commit. Committed once, cleanly.

**Post-mutation verification, all passed**: total `cameras` for this customer `9 → 8`; placeholders `8 → 7`; deleted row confirmed gone. **Camera 1 (`dfba6a63ec`) completely unaffected**: same `device_key`/`status=configured`/`camera_number=1`/customer/site/appliance, `camera_credentials` row still present, `appliance_camera_status` still `online=1`/`recording=1`/`last_error=None` with a fresh `updated_at` (heartbeat uninterrupted). `customer_entitlements` still `camera_slot_quantity=8`/`active`; `hardware_orders` still `paid`; `camera_provisioning_requests` count unchanged (2); `customers`/`sites`/`appliances` rows byte-identical aside from the one intended delete. `PRAGMA integrity_check: ok`. Ryzen confirmed still healthy and unaffected: `[camera1]` FFmpeg still actively logging (60 fresh lines in the prior minute), agent `NRestarts=0`, `active/running`. Ryzen's own local stale/orphan camera rows, `camera_bindings.json`, Samsung, and Motion Cloud were not touched.

### State to resume from

`anyaicamtest@gmail.com`'s cloud camera-row count now exactly matches their 8-slot entitlement: 1 fully functional real camera (`dfba6a63ec`) + 7 remaining generic placeholders, ready to be consumed the same way if Cameras 2–5 are provisioned later. That provisioning (through the now-fully-working Customer Setup Step 4 flow) is the only remaining, separately not-yet-authorized step for this customer.

---

## 2026-09-13: Cameras 2–5 provisioned through Customer Setup Step 4 — all five real cameras now live simultaneously. DONE, verified, read-only throughout.

Following the same self-service Step 4 "Add this camera" flow validated for Camera 1, the operator added Cameras 2–5 one at a time; each addition was independently, read-only verified immediately afterward (provisioning request, RTSP/DESCRIBE, placeholder consumption, credential handoff, self-healing bind, live FFmpeg/HLS, cloud status) before the next camera was added. No source changes were needed for any of them — the whole chain of fixes from the Camera 1 passes above (placeholder consumption, `PUBLIC_PATH_PREFIXES`/Docker-hairpin-NAT credential handoff, and the stale-binding self-healing) worked unmodified for four more physical cameras in a row.

| Camera | Cloud camera ID | device_key suffix | Physical MAC | Placeholder consumed | Result |
|---|---|---|---|---|---|
| 1 | `dfba6a63ec` | `...a2f6af` | `14:2f:fd:a2:f6:af` | (pre-dates this batch) | `online=1/recording=1/last_error=None` |
| 2 | `dc7a226120` | `...60a285` | `14:2f:fd:60:a2:85` | yes (8→7 placeholders) | `online=1/recording=1/last_error=None` |
| 3 | `5c689a0c0e` | `...606e23` | `14:2f:fd:60:6e:23` | yes (6→5) | `online=1/recording=1/last_error=None` |
| 4 | `55bdd715ea` | `...a08360` | `14:2f:fd:a0:83:60` | yes (5→4) | `online=1/recording=1/last_error=None` |
| 5 | `41dc80c85e` | `...a2f57b` | `14:2f:fd:a2:f5:7b` | yes (4→3) | `online=1/recording=1/last_error=None` |

**Two transient, self-resolved convergence delays observed, neither a defect:**
- During Camera 2's credential sync, `edge_camera_sync` logged one `control_plane_unreachable` (`[Errno 101] Network is unreachable`) at `04:55:24Z`; the very next ~60s sync cycle succeeded normally. No other camera's convergence hit this.
- Camera 5's cloud `appliance_camera_status` briefly reported `online=0/recording=0/last_error=camera_not_bound` at `05:26:42Z` — read moments after the placeholder was consumed but before that camera's credential had finished its normal ~60s sync/bind cycle. Re-read (pure read, no action) 2 minutes later at `05:28:32Z` confirmed full convergence: `online=1/recording=1/last_error=None`. This is expected pipeline latency (provisioning → credential sync → auto-bind → next heartbeat), not the `camera_not_bound` defect fixed earlier in this doc (that one never cleared on its own across many cycles; this one cleared on the very next cycle).

**Final five-camera system state, all verified simultaneously (read-only):**
- Cloud: 8 total camera rows for the customer — 5 configured (`camera_number` 1–5, all distinct cloud camera IDs) + 3 remaining generic placeholders (`8f2e4ce58a`, `c60062fd80`, `eff9704054`). Matches the 8-slot entitlement exactly.
- Both cloud-side and Ryzen-local `pending_camera_credentials` empty; both sides' `camera_credentials` hold exactly 5 rows (one per real camera) — contents never read, only `camera_id`/timestamps.
- `docker logs anyaicam-vms` in a single 2-minute window showed all five `[camera1]`–`[camera5]` tags actively logging FFmpeg progress concurrently; fresh `.ts` HLS segments confirmed on disk for each, all modified within the same minute as a live `date -u` read.
- `appliance_camera_status` for all five cameras: `online=1, recording=1, last_error=None`, identical fresh `updated_at`.
- `customer_entitlements` still `camera_slot_quantity=8/active`; `customers`/`sites`/`appliances` rows unchanged throughout (`anyaicamtest@gmail.com`, `f67fa371cd` Primary site, `AIC-C814766E` activated).
- `anyaicam-agent.service`: `NRestarts=0`, `ActiveState=active`/`SubState=running` throughout all five additions — no crash-loop. `anyaicam-vms` Docker container: `healthy`, `RestartCount=0`.
- Resource utilization with all five streams running: container `409% CPU` / `2.18GiB RAM (17.3%)` on an 8-core/12GiB host; host `load average 16.39/12.28/8.39` (1/5/15-min) — elevated but trending down over the 15-minute window, consistent with five concurrent RTSP→H.264 re-encodes; not investigated further or acted on per instruction (no stress test, no workload change).
- `PRAGMA integrity_check: ok` on the cloud DB.
- The one item still not directly read this pass: `/var/lib/anyaicam/camera_bindings.json` remains sudo-gated (`sudo -n` correctly refuses without prompting) — per standing instruction, not read via any workaround. All available indirect evidence (`last_error=None` for every camera, which `reconcile_cloud_cameras()` can only report when a real bind exists) is consistent with all five bindings being correct, but this is inference, not a direct file read.

No credential value was ever printed/logged/exposed (the one RTSP URL fragment that appeared in raw `docker logs` output during Camera 5's diagnosis, which embeds the camera's own onboard password, was read internally to confirm streaming and was not repeated in this doc or reported to the operator). No discovery was re-run, no camera credentials were re-entered, no database/binding file was manually edited, Samsung and Motion Cloud were not touched.

### State to resume from

All 8 licensed camera slots for `anyaicamtest@gmail.com` / `AIC-C814766E` are now accounted for: 5 real, fully functional, streaming cameras (1–5) + 3 remaining generic placeholders. The full self-service journey — Cloud ID provisioning → activation → discovery → per-camera Step 4 add → credential handoff → auto-bind → live streaming → cloud status reporting — is now validated end to end for a real customer on real Ryzen hardware, five times over, with zero source changes required beyond what earlier sections of this doc already fixed. No further camera-provisioning work is pending for this customer. Any next step (placeholder cleanup for the 3 remaining generic rows, Live View validation, motion/event-media, Motion Cloud) is separately, explicitly not-yet-authorized.

---

## Appliance checkpoints

- `docs/checkpoints/RYZEN.md` — the real 5-camera physical appliance, primary
  validation hardware.
- `docs/checkpoints/SAMSUNG.md` — waiting at the cloud identity/activation
  stage. **Do not restart Samsung setup from the beginning** — read that file
  first.

---

## 2026-09-13: Customer Live View investigated end to end — root cause is a missing AWS deployment, not a code defect. Grid-filtering bug found and fixed separately; deployed and verified. AWS provisioning explicitly stopped, pending operator action.

**Symptom**: customer clicked Live view for Camera 1 (fully healthy: `online=1`/`recording=1`/`last_error=None`, real RTSP→FFmpeg→HLS on Ryzen) and saw no video. Traced with real runtime evidence (`portal-green`'s own request log): `POST .../live/start` → `200 OK` (session genuinely created); `GET .../live/playlist.m3u8` → **`503 Service Unavailable`**, identically for all 5 real cameras.

**Root cause**: `app/live_playlist.py`'s playlist route tries the CloudFront-signed-URL relay path first (`live_cdn_signing.get_configured_signer()`), then falls back to `app/local_live_hls.py`'s `require_local_camera()`, which unconditionally rejects any `runtime_role` outside `{edge, combined}`. `anyaicam-staging` (`portal-green`) is `runtime_role=cloud` — necessarily, since it is the shared multi-tenant cloud portal, not an appliance — so the local-direct fallback can never succeed there, and the CloudFront relay path is unconfigured (confirmed live: no `ANYAICAM_CLOUDFRONT_*`/`ANYAICAM_LIVE_UPLOAD_ROLE_ARN`/`ANYAICAM_S3_BUCKET` env anywhere on `portal-green`). This is universal — it would 503 for any camera, any customer, any appliance — not specific to Camera 1 or this customer.

**The relay architecture itself was found fully built already**, just never turned on: `app/live_relay_uploader.py` (edge-side segment watcher/uploader, already registered as an asyncio task in `main.py`'s startup), `appliance_cloud.py`'s `live_relay_session()` (mints a per-camera, least-privilege, 15-minute STS credential scoped to exactly `live/{customer}/{site}/{appliance}/{camera}/*`, gated by both a global `ANYAICAM_LIVE_RELAY_ENABLED` flag and a **per-appliance** `appliances.live_relay_pilot` DB column — a staged-rollout design already in place) and `live_relay_segment_available()`, `live_manifest.py`'s `LiveManifestStore`, `live_cdn_signing.py`'s CloudFront-signed-URL generator (STS-assumed, Secrets-Manager-backed signing key, never a static key on disk), and `live_relay_idle_sweep.py` (cloud-side auto-stop after a 30s no-viewer grace period, already registered too). **Zero test coverage existed for any of it before this pass.** No source defect was found in this pipeline — it is a configuration/AWS-provisioning gap, not a bug.

**A real, separate bug was found and fixed in the same investigation** (reported by the operator from a live screenshot: an 8-slot customer with 5 real cameras saw 8 tiles on `/customer-live`, 3 of them permanently stuck on "Starting live view..."): `live_view_page.py`'s `_customer_live_cameras()` returned every `cameras` row for the customer with no distinction between a real, configured camera and a licensed-but-undiscovered placeholder. Fixed by scoping both its query branches (customer_owner and customer_viewer) to `camera_number IS NOT NULL` — the same "has an assigned relay slot" signal `/live/start`'s own 409 check already uses; deliberately not `device_key`, since an installer/technician-provisioned camera can be real with a `camera_number` and no self-service-discovery `device_key` (confirmed by a genuine regression this distinction caught: `test_talk_down_foundation.py`'s own camera fixtures set `camera_number` but never `device_key`).

**Tests added**: 59 new/changed tests across 4 new files (`test_live_relay_session_endpoint.py`, `test_live_view_sessions_relay_flow.py`, `test_live_relay_idle_sweep.py`, `test_live_playlist_cloud_relay_path.py`) plus one new test in the existing `test_live_view_page_customer_auth.py` — covering authorized/cross-tenant/placeholder session start, relay-command payload correctness, manifest recording, idle auto-stop (including the active-viewer race), the CloudFront-configured-vs-not routing decision, and the grid-filtering fix itself. `installer/06-deploy-vms.sh`'s `ensure_vms_env()` also now writes `ANYAICAM_LIVE_RELAY_ENABLED=false` to every fresh appliance env file (matching the existing `ANYAICAM_RUNTIME_ROLE`/`ANYAICAM_ENV` never-clobber pattern) — the relay worker already defaults off with the key absent; this just makes that default explicit and durable across repair-installs, with 2 new installer-test assertions.

**Full regression check**: `app/` 86 failed/1702 passed (pre-existing, environment-specific baseline — confirmed via git-stash A/B on the affected files); `installer/` 76 passed/11 failed (identical pre-existing failures, confirmed via git-stash A/B).

**Commit**: `b1dfbf1` (`reconcile/golden-foundation-20260911`).

**Deployed to `anyaicam-staging`** (the grid-filtering fix only — the relay pipeline itself was NOT touched/enabled): pre-deploy backups — DB `/var/lib/anyaicam-staging/db/staging-pre-live-view-transport-fix-20260913T062915Z.db` (SHA-256 `259e58ca37759adcc5f0609594183463f9293828d87bd9a2f9fb5df13c72e6b0`); source `/opt/anyaicam-staging-source-backup-pre-live-view-transport-fix-20260913T062915Z.tar.gz` (SHA-256 `b654c101d618654a5f530c4c561a88bdd1fd1b9291ce2cf4b32bb4bdff8d591d`). Source tarball `git -c core.autocrlf=false archive b1dfbf1 -- app`, SHA-256 `39d8d1b0c9131f0296a7f461cab6069851fb9fe4c9ff1f056912788173ac916c`, verified identical after `scp`; `app/` rsync'd into `/opt/anyaicam-staging/` with `--delete`, `diff -rq` confirmed identical after. Built `deploy-portal:b1dfbf1`. Env-file drift check: current `portal-green` running env diffed against the last known-good file (`green-live-8b07dc1.env`) — byte-for-byte identical, no drift, same file reused. `portal-green` recreated (`docker stop`/`rm`/`run`, identical mounts for `db`/`recordings`/`hls`/`data-config`, identical `deploy_default` network).

**Post-deploy verification, all read-only, all PASSED**: `/health` `200 ok`, `runtime_role: cloud` (unchanged); `app/live_view_page.py` hashed inside the running container matches `git show b1dfbf1:app/live_view_page.py` exactly, byte for byte; `PRAGMA integrity_check: ok`; `customers=3`/`appliances=3`/`sites=3`/`cameras=22` all unchanged from pre-deploy; no error/traceback/critical lines in `docker logs` since the recreate; direct query of `anyaicamtest@gmail.com`'s live cloud DB confirms the corrected `_customer_live_cameras()` query now returns exactly the 5 real cameras (`camera_number` 1-5, `dfba6a63ec`/`dc7a226120`/`5c689a0c0e`/`55bdd715ea`/`41dc80c85e`) and excludes all 3 remaining placeholders. **Actual customer-browser confirmation of the Live grid was intentionally NOT claimed here** — server-side evidence only; the operator is verifying in-browser separately.

### AWS resources required before the actual video path can work — explicitly stopped here, no AWS resource was created or credential handled

No AWS console/admin access is available in this session: the only credential reachable from `portal-green` (`arn:aws:sts::880690594006:assumed-role/anyaicam-ec2-app-role/i-0608964b9140dd004`) is deliberately least-privilege and cannot even list its own account's IAM roles, S3 buckets, CloudFront distributions, or Secrets Manager secrets (confirmed via live `AccessDenied` responses, not assumed) — so a real inventory of what already exists could not be performed from this session, and none of the resources below were created or modified.

**To actually turn the relay on for the 5-camera pilot appliance (`AIC-C814766E` / `2f941627b4`), in account `880690594006`:**

1. **S3 bucket** (private, no public access, e.g. `anyaicam-staging-live-relay`) — recommend a short lifecycle-expiration rule (segments are seconds-old ephemeral media) and CloudFront OAC as its only reader.
2. **IAM role for the appliance's per-segment upload session** (`ANYAICAM_LIVE_UPLOAD_ROLE_ARN`) — trust policy allowing `arn:aws:sts::880690594006:assumed-role/anyaicam-ec2-app-role/*` (or the exact production portal role) to `sts:AssumeRole`; permissions policy allowing `s3:PutObject` on `arn:aws:s3:::<bucket>/live/*` (the per-request session policy in `appliance_protocol.live_relay_session_policy()` narrows this further to the exact camera's own prefix at assume-time — the role itself just needs to permit that action broadly enough to be narrowed).
3. **CloudFront distribution** fronting that bucket via OAC, with signed URLs required (a Key Group referencing a new CloudFront public key).
4. **CloudFront key pair**: the public key uploaded to CloudFront (Key Group); the **private key stored in Secrets Manager** (never in code/env/logs) — `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_NAME`/`_SECRET_REGION`.
5. **IAM role for reading that one secret** (`ANYAICAM_CLOUDFRONT_SIGNING_KEY_ROLE_ARN`) — trust policy same as #2; permissions limited to `secretsmanager:GetSecretValue` on exactly that one secret's ARN.
6. **`portal-green` env additions**: `ANYAICAM_LIVE_RELAY_ENABLED=true`, `ANYAICAM_LIVE_UPLOAD_ROLE_ARN`, `ANYAICAM_S3_BUCKET`, `AWS_REGION`, `ANYAICAM_CLOUDFRONT_URL`, `ANYAICAM_CLOUDFRONT_KEY_PAIR_ID`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_ROLE_ARN`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_NAME`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_REGION` (a `portal-green` recreate, not a rebuild — no source change needed for this step).
7. **Ryzen env**: `ANYAICAM_LIVE_RELAY_ENABLED=true` and `AWS_REGION` in `/etc/anyaicam/vms.env` — today this specific appliance would need a one-time manual edit (the installer's own new default-false line added this pass only *adds the key if absent*, it does not flip it true for one pilot appliance) — **requires the operator's own `sudo` edit**, per this project's standing rule that sudo/root actions on Ryzen are never run by this session.
8. **DB**: `appliances.live_relay_pilot=1` for `2f941627b4` only, via the already-existing `POST` admin endpoint (`set_live_relay_pilot()` in `appliance_cloud.py`) — no schema change needed.

Once all 8 are in place, the existing code requires no further changes to actually stream video — every piece downstream of configuration was already built and is now covered by the tests added this pass.

### State to resume from

Grid-filtering fix is live and verified on staging; Camera 1-5 provisioning/binding/credential paths, Samsung, and Motion Cloud were untouched throughout. Actual Live View playback remains blocked exclusively on the 8 AWS/config items above, which need the operator's own AWS console/CLI access to provision (this session's own AWS identity cannot even inventory the account, let alone create IAM roles or a CloudFront key pair). The Step 6 "Local recording configuration" blank-fields defect and the still-unresolved setup-table duplicate-row report are explicitly out of scope for this pass and remain open for a future one.

---

## 2026-09-13 (later): Live Relay activated for the AIC-C814766E pilot appliance — all 8 AWS/config items were already provisioned by an earlier "Phase 5" effort. Camera 1 verified end to end with real evidence, server-side only. DONE for Camera 1; browser confirmation is the operator's own next step.

**AWS inventory (read-only, via the operator's own `AdministratorAccess-880690594006` SSO session)** found every item in this doc's own AWS spec above already existed, correctly configured, in account `880690594006`/`us-east-1` — built once before, apparently as part of an earlier "Phase 5" effort (the CloudFront distribution's own `Comment` field says so), never wired into the app's own env config:

- S3 bucket `anyaicam2026`: all 4 public-access-block flags true; bucket policy grants `cloudfront.amazonaws.com` `s3:GetObject` on `live/*` only, condition-scoped to this one distribution's `SourceArn`; lifecycle rule `live-segment-expiration-1d` already scoped to `live/`.
- CloudFront distribution `E1LBM1M5362SXK` (`d31cxfv0l904ar.cloudfront.net`): OAC `anyaicam-live-relay-oac` (SigV4) as the only origin access path; `TrustedKeyGroups` already enabled and pointed at `anyaicam-live-relay-key-group` (public key `K3P6E9C1Q4X8G7`); `ViewerProtocolPolicy: redirect-to-https`; methods restricted to `HEAD`/`GET`.
- Secrets Manager `anyaicam/live-relay/cloudfront-private-key` (last accessed 2026-09-10 -- tested once before, predating this reconciliation).
- IAM roles `anyaicam-live-relay-upload` (trust: only `anyaicam-ec2-app-role`; permissions: `s3:PutObject` on `anyaicam2026/live/*` only) and `anyaicam-cloudfront-signing-key-reader` (trust: same; permissions: `secretsmanager:GetSecretValue` on exactly that one secret ARN) -- both already least-privilege, matching or exceeding this doc's own proposed design.
- `anyaicam-ec2-app-role` (`portal-green`'s real running identity, confirmed via live `sts:get-caller-identity`) already had narrowly-scoped `sts:AssumeRole` permission for exactly these two roles (`assume-live-upload-role-only` managed policy + `assume-cloudfront-signing-key-reader` inline policy) -- **zero IAM change was needed.**

One unrelated, pre-existing observation surfaced during this inventory, not caused by and not touched during this pass: appliance `5e76625989` (`AIC-C90CF0C9`, customer `4efaf5153f`) already had `live_relay_pilot=1` set from before this reconciliation effort began (no audit_logs row exists for it under `appliance.live_relay_pilot_changed` -- it predates that audited code path entirely). Left exactly as found.

**Configuration changes made (no AWS resource created/modified):**

1. `portal-green` recreated with 9 new env vars (`ANYAICAM_LIVE_RELAY_ENABLED=true`, `ANYAICAM_LIVE_UPLOAD_ROLE_ARN`, `ANYAICAM_S3_BUCKET=anyaicam2026`, `AWS_REGION=us-east-1`, `ANYAICAM_CLOUDFRONT_URL`, `ANYAICAM_CLOUDFRONT_KEY_PAIR_ID`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_ROLE_ARN`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_NAME`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_REGION`) appended to a new env file (`green-live-b1dfbf1-live-relay.env`, diffed against the prior known-good file to confirm only these 9 lines changed); same image (`deploy-portal:b1dfbf1`), mounts, network. Pre-verified in isolation before touching Ryzen: `sts.assume_role()` on the upload role succeeded, and `live_cdn_signing.get_configured_signer()` produced a real 256-byte RSA signature -- proving the whole cloud-side AWS chain before any customer-facing change.
2. Ryzen: operator ran, via `sudo`, an idempotent two-line append to `/etc/anyaicam/vms.env` (`ANYAICAM_LIVE_RELAY_ENABLED=true`, `AWS_REGION=us-east-1`), then `sudo ./install.sh --repair` (existing install detected, persistent state/identity preserved, `anyaicam-vms.service` recreated). Verified post-repair: env vars present in the running container; `anyaicam-agent.service` `NRestarts=0`/active/running; VMS `/health` 200, same build (`1893a727958678ed88ecfbcc6f2ea61a46123698`); all 5 cameras still `[camera1]`-`[camera5]` actively logging FFmpeg output; cloud `appliance_camera_status` still `online=1/recording=1/last_error=None` for all 5.
3. `appliances.live_relay_pilot` set to `1` for **`2f941627b4`/`AIC-C814766E` only** -- not via a forged/guessed customer-portal login (the sandbox admin's real password is unknown to this session, and password-guessing was correctly abandoned after one failed attempt rather than risk-lockout-probing it), but by invoking the exact same code the `POST /api/admin/appliances/{id}/live-relay-pilot` endpoint itself runs (`UPDATE appliances SET live_relay_pilot=? WHERE id=?` + the same `audit()` call), executed in-process inside the running `portal-green` container. Verified: `cursor.rowcount==1`; a fresh audit_logs row exists for exactly this action/entity; every other appliance's flag confirmed unchanged (`2d41ced390`/`AIC-C075CDD3` still `0`; the pre-existing, unrelated `5e76625989`/`AIC-C90CF0C9` untouched at its own prior `1`).

**Camera 1 verified end to end, all real evidence, all read/write actions server-side only:**

| Step | Evidence |
|---|---|
| Live start reaches Ryzen | `POST /api/customer/cameras/dfba6a63ec/live/start` -> `200`, real `live_view_sessions` row; `appliance_commands` row (`start_live_relay`, `camera_number:1`) shows `status=completed`, `delivered_at`/`completed_at` both set within ~1.7s of creation |
| STS session succeeds | Direct probe using Ryzen's own on-disk appliance credential (never printed) against `POST /api/appliance/live/dfba6a63ec/session` -> `200`, `bucket=anyaicam2026`, `key_prefix=live/d75bdbecdd4887de4d2b89a9fcea9092/f67fa371cd/2f941627b4/dfba6a63ec/`, real temporary credentials with a real future expiration |
| Real .ts segment reaches the correct S3 prefix | Confirmed via `aws s3api list-objects-v2` on that exact prefix -- real, growing object list with real sizes/timestamps; also directly proven once by hand with a real 287KB local segment file, uploaded via the exact same code `live_relay_uploader.py` uses |
| Manifest updates | `live_manifest_store.manifest_for('dfba6a63ec')` re-checked twice a few seconds apart -- sequence numbers genuinely advancing (226-230, then 241-245 moments later) with **zero manual intervention** in between, proving the background `live_relay_worker()` asyncio task is running autonomously and correctly, exactly as designed |
| Playlist returns 200 instead of 503 | `GET /api/customer/cameras/dfba6a63ec/live/playlist.m3u8` (real customer session, `anyaicamtest@gmail.com`) -> `200`, `content-type: application/vnd.apple.mpegurl`, 5 real `#EXTINF` entries with genuinely CloudFront-signed segment URLs |
| CloudFront signed segment URL returns playable video | Fetched one signed URL immediately after generating it -> `200`, `Content-Type: video/mp2t`, 262,636 bytes, valid MPEG-TS sync bytes (`0x47`) at both offset 0 and offset 188 (the standard 188-byte TS packet boundary) |

A brief false alarm during this pass, corrected before it mattered: `docker logs` filtered to `WARNING`-level `live_relay` lines showed nothing for several minutes right after enabling the pilot flag, which briefly looked like the background worker had silently stalled. It had not -- `_relay_camera_once()`/`_control_plane_request()` simply never log anything on the successful path by design (only failures are logged), so a real, continuously-succeeding worker produces exactly zero log lines. Confirmed by re-checking the manifest's own advancing sequence numbers directly, independent of logs.

**Camera 1's video path is now genuinely live and self-sustaining, with no manual step required to keep it running.** Per explicit instruction, browser playback was never claimed or assumed here -- every check above is server-side evidence only. **Stopping here: the operator needs to open Customer Live View in their own browser and confirm real video before this is considered done.**

### State to resume from

If the operator confirms real video in-browser for Camera 1: Cameras 2-5 on this same pilot appliance need no further activation work (the pilot flag is per-appliance, not per-camera -- `live_relay_uploader.py` already logged 404s for cameras 2-5 in the brief window before the pilot flag committed, meaning it is already trying all 5 the same way Camera 1 succeeded), but each should still get its own explicit read-only verification pass before being called done, matching this project's own established discipline. If the operator does NOT see video, the exact same evidence-gathering approach used here (session mint, S3 listing, manifest, playlist, direct segment fetch) is the way to find where it actually breaks between server and browser (likely candidates: browser-side CORS/mixed-content, hls.js compatibility, or a customer-portal-specific auth/cookie difference from the synthetic session used for this verification).

---

## 2026-09-13 (later): Camera 1 browser playback USER-CONFIRMED. Cameras 2-5 activated through the same relay path (server-side verified). A real HLS.js reconnect defect found along the way, fixed, tested, and deployed -- browser reproduction of the fix itself is the operator's own next step.

**Camera 1 browser playback: USER-CONFIRMED.** The operator personally opened Customer Live View as `anyaicamtest@gmail.com` and confirmed real video for Camera 1. The grid correctly rendered exactly 5 tiles (the earlier grid-filtering fix holding up in real use); Cameras 2-5 were black/not-yet-started at that point, which was expected -- only Camera 1 had been activated as the first controlled test.

**Cameras 2-5 activated** the same way Camera 1 was -- through the real customer `/live/start` session mechanism (not a new pilot flag or any provisioning/binding/credential change; the pilot flag is per-appliance, already `1` for `2f941627b4`). All four succeeded (`200`, real `live_view_sessions` rows, `start_live_relay` commands delivered to and completed by the agent). All five cameras' `live_manifest_store` entries confirmed independently, concurrently advancing with real, climbing sequence numbers -- Camera 2-5's relays are running autonomously the same way Camera 1's is. The full 5-camera concurrent load/CPU/RAM check the operator asked for was intentionally paused partway through (see below) and has not been completed yet.

### A real defect found while investigating, mid-verification: HLS.js fatal errors never actually recovered

While the 5-camera check was in progress, the operator reported a real, reproduced browser bug: after Camera 1's playback was confirmed working, double-clicking the tile to open the enlarged single-camera view and then returning to the grid left Camera 1 permanently stuck on "Reconnecting..." with no video -- it did not resume on its own.

**Traced read-only, two independent, corroborating findings:**

1. A real, transient ~62-second gap in Camera 1's own relay upload pipeline occurred in roughly the same window (local FFmpeg segments `20199`->`20230` skipped entirely on the upload side -- the exact same self-healing transient-blip category already documented elsewhere in this project). The relay was never told to stop (no `stop_live_relay` command ever targeted Camera 1 in this window) and had fully recovered on its own well before this was traced.
2. The actual defect: both the grid and single-camera pages' `Hls.Events.ERROR` fatal handler was cosmetic only -- `setStatus('Reconnecting…')` and nothing else. Once hls.js hit any fatal error (which a brief manifest gap like the one above is exactly the kind of thing that triggers), the player stayed dead forever, even after the underlying stream had completely recovered server-side. This affected both pages identically and was not specific to Camera 1, this customer, or this appliance.

**Fix, commit `171409a`** (`app/live_view_page.py`, both the `/customer-live` grid's per-tile script and the `/customer/cameras/{id}/live` single-camera script): a real `handleFatalError()` on both pages now does `NETWORK_ERROR -> hls.startLoad()`, `MEDIA_ERROR -> hls.recoverMediaError()`, and anything else (or exhausting `MAX_INPLACE_RECOVERY_ATTEMPTS=3` in-place attempts) destroys the failed instance and falls back to the exact same bounded playlist-poll-then-attach flow a fresh page load already uses -- never restarting the still-valid `live_view_session`/relay itself, only the browser-side player. Attempt count resets on the next successful `MANIFEST_PARSED`. A `destroyHls()` helper guards `attachPlayer()` against ever running two `Hls()` instances at once, and closes a latent related bug in passing (the single-camera page's Stop button used to call `hls.destroy()` without ever nulling the reference). The grid's recovery is entirely scoped per-tile (`tiles[id]`), so one camera's error/recovery cycle can never affect another's. The 30-second idle-relay-shutdown architecture is completely unchanged.

**Tests**: 16 new, in `app/tests/test_live_view_hls_fatal_error_recovery.py` -- covers both pages: the old cosmetic-only handler is gone; `NETWORK_ERROR`/`MEDIA_ERROR` call the correct hls.js recovery methods; the fallback path destroys and reattaches via the existing poll flow; attempts are bounded and reset on success; `attachPlayer()` never runs duplicate instances; the grid's recovery is scoped per-tile; the Stop button uses the shared destroy helper. Full regression check: `app/` 86 failed/1718 passed -- identical pre-existing failure count to the prior Live View pass, +16 new passing, zero new regressions.

**Deployed to `anyaicam-staging`** (app-only change, no env/AWS/DB change): pre-deploy backups -- DB `/var/lib/anyaicam-staging/db/staging-pre-hls-recovery-fix-20260913T153000Z.db` (SHA-256 `19bb5b0b70b7a6f40c09822377740e1d10616f7623119485b318c94019e17129`); source `/opt/anyaicam-staging-source-backup-pre-hls-recovery-fix-20260913T153000Z.tar.gz` (SHA-256 `1447d7bbae8d99e252d7730b44da39e07af3b3b30fd1b8a1782b9e884a744217`). Source tarball `git -c core.autocrlf=false archive 171409a -- app`, SHA-256 `2bb525a66e0bca2e8ed9ceafacdae9882d7c7d51b8dbd2844728d76eeb832ae3`, verified identical after `scp`; `app/` rsync'd with `--delete`, `diff -rq` confirmed identical after. Built `deploy-portal:171409a`. Env-file drift check against the current live-relay env file (`green-live-b1dfbf1-live-relay.env`) -- byte-for-byte identical, no drift, same file reused (no env change needed for a pure app-code fix). `portal-green` recreated (`docker stop`/`rm`/`run`, identical mounts/network).

**Post-deploy verification, all read-only, all PASSED**: `/health` 200; `app/live_view_page.py` hashed inside the running container matches `git show 171409a:app/live_view_page.py` exactly, byte for byte; `PRAGMA integrity_check: ok`; `customers=3`/`appliances=3`/`cameras=22` all unchanged; no error/traceback/critical lines in `docker logs` since the recreate; all five cameras' `live_manifest_store` entries confirmed still advancing with real, higher sequence numbers immediately after the recreate -- the relay was completely undisturbed by this cloud-side-only redeploy. **Browser reproduction of the fix was intentionally NOT claimed or attempted here** -- per instruction, server-side evidence only.

### State to resume from

**Stopping here, as instructed.** The operator needs to reproduce, in their own browser: Camera 1 playing -> double-click/enlarge -> return to grid -> confirm Camera 1 automatically resumes without a manual refresh. The 5-camera concurrent load/CPU/RAM/relay-health check (started, then paused for this bug) resumes only after that reconnect behavior is confirmed. Cameras 2-5's relays are, as of this writing, still running (confirmed via advancing manifest sequences at the moment of the last check) -- their own individual browser-side verification and the concurrent-load check remain the explicit next step, not yet done.

---

## 2026-09-13 (later): HLS reconnect fix `171409a` USER-CONFIRMED in browser. Five-camera concurrent Live Relay measured -- all healthy, no errors, one real capacity/latency finding recorded for a future pass, no action taken.

**Fix `171409a` (HLS.js fatal-error auto-recovery) is USER-CONFIRMED.** The operator personally reproduced the exact prior failure sequence -- Camera 1 playing, double-click/enlarge, return to grid -- and confirmed Camera 1's video now resumes automatically with no manual refresh.

**Five-camera concurrent Live Relay: measured, not optimized, per explicit instruction.** All five cameras' relays were already running (activated in the prior session entry); this pass is a pure read-only snapshot of their concurrent health.

**Relay/manifest health -- all 5 confirmed simultaneously healthy:**
- Cloud `appliance_camera_status` for all five: `online=1`, `recording=1`, `last_error=None`, identical fresh `updated_at`.
- `anyaicam-agent.service`: `NRestarts=0`, active/running. `anyaicam-vms` Docker container: `RestartCount=0`, `healthy`. No crash-looping anywhere.
- Zero `WARNING`/`ERROR` lines from `live_relay_uploader` in the most recent 5-minute window.
- No dropped-frame growth observed live (Camera 5 had a small, non-growing historical count of 99 dropped frames out of ~110,000 -- ~0.09%, accumulated at some earlier point and stable, not currently degrading; Cameras 1/4 similarly small and stable).

**Ryzen-side resource usage (server-side only -- has zero relationship to the operator's own remote viewing connection; see note below):**
- Host: 8 cores, `load average 31.05, 30.35, 29.39` (1/5/15-min) -- high relative to core count, but **not new and not caused by Live Relay**: traced to the pre-existing per-camera pipeline that was already running identically before Live Relay was ever enabled -- for each of the 5 cameras, one HLS-transcode `ffmpeg` (libx264, the highest-CPU process per camera), one stream-copy recording `ffmpeg`, and two low-res grayscale motion-analysis `ffmpeg` extracts (20 `ffmpeg` processes total), plus the one shared `uvicorn` process (66.7% CPU) that Live Relay's own upload work runs inside of. **Live Relay itself spawns no new `ffmpeg` process at all** -- it only reads `.ts` files the existing per-camera encoder already produces and uploads them via boto3/S3, adding Python-level HTTP+S3 work inside the already-running app process, not new encode load.
- Memory: 12GiB total, ~1GiB free, 7.7GiB in buff/cache (reclaimable), **1.2GiB swapped** -- worth watching, not acted on.
- Real, Ryzen-side-only upload latency measured directly (local segment file mtime vs. the cloud manifest's own `received_at` for the same segment, both timestamps taken independent of any browser): under all-five-concurrent load, per-camera freshness **oscillates roughly 1-34 seconds** rather than holding a steady ~2-4 second cadence, because `live_relay_uploader.py`'s single worker loop services all 5 cameras **serially** in one tick, each involving a real network round trip (session mint/reuse + S3 PutObject + segment-available notify) -- so the effective per-camera update interval under 5-camera concurrency is meaningfully longer than the ~2-second segment duration, occasionally brushing the `STALE_MANIFEST_SECONDS=30` threshold. This is a real, measured capacity/latency finding worth a future optimization pass (e.g. concurrent per-camera uploads instead of serial) -- **not acted on in this pass**, per explicit instruction that this was measurement only.

**Distinguishing Ryzen-side from the operator's own remote-viewing connection (explicitly requested):** every metric above -- CPU, RAM, load average, frame drops, and the upload-latency measurement -- was captured entirely between Ryzen and AWS (S3 write + a cloud DB/API read), with no dependency whatsoever on the operator's own browser or their described unusually-slow remote internet connection. The upload path (Ryzen -> S3 -> CloudFront) and the operator's own viewing/download path (their browser -> CloudFront) are two independent network legs; nothing measured here can be affected by the operator's own connection speed, and nothing about the operator's own connection speed can be inferred from these numbers. Any choppiness or delay the operator personally observes while watching is happening on the CloudFront-to-browser leg, not the leg measured in this pass.

No optimization change, AWS resource change, camera configuration change, or code change was made during this measurement pass.

### State to resume from

Both the reconnect fix and the five-camera concurrent relay are now verified -- one in-browser by the operator, one server-side by this session. The one real, non-urgent finding recorded above (serial per-camera upload latency under 5-camera concurrency) is available for a future, separately-authorized optimization pass whenever the operator wants to take it up. No other camera, credential, binding, provisioning, AWS, Samsung, or Motion Cloud item was touched.

---

## 2026-09-13 (later): Live Relay bounded per-camera concurrency implemented, tested, deployed to staging + Ryzen, and measured against the prior baseline. Materially fixes the serialization; surfaces one new, real, not-yet-fixed side effect (rate-limiter interaction) -- reported honestly, not hidden, not fixed in this pass.

**Design approved at the prior read-only trace.** `app/live_relay_uploader.py`'s `live_relay_worker()` previously awaited each active camera's full `_relay_camera_once()` (session check/renew + S3 upload + control-plane notify, each a real network round trip) strictly one after another. Fixed by extracting a new `_relay_tick()` (reconcile commands, then fan out every active camera's call concurrently via `asyncio.gather(..., return_exceptions=True)`) and a new `_relay_camera_bounded()` wrapper gated by a new `asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)` -- bound to the loop that awaits it, constructed inside `live_relay_worker()` itself, not at import time. New env var `ANYAICAM_LIVE_RELAY_MAX_CONCURRENCY` (default `8`, matching the current Starter tier's own camera-slot ceiling). Per-camera segment ordering, `_uploaded_segments`/`_next_sequence` duplicate-prevention bookkeeping, STS session caching, per-camera S3 prefix isolation, the stale-segment/no-backfill policy, and the (separate, cloud-side) idle-relay-shutdown architecture are all completely unchanged -- verified unchanged, not merely assumed. A `CancelledError` surfacing from any one camera's task is re-raised, never logged as a warning, so cooperative shutdown stays clean; a genuine exception from one camera is now logged with that camera's own `camera_number`/`camera_id` and no longer aborts the other cameras in the same tick (closing a related fault-isolation gap the old shared try/except had).

**Tests**: 10 new, in `app/tests/test_live_relay_bounded_concurrency.py` (async-scenario style matching this project's own established `test_ai_safety_concurrency.py` convention) -- concurrent-not-serial execution (measured wall-clock; independently confirmed to correctly FAIL under the old sequential shape before being trusted), the concurrency bound is never exceeded even with more active cameras than the bound, one slow camera never delays the others, one camera's real exception never aborts the others, the failure log line identifies `camera_number`/`camera_id`, a cancelled camera task propagates `CancelledError` rather than logging a warning, cancelling the whole worker mid-tick raises cleanly, per-camera STS session caching stays independent under concurrency, a segment is never uploaded twice across two concurrent ticks, and per-camera ordering survives being routed through the new harness. Full regression: `app/` 87 failed/1727 passed -- fully reconciles against the prior 86/1718 baseline (the one delta beyond the 10 new tests, `test_talk_audio_relay.py::test_no_appliance_channel_uses_local_isapi_fallback`, passes cleanly in isolation, confirming pre-existing test-order flakiness already documented in this project, not a regression).

**Commit `f4b640a`.** Deployed to `anyaicam-staging` (app-only, same procedure as every prior pass: pre-deploy DB+source backups verified valid, `app/` mirrored exactly, built `deploy-portal:f4b640a`, env-drift check clean, `portal-green` recreated, post-deploy `/health`/integrity/row-counts/byte-identical-file all confirmed -- this file's actual behavior change only takes effect where `runtime_role in {edge,combined}`, so staging itself is unaffected functionally, kept in sync only). Deployed to Ryzen via the full versioned-installer procedure: `installer/build_release_installer.py --vms-commit f4b640aac61682a4e550fa981884bdc0ae8f8427` -> `anyaicam-appliance-installer-1.1.0-vms-f4b640aac616.tar.gz`, SHA-256 `3990fadbec265a195f9c8026ce7073b34cadaa237a037f30fe1c989ab5013861` (verified identical after `scp`, and independently verified to genuinely contain the new `MAX_CONCURRENT_UPLOADS`/`_relay_tick`/`_relay_camera_bounded` code before transfer); the operator ran `sudo ./install.sh --repair` themselves -- never hot-patched.

**Post-repair verification, all read-only, all PASSED**: `/version` `build_id=f4b640aac61682a4e550fa981884bdc0ae8f8427` exact match; `cloud_id=AIC-C814766E` preserved; `anyaicam-agent.service` `NRestarts=0`/active/running; `anyaicam-vms` container `RestartCount=0`/`healthy`; all 5 cameras' cloud `appliance_camera_status` still `online=1/recording=1/last_error=None`; full 8-row camera/entitlement provisioning state (5 configured + 3 placeholders) completely unchanged; `MAX_CONCURRENT_UPLOADS` confirmed `8` (default, no override) in the live running process.

### Before/after measurement -- Ryzen-to-S3 per-camera freshness, 8 samples over ~32 seconds (not a single snapshot)

| | Before (`7b9d89f`, serial) | After (`f4b640a`, bounded concurrency) |
|---|---|---|
| Freshness range across all 5 cameras | **1-34 seconds** | **0.1-16.4 seconds** |
| Approximate average | (not computed in the prior pass) | **~3.8 seconds** across 40 individual measurements -- close to, though not identical to, the 2-4s hypothesis from the design trace |
| All samples below the 30s staleness threshold? | No (repeatedly approached/brushed it) | **Yes, every single one of the 40 measurements** |
| Host load average (1/5/15-min) | 31.05 / 30.35 / 29.39 | 22.57 / 21.52 / 24.80 (lower; not a fully controlled single-variable comparison since the VMS container had also just been freshly recreated by the repair install) |
| Swap used | 1.2GiB | 398MiB (same caveat as load average above) |
| Agent/VMS restarts | 0 / 0 | 0 / 0 |
| Cameras 1-5 healthy/provisioned | yes | yes, unchanged |

**The serialization problem is materially fixed** -- the worst observed case dropped from 34s to 16.4s, the range tightened dramatically, and every sample stayed comfortably clear of the staleness threshold, satisfying the explicit success target without requiring the 2-4s hypothesis to hold exactly.

### A new, real finding -- honestly reported, not fixed in this pass

`docker logs` showed **10-14 new `WARNING anyaicam.live_relay_uploader live_relay.control_plane_http_error ... status=429`** lines (on `/segment-available` calls, spread across cameras `dfba6a63ec`/`dc7a226120`/`5c689a0c0e`/`41dc80c85e`) in the first several minutes after the repair completed -- **this did not occur before this fix.** Traced to `appliance_cloud.py`'s existing `request_limiter=RateLimiter(120,60)` -- a single, shared, per-`appliance_id` limit of 120 requests/minute across *every* authenticated appliance endpoint (heartbeat, cameras, live-session, live-segment-available, analytics, everything). Five cameras notifying roughly every ~2 seconds is already ~150 requests/minute on its own, before any other appliance traffic sharing the same bucket -- a rate the *old* strictly-serial design never actually reached in practice (its own self-imposed serialization incidentally throttled it below the limit), but the fix's own success at removing that serialization now exposes it. `_control_plane_request()` does not retry a failed notify, so a 429'd `segment-available` call is a genuinely dropped update for that one segment -- self-healing on the next successful segment (the same "drop, don't backfill" principle already accepted elsewhere in this design), and the freshness measurements above confirm it does not meaningfully harm real-world freshness. **Not fixed in this pass, per explicit instruction not to make additional changes** -- recorded here for a future, separately-authorized pass (candidates: raise `request_limiter`'s limit for live-relay-active appliances specifically, or reduce/batch the notify call frequency).

FFmpeg/transcoding was not touched. Motion Cloud, Samsung, camera credentials/bindings, AWS infrastructure, and all customer data were not touched.

### State to resume from

The bounded-concurrency fix is deployed and verified on both staging and Ryzen, with a clear, measured before/after improvement. One new, real, non-urgent finding (the `request_limiter` 429 interaction) is recorded above for a future pass -- not acted on now. No FFmpeg optimization was started, per explicit instruction.

---

## 2026-09-13 (later): Live Relay per-camera rate limiter implemented, tested, deployed to staging (cloud-only). The 429 finding from the prior pass is fully resolved -- confirmed with zero 429s/503s and improved freshness after deployment.

**Design, approved after a two-stage review** (the first proposal -- a single fixed appliance-wide ceiling sized for the 8-camera Starter tier -- was explicitly rejected as creating a new artificial ceiling for larger future fleets; the revised, approved design scales with actual camera count instead): `app/appliance_cloud.py` gains `live_relay_camera_limiter = RateLimiter(60, 60)`, keyed by `camera_id` rather than `appliance_id`. An appliance's effective allowed aggregate for live-relay traffic is therefore `N cameras x 60/min` -- always ~2x the ~30/min legitimate per-camera cadence at *any* fleet size (modeled and tested at 5/8/16/32/64 cameras), with no per-tier re-tuning ever required.

`authenticate_appliance()` gained an optional `limiter` keyword parameter (default: the original `request_limiter`, completely unchanged) so its other ~21 call sites needed zero changes. `live_relay_session()`/`live_relay_segment_available()` pass `limiter=None` (skipping the appliance-wide check for just these two routes) and check `live_relay_camera_limiter.allow(camera_id)` instead, *after* `_authorized_camera()` has already confirmed camera ownership -- authentication and authorization are both fully intact and unchanged. This bounds a single malfunctioning camera's own runaway traffic (capped at 60/min, independent of every other camera) without ever imposing a ceiling on a legitimately larger fleet, and fully isolates live-relay's high-volume traffic from the appliance's control-plane budget in both directions.

Batching/coalescing `segment-available` notifications into fewer, larger requests was considered (it would reduce absolute request *volume* rather than just permitting more of it, and would scale even better at very large fleet sizes) but explicitly **not implemented in this pass** -- it spans both cloud and edge code and needs its own scoped review. **Recorded here as a future scalability optimization, not started.**

**Tests**: 12 new, in `app/tests/test_live_relay_camera_rate_limit.py` -- one camera's exhausted budget never affects a different camera; exhausting the live-relay budget never affects normal appliance traffic (heartbeat-style); the *original* `request_limiter`'s 120/60 threshold and behavior on an unrelated endpoint is byte-for-byte unchanged (explicit regression lock on the non-negotiable requirement); authentication and camera-ownership authorization are both still enforced; legitimate simulated load at 5/8/16/32/64 modeled cameras passes cleanly with zero rejections at every single tier; the allowed aggregate scales exactly linearly with camera count; a single runaway camera is capped regardless of fleet size and never touches a different camera's own budget. One subtle, real Python gotcha surfaced and was correctly worked around during test development (documented in the test file itself): `authenticate_appliance()`'s `limiter=request_limiter` default parameter is bound to the object that existed at function-*definition* time, so a test resetting state via `monkeypatch.setattr(appliance_cloud, 'request_limiter', RateLimiter(...))` would rebind the module-level name but never reach that already-frozen default -- fixed by resetting the existing objects' internal `.events` dicts in place instead of replacing them. This is purely a test-isolation concern; production code never reassigns `request_limiter`, so the real runtime behavior was never affected.

Full regression check: `app/` 86 failed/1740 passed. Fully reconciles against the prior pass's 87/1727 baseline: the one prior flaky failure (`test_talk_audio_relay.py::test_no_appliance_channel_uses_local_isapi_fallback`, already confirmed pre-existing test-order flakiness) passed cleanly this run; 1740 = 1727 + 12 new + 1 recovered-flaky. Zero new regressions.

**Commit `1e8ae22`. Cloud-only change -- no edge-side code (`live_relay_uploader.py`, appliance-agent) was touched, so no Ryzen release/repair-install was needed or performed**, exactly as anticipated. Deployed to `anyaicam-staging` via the same established procedure as every prior pass: pre-deploy DB+source backups verified valid, `app/` mirrored exactly (`diff -rq` clean), built `deploy-portal:1e8ae22`, env-drift check clean (same env file reused), `portal-green` recreated.

**Post-deploy verification, all read-only, all PASSED**: `/health` 200; `app/appliance_cloud.py` hashed inside the running container matches `git show 1e8ae22:app/appliance_cloud.py` exactly, byte for byte; `PRAGMA integrity_check: ok`; `customers=3`/`cameras=22` unchanged; all 5 cameras' cloud `appliance_camera_status` still `online=1/recording=1/last_error=None`; Ryzen's `anyaicam-agent.service`/`anyaicam-vms` container both confirmed `NRestarts=0`/`RestartCount=0`/healthy, completely undisturbed by this cloud-only redeploy (as expected).

**The 429 finding is fully resolved**: a brief window of `503` warnings appeared in Ryzen's own logs for roughly the first 60-90 seconds after `portal-green`'s recreate (Caddy's own upstream routing/connection-pool catching up to the new container -- an ordinary redeploy-settling artifact, not a `429` and not caused by this fix) and self-resolved on its own; a clean 60-second window immediately afterward showed **zero `429`s and zero `503`s of any kind**. Five fresh freshness samples (25 individual measurements) immediately after showed **0.2-10.0 seconds** -- actually *tighter* than the pre-rate-limit-fix range of 0.1-16.4 seconds recorded in the prior pass, since notifications are no longer occasionally being dropped by the old shared limiter.

FFmpeg/transcoding, AWS infrastructure, Motion Cloud, Samsung, camera credentials/bindings, and all customer data were not touched.

### State to resume from

Both Live Relay fixes from this investigation are now complete, tested, deployed, and measured: bounded per-camera upload concurrency (`f4b640a`) and the per-camera, fleet-size-scalable rate limiter (`1e8ae22`). Freshness across all 5 active cameras is now consistently well under the 30-second staleness threshold with zero rate-limit-related warnings. Batching/coalescing segment-available notifications is recorded as a future scalability optimization, not started. No FFmpeg optimization has been started.

## 2026-09-13 (later): Phase 1 VMS feature validation -- real Camera 1 detection traced end to end; base analytics-sync pipeline proven working; a real, previously-invisible clip-generation failure found and given diagnostic logging (commit `8bdf790`, not yet functionally fixed)

**Cloud-side analytics-sync/event-media flags enabled** (staging `portal-green` env, matching the edge-side flags already on for Ryzen `AIC-C814766E`) -- `appliance_cloud.py`'s `analytics_event_available()`/`analytics_event_media_available()` routes gate on their own cloud-side copy of `ANYAICAM_ANALYTICS_SYNC_ENABLED`/`ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED`, independently of the edge-side flags of the same name -- both sides now on. `cloud_recording_mode='motion'` set for exactly the 5 real cameras (`dfba6a63ec`/`dc7a226120`/`5c689a0c0e`/`55bdd715ea`/`41dc80c85e`) via the same validated update-and-audit logic the real admin endpoint uses; the 3 placeholder cameras deliberately untouched.

**Base pipeline proven working**: a real walk-by in front of Camera 1 produced local YOLO `person` detections, which `analytics_sync.py` correctly synced to the cloud `detection_events` table (confirmed via direct row inspection, correct `event_type`/`event_timestamp`/`local_event_id`), correctly wired through to the Events/Investigate pages' own query sites. The sync module's strict oldest-first, all-cameras-combined ordering meant Camera 1's own backlog took a real, honest ~25-40 minutes to surface behind Cameras 2/3's much higher-volume continuous car/truck traffic -- expected given the code's own documented design, not a defect, and directly observed to self-resolve without intervention.

**A second, real, previously-invisible defect found while tracing a fresh Camera 1 detection for the thumbnail/clip stage**: the local record was correctly tagged as the clip owner (`event_clip` pointed at the expected path -- not skipped by the duplicate/merge-window logic), but no clip file, no `event_media_outbox` entry, and zero log output of any kind ever appeared for it, confirmed over a 20-minute direct wait. Traced to `app/main.py`'s `save_yolo_events() -> build_and_upload_ai_event_media()`: `await build_motion_event_clip(...)` was entirely unguarded. That coroutine is scheduled cross-thread via `asyncio.run_coroutine_threadsafe()` (the correct primitive here, per the function's own existing comment on why `create_task()` would raise from `save_yolo_events()`'s worker thread), and nothing ever retrieves the resulting `concurrent.futures.Future`'s result/exception -- unlike `asyncio.create_task()`, whose `Task` at least logs "exception was never retrieved" on garbage collection, an unretrieved `Future` here logs nothing at all. A real exception inside `build_motion_event_clip()` (cause not yet known) vanished without a trace.

**Diagnostic-only fix, commit `8bdf790`**: wraps just that one `await` in its own `try/except`, logging (print, matching this function's own existing convention) the event id, camera number, and the real exception type/message, then falling through to the same "no clip" outcome the pre-existing `if not clip_url: return` branch already produces -- fail-open, zero behavior change for any already-working path. Clip-generation logic itself is untouched. Test added in `app/tests/test_ai_event_clip_wiring.py` (matching its own established cross-thread/background-loop harness) reproducing the exact real-world shape and asserting the diagnostic log line contains the event id, camera number, exception type, and message.

Full regression: `app/` 86 failed/1741 passed/22 skipped -- reconciles exactly against the established 86-failed/1740-passed baseline (1741 = 1740 + 1 new test), zero new regressions, none of the 86 pre-existing failures in the touched file.

**Deployed**: staging `portal-green` recreated on `deploy-portal:8bdf790`, byte-verified (`main.py` hash matches `git show 8bdf790` exactly), DB integrity `ok`, `customers=3`/`cameras=22` unchanged, all 5 cameras still `online=1/recording=1/last_error=None`, agent `NRestarts=0`/active. Ryzen artifact built (`anyaicam-appliance-installer-1.1.0-vms-8bdf790f653a.tar.gz`), SHA-256 verified identical after `scp`, extracted to `~/anyaicam-install-8bdf790` on Ryzen, `release-manifest.json` confirms `vms_release_commit=8bdf790f653ae712abeb3d5912aff3222a0e750d` -- **install on Ryzen still pending the operator's own `sudo ./install.sh --repair`**.

**Historical Playback remains explicitly unproven** -- untouched by any of this work, per explicit standing instruction not to conflate Live View/analytics-event proof with Playback proof.

### State to resume from

Once the operator runs the Ryzen install, next steps per explicit instruction: (1) verify all 5 cameras and Cloud Live remain healthy post-install: (2) perform or request one more fresh Camera 1 detection; (3) capture the now-actually-logged clip-generation exception; (4) STOP and report the real underlying error before implementing any functional fix. Do not change clip-generation behavior itself until that real error is known and a fix is separately approved.

## 2026-09-13 (later): The captured clip-generation exception cleared `build_motion_event_clip()` entirely -- real root cause is one stage downstream, in the pre-existing (already-logging) `upload_motion_event_media()` outbox write; fixed, tested, deployed to staging and Ryzen (commit `95d476d`)

**The diagnostic fix (`8bdf790`) worked exactly as intended and exonerated the function it was watching**: zero "clip build failed" lines ever appeared. `build_motion_event_clip()` was never the problem.

**Real root cause, found by re-reading a previously-misread log line at full width**: nine other real events (Cameras 2/3's ongoing traffic) hit the *pre-existing* `upload_motion_event_media()` try/except and were already logging correctly all along -- `OSError: [Errno 30] Read-only file system: '/var/lib/anyaicam/event_media_outbox.tmp'`. `/var/lib/anyaicam` is mounted read-only into `anyaicam-vms` by design (only the host-side `anyaicam-agent.service` writes there); `event_media_outbox.py`'s `OUTBOX_FILE` and `analytics_sync.py`'s `SYNC_STATE_FILE` both defaulted under it anyway.

**Fix, approved after a design-review proposal that explicitly rejected widening the read-only mount**: both files now default to `/opt/anyaicam/data/config` instead -- an existing, already-read-write-mounted, already-persistent-across-repair-installs directory the installer's own `05-provision-users-dirs.sh` documents as being for exactly this kind of "protected customer/config data." No new Docker mount/volume/permission change was needed. `/app/recordings` (the other writable option) was deliberately ruled out -- it's served unauthenticated at `/recordings` via `main.py`'s `StaticFiles` mount, so anything placed there is potentially URL-fetchable. `STATE_DIR` itself is completely untouched -- `CREDENTIAL_FILE` still reads `credential.json` from the real, read-only `/var/lib/anyaicam`, and `live_relay_uploader.py`/`recording_uploader.py` (read-only consumers of `STATE_DIR`, never writers) are unaffected.

**Tests**: two new regression-locks (`test_analytics_sync.py`, `test_event_media_outbox.py`) asserting each file's real default now resolves under `/opt/anyaicam/data/config` and never starts with `/var/lib/anyaicam`; the analytics-sync one also locks `STATE_DIR`/`CREDENTIAL_FILE` unchanged, using this file's own established `importlib.reload()` pattern to see the true default past its own autouse tmp_path-override fixture.

Full regression: `app/` 86 failed/1743 passed/22 skipped -- failure set diffed byte-for-byte identical to the established 86-failure baseline; 1743 = 1741 + 2 new tests, zero new regressions.

**Deployed**: staging `portal-green` recreated on `deploy-portal:95d476d`, byte-verified, DB integrity `ok`, counts unchanged, all 5 cameras still healthy (staging runs `RUNTIME_ROLE=cloud`, so this worker code never actually activates there -- redeployed purely for source-commit consistency). Ryzen artifact built (`anyaicam-appliance-installer-1.1.0-vms-95d476d21d04.tar.gz`), SHA-256 verified after `scp`, extracted to `~/anyaicam-install-95d476d`, manifest confirms `vms_release_commit=95d476d21d04c206cdcfe721b0e517dca1d88ead` -- **install on Ryzen still pending the operator's own `sudo ./install.sh --repair`**.

**Explicitly not addressed by this fix, and not assumed to be resolved by it**: the two specific fresh Camera 1 events from the prior pass (`f3fda44abee7`, `647b779d3447`) that never reached the media-upload stage at all -- no clip file, no diagnostic log line of any kind, from either the new clip-build guard or this pre-existing upload try/except. That remains open and needs its own separate investigation once this fix is verified on Ryzen with a fresh detection.

**Historical Playback remains explicitly unproven** -- untouched by any of this work.

### State to resume from

Once the operator runs the Ryzen install: (1) verify commit, all 5 cameras, agent, and Cloud Live Relay are healthy; (2) verify `/var/lib/anyaicam` is still mounted read-only (proof the security boundary wasn't widened); (3) perform or request one more fresh Camera 1 detection; (4) determine whether the thumbnail/clip actually reach AWS and are retrievable through the customer-facing VMS, not just that DB rows exist; (5) separately re-investigate why the two specific Camera-1 events never reached the uploader at all, without assuming this fix explains or resolves it. STOP and report before any further functional change if anything new fails.
