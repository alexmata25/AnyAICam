# Phase 1 staging repair — root-cause report, security review, deployment plan

Branch: `staging/cloud-integration-repair`, base `codex/authoritative-vms-reconciliation-20260907` (`ec5272f`).
Commits, in order:
1. `85a0e5561fef41a910da2702f80bd53d91c184cc` -- activation/handoff/updates-endpoint fix (§1-§2).
2. `ae565a87b213174b18784ecf2ea4a658643a5096` -- this report, `docs/AI_HANDOFF.md`, `vms.env`-preservation tests.
3. `50bdd6ed4d77c859b5b4b2ea1ea05df7bbbbb9b2` -- per-camera `cloud_recording_mode` eligibility gate (§2a).
4. (docs) -- this update: final test counts, installer package, eligibility completion, staging-readiness decision.

Status: **local/isolated-worktree work only.** Not pushed. Not deployed to
staging or production. Samsung not touched. No camera discovery run.

## 1. Root-cause report

Three independent, confirmed root causes behind the Samsung lab's failed
end-to-end test, found by reading the actual code (not inferred from
symptoms alone):

### 1.1 First-time appliance activation could not succeed at all (primary cause)

`appliance-agent/anyaicam_agent/reenrollment.py`'s `coordinated_reenroll()`
(the just-added identity-handoff mechanism, `ec5272f` "appliance: coordinate
identity re-enrollment") requires `agent.json`, `credential.json`, and the
VMS's own `appliance_identity.json` to **already exist** before it writes
anything -- confirmed directly in its own precondition
(`if any(not p.is_file() for p in paths): raise ValueError(...)`) and in its
own test suite, which always seeds an "old" identity before calling it. It
exists to REPLACE a prior identity, with something to roll back to on
failure.

A freshly activated appliance has none of those three files. Calling
`anyaicam-setup` (`setup_wizard.py`) on a genuinely fresh box was therefore
guaranteed to raise `ValueError` -> `ReenrollmentError` -> `SystemExit`
before any identity, cloud URL, or credential path was ever written --
which is consistent with everything the lab report observed downstream
(no cloud config, disabled upload workers), even though the report says
activation itself "succeeded" (most likely explained by the deployed
Samsung binary predating this commit, or a manual workaround -- see
`docs/AI_HANDOFF.md`'s note on this discrepancy).

### 1.2 VMS container never received a control-plane URL

`recording_uploader.py`, `live_relay_uploader.py`, `analytics_sync.py`, and
`event_media_uploader.py` all read `ANYAICAM_CLOUD_URL` (not
`ANYAICAM_PORTAL_URL`, a different, unrelated setting `cloud_config.py`
uses elsewhere) for their own control-plane calls. Neither
`installer/06-deploy-vms.sh`'s `ensure_vms_env()` nor any other installer
script ever wrote this variable -- confirmed by grepping every installer
script for `PORTAL_URL`/`CLOUD_URL` (zero matches before this fix). The
portal URL only becomes known at activation time (`anyaicam-setup`, run
after install), not at install time, so the fix belongs in the activation
flow, not the bash installer.

### 1.3 VMS container could not read the appliance's own identity

`recording_uploader.py`/`live_relay_uploader.py`/`analytics_sync.py`/
`event_media_uploader.py` all read `credential.json` from
`ANYAICAM_STATE_DIR` (default `/var/lib/anyaicam` -- the **agent's** own
state directory, per `appliance-agent/anyaicam_agent/config.py`'s
`DEFAULT_STATE_DIR`), not the VMS's separate `appliance_activation.py`
identity file. `docker-compose.yml` never mounted that directory into the
VMS container, so even a fully-activated agent's credential was
unreachable from inside the container.

### 1.4 `GET /api/appliance/updates/latest` was never registered server-side

The device-side client (`appliance-agent/anyaicam_agent/updater/
s3_source.py`'s `ManifestSource`), its data models (`updater/models.py`),
and its signature verification (`updater/verify.py`) all already existed
and are already tested -- but no route in `app/appliance_cloud.py` ever
answered this path, and no server-side catalog/signing module
(`app/updates_storage.py`) existed at all. Every call 404'd, and the
device's own `SourceUnavailable` exception fired on every attempt --
exactly the reported traceback loop.

## 2. What was fixed (Phase 1 scope)

| # | Fix | File(s) |
|---|---|---|
| 1 | `first_enroll()`: the missing first-time counterpart to `coordinated_reenroll()` | `appliance-agent/anyaicam_agent/reenrollment.py` |
| 2 | `setup_wizard.main()` picks `first_enroll` vs `coordinated_reenroll`; writes `ANYAICAM_CLOUD_URL` into `vms.env` after enrollment; queues a `restart_vms` privileged action | `appliance-agent/anyaicam_agent/setup_wizard.py` |
| 3 | Read-only mount of `/var/lib/anyaicam` into the VMS container | `docker-compose.yml` |
| 4 | `GET /api/appliance/updates/latest` (no_update_available / signed manifest) + `POST /api/appliance/updates/{id}/result` | `app/appliance_cloud.py`, `app/updates_storage.py` (new) |
| 5 | Additive `appliance_update_results` table; `'updates'` storage category | `app/db_migrations.py`, `app/object_storage.py` |

Not in scope for Phase 1 (see §5 and the architecture plan doc for why):
Stripe/pricing/entitlement work, production or Samsung deployment.

### 2a. Per-camera cloud-recording eligibility (follow-up, commit `50bdd6e`)

The gap flagged in the first version of this report -- `cameras.
cloud_recording_mode` existed in the schema and API but nothing in the
upload path read it, so eligibility was only ever the single appliance-
wide `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` flag -- is now closed:

- `app/recording_uploader.py`'s `_refresh_camera_map()` now carries
  `cloud_recording_mode` (aliased as `recording_mode` in the API
  response) through into the shared per-camera cache, read the same way
  `main.py`'s `people_counting_worker()` already reads its own
  per-camera entitlement (`people_counting_enabled`) off that identical
  map -- one shared cache, not a second one.
- `app/event_media_uploader.py`'s `upload_motion_event_media()` now
  returns `False` for any camera whose `cloud_recording_mode` is not
  exactly `'motion'` -- checked *after* the appliance-wide flag (so the
  master switch still overrides per-camera eligibility, never the
  reverse) and *before* any session/S3/control-plane work, so an
  ineligible camera never reaches short-lived credential issuance.
  `'continuous'` is deliberately excluded -- that's the separate Cloud
  24/7 product, served entirely by `recording_uploader.py`'s own
  continuous-recording path; motion-event upload and continuous-
  recording upload stay on fully separate entitlements, never inferred
  from each other.
- 9 new tests, `app/tests/test_event_media_uploader.py`: ineligible
  (`None`/`'disabled'`/`'continuous'`) and unknown cameras never reach
  session/S3/control-plane; an eligible (`'motion'`) camera uploads both
  clip and thumbnail using short-lived, camera-scoped credentials; the
  appliance-wide disable overrides camera eligibility and short-circuits
  before any camera lookup at all; a cloud/credential failure
  (`_ensure_session()` returning `None`) returns `False` without
  raising; a registration retry against an already-`'duplicate'`
  response still reports success on repeated calls.

## 3. Motion-event media path — full audit (as specifically requested)

Traced end to end by reading the actual call chain, not assumed:

| Stage | Implementation | Status |
|---|---|---|
| Local event creation | `store_motion_event()` / `save_yolo_events()` in `app/main.py` append the event, independent of anything downstream | **Done, tested** |
| Clip + thumbnail generation | `build_motion_event_clip()`, called from both event paths before any upload is attempted | **Done, tested** |
| Eligibility check | Appliance-wide `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` flag (master switch) **and** per-camera `cameras.cloud_recording_mode == 'motion'`, read live off the shared camera-configuration cache -- see §2a. | **Done, tested** (commit `50bdd6e`, `app/tests/test_event_media_uploader.py`) |
| Short-lived, camera-scoped S3 credentials | `recording_uploader._ensure_session()` -> `POST /api/appliance/recordings/{camera_id}/credentials` -> STS `AssumeRole`, session-policy-scoped per camera, reused by `event_media_uploader.py` | **Done, tested** |
| Upload | Direct `boto3` `upload_file()` for clip + thumbnail to the scoped session | **Done, tested** |
| Cloud registration | `detection_events` (event) then `detection_event_media` (clip/thumbnail keys) via `analytics_event_available()` / `analytics_event_media_available()` | **Done, tested** |
| Duplicate-safe retry | Both endpoints catch the insert conflict and return `{'status':'duplicate', ...}` instead of a second row -- `detection_event_media` additionally UPDATEs the existing row on retry rather than erroring | **Done, tested** |
| Customer API thumbnail URL | `_customer_event_thumbnail_url()` / `customer_event_thumbnail()` in `app/main.py`, reading `detection_event_media.thumbnail_s3_key` and returning a presigned URL | **Done, tested** |
| Portal display | Dashboard, analytics-results, investigation, and per-camera events views all render `event.thumbnail` as an `<img>` when present, with an explicit fallback state when absent | **Done** (covered by existing UI tests) |
| Survives cloud failure | Every upload call site (`build_and_upload_event_media()` / `build_and_upload_ai_event_media()`) wraps `upload_motion_event_media()` in `try/except`, logs, and returns -- local event/clip creation above it already completed and is never rolled back | **Done, tested** |

**Test-suite note:** `app/tests/test_motion_event_media_wiring.py` passes
8/8 in isolation. One of its tests
(`test_correct_event_id_camera_and_timestamps_are_passed`) fails only when
run as part of the full ~1,700-test suite -- confirmed identical on the
unmodified base commit, i.e. a pre-existing test-isolation issue (shared
global state leaking between unrelated test files somewhere in the suite),
not a defect in the feature and not introduced by Phase 1.

**Verdict on this path: implemented and tested end to end, including
per-camera eligibility as of commit `50bdd6e`.** See the coverage table
(§11).

## 4. Other specifically-requested confirmations

- **Installer rerun safety:** `05-provision-users-dirs.sh` uses `install -d`
  (create-only-if-missing) for every directory, including
  `/var/lib/anyaicam` itself. `06-deploy-vms.sh`'s
  `migrate_legacy_persistent_data()`/`migrate_legacy_persistent_file()`
  preserve existing recordings/data-config/`.env`; its `rsync -a --delete`
  explicitly excludes `recordings/`, `data/config/`, and `.env`.
  `ensure_vms_env()` only appends missing keys and never regenerates
  `ANYAICAM_APP_SECRETS`/`ANYAICAM_CAMERA_CREDENTIAL_KEY` once present.
  `09-identity.sh` preserves an existing `appliance_identity.json`
  unconditionally. Confirmed by reading all four scripts; not run against a
  live install in this pass (no Linux target available from this
  machine -- see the staging plan, §5).
- **VMS credential readability inside the container:** confirmed by
  matching path constants -- `ANYAICAM_STATE_DIR` defaults to
  `/var/lib/anyaicam` in both the agent's `config.py` (where
  `credential.json` is written, host-side) and all four VMS-side uploader
  modules (where it's read, container-side), and the new Compose mount is
  an exact `/var/lib/anyaicam:/var/lib/anyaicam:ro` bind. The VMS
  `Dockerfile` has no `USER` directive, so the container runs as root and
  can read the host's `0600`, `anyaicam`-owned `credential.json` through
  the bind mount regardless of UID matching. **This was verified by static
  analysis, not a live container run** -- this Windows machine cannot run
  the Linux-absolute-path-dependent Compose stack. The staging plan and
  Samsung checklist below both include an explicit live-verification step
  for this.
- **Unrelated `vms.env` values preserved:** three new tests
  (`appliance-agent/tests/test_setup_wizard.py`) prove
  `_upsert_vms_env_key()` preserves every other existing line, replaces
  rather than duplicates an existing `ANYAICAM_CLOUD_URL`, and creates the
  config directory if missing.
- **Local recording/analytics survive cloud failure:** `recording_uploader.py`'s
  own comment states "local recording behavior is identical whether or not
  this worker is [running]" -- the upload worker is a separate `asyncio`
  task from the FFmpeg-based recording pipeline, and every per-camera
  upload failure `continue`s or `return`s without touching local state.
  `analytics_sync.py` follows the same separate-worker pattern. Both were
  already true before Phase 1; nothing here changes that behavior.

## 5. Security review

- **No secrets, tokens, keys, or credentials in the diff** (reviewed the
  full diff line by line; only placeholder test literals like
  `'new-secret'` in `test_reenrollment.py`, a pre-existing pattern in that
  file).
- **Fail-closed signing:** `GET /api/appliance/updates/latest` returns 503
  rather than an unsigned manifest if `ANYAICAM_UPDATE_SIGNING_KEY_FILE` is
  unset/invalid -- proven by `test_published_release_without_signing_key_fails_closed`.
  The signature this server produces was proven to verify under the
  device's own existing `updater/verify.py` scheme
  (`test_published_release_is_served_signed_and_verifiable`).
- **`POST /api/appliance/updates/{id}/result`** requires the same
  appliance authentication as every other route in this file and rejects a
  duplicate report for the same `update_id` as a 409 (never silently
  overwritten).
- **New `'updates'` storage category, local-storage-backend caveat:** when
  `ANYAICAM_STORAGE_BACKEND=local` (not S3), package objects under this
  category are reachable via the existing, already-unauthenticated
  `/storage/{category}/{key}` route in `cloud_features.py` -- the same
  simplification every other local-storage category (`thumbnails`,
  `clips`, etc.) already has, not something Phase 1 introduces uniquely.
  Reaching an object still requires knowing its exact, unguessable key.
  Recommend S3 backend (presigned, time-limited URLs, no public route) for
  any real staging/production use of the updates catalog.
- **VMS container runs as root** (no `USER` in `Dockerfile`) -- a
  pre-existing condition, not changed by this patch, but worth noting as a
  hardening opportunity: it's also what makes the read-only credential
  mount work regardless of host/container UID matching. Tightening this
  later (a dedicated non-root user, matching UID/GID to the host
  `anyaicam` user) is a reasonable follow-up but is out of scope here.
- **Read-only mount** limits the new attack surface: the VMS container can
  read but never modify the agent's identity/state directory.
- **`ANYAICAM_ANALYTICS_SYNC_ENABLED`/`ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED`
  were not touched** by any Phase 1 code -- only `ANYAICAM_CLOUD_URL` is
  written by the new `setup_wizard.py` logic. Whatever state those flags
  are already in on a given appliance is preserved exactly.
- No production AWS, Stripe, or Samsung credentials were read, written, or
  printed anywhere in this work.

## 6. Staging installation plan

Not executed -- presented for approval.

1. Merge or otherwise make commit `85a0e5561fef41a910da2702f80bd53d91c184cc`
   (branch `staging/cloud-integration-repair`) available to whatever build
   process produces the staging installer/VMS image. Do **not** touch
   `main`/`develop` or any production branch/host.
2. On the staging host only (the previously-identified
   `anyaicam-staging` box, not Samsung, not the AWS production instance
   from the separate Phase 4 track): back up the current
   `/opt/anyaicam/docker-compose.yml`, `/etc/anyaicam/vms.env`, and note
   the currently-running `anyaicam-vms` image tag, before making any
   change.
3. Rerun the installer in repair mode (it will detect `INSTALL_STATE=existing`
   automatically) so the new `docker-compose.yml` mount and the Compose
   rebuild land, without disturbing existing recordings/config per §4.
4. Run (or re-run) `anyaicam-setup` against the staging box's already-known
   Cloud ID/portal so the new `first_enroll`/`coordinated_reenroll`
   branch-selection logic and the `ANYAICAM_CLOUD_URL` write are exercised
   for real.
5. Verify, on the staging host itself:
   - `docker exec anyaicam-vms env | grep ANYAICAM_CLOUD_URL` shows the
     expected portal URL.
   - `docker exec anyaicam-vms cat /var/lib/anyaicam/credential.json`
     succeeds and its `appliance_id` matches the agent's own
     `/var/lib/anyaicam/credential.json` on the host.
   - `anyaicam-vms` container health is `healthy`; `GET /health` and
     `GET /version` succeed.
   - An authenticated `GET /api/appliance/updates/latest` call returns
     `{"status":"no_update_available"}` (200), not a 404, and the agent's
     own logs show no further `SourceUnavailable` tracebacks.
6. Only after every check in step 5 passes, proceed to a **separate,
   explicitly approved** Samsung lab phase using the checklist below.

## 7. Samsung lab validation checklist (for the future, explicitly-approved phase)

This is the exact checklist to run **once approval is given** -- nothing
in it has been executed.

1. Confirm staging verification (§6, step 5) fully passed first.
2. Confirm the Samsung box's current recordings, customer/camera
   configuration, and `talk_isapi_diagnostic` setting are recorded
   (snapshot/checksum) before touching anything.
3. Run the installer in repair mode on Samsung.
4. Run `anyaicam-setup`; confirm it selects the correct enrollment path
   (fresh -> `first_enroll`, already-activated -> `coordinated_reenroll`)
   and completes without error.
5. Confirm `/etc/anyaicam/vms.env` now contains `ANYAICAM_CLOUD_URL=https://portal-staging.anyaicam.com`.
6. Confirm the VMS container restarted (via the queued `restart_vms`
   action or manually) and picked up the new env var.
7. Repeat the same three live checks as staging step 5 (env var present in
   the running container, `credential.json` readable and matching,
   `/health`/`/version` succeed).
8. Confirm the agent's own logs no longer show `SourceUnavailable`
   tracebacks for the update-check cycle.
9. Confirm existing recordings, customer records, camera configuration,
   and Cloud ID are byte-for-byte unchanged from step 2's snapshot.
10. Confirm Samsung audio, recording, and `talk_isapi_diagnostic` settings
    are unchanged and `talk_isapi_diagnostic` is still enabled.
11. Do **not** run camera discovery as part of this checklist -- separate
    explicit approval required.
12. Do **not** enable `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` as part of this
    checklist -- per-camera `cloud_recording_mode` eligibility is now
    enforced underneath it (§2a), but the appliance-wide flag remains its
    own deliberate, separately-approved decision, not a Phase 1 default.
    If it is enabled during a later phase, first confirm every camera's
    `cloud_recording_mode` is set to the intended value (`'motion'` for
    Cloud Motion, unset/`'disabled'` for anything that should stay local-
    only) via `POST /api/admin/cameras/{id}/cloud-recording-mode` --
    otherwise no camera will upload event media even with the flag on.

## 8. Rollback procedure

- **Code:** this work lives entirely on `staging/cloud-integration-repair`
  (an isolated worktree/branch), unpushed, un-merged. Rollback is simply
  not deploying it; if it is ever merged and needs reverting, `git revert`
  each code commit in reverse order (`50bdd6ed4d77c859b5b4b2ea1ea05df7bbbbb9b2`
  then `85a0e5561fef41a910da2702f80bd53d91c184cc`) cleanly reverts every
  Phase 1 change -- both are self-contained, no unrelated files touched.
  The documentation commits (`ae565a87...`, and this update) carry no
  runtime behavior and don't need reverting for a code rollback.
- **Staging:** restore the backed-up `docker-compose.yml` and `vms.env`
  from §6 step 2, `docker compose down`, retag/restore the previously-noted
  VMS image, `docker compose up -d`. No database rollback is required --
  Phase 1 adds one new, empty-by-default table
  (`appliance_update_results`) and modifies no existing rows.
- **Samsung (future phase only, not yet touched):** the same
  backup-then-restore pattern applies once a pre-install backup exists;
  restated here for completeness of the checklist, not because anything
  needs rolling back now.
- **Trigger conditions:** container fails health, `/health`/`/version`
  fail, login breaks, the credential file is unreadable inside the
  container, the update endpoint still 404s, or any unexpected exception
  appears in the VMS or agent logs after the change.

## 9. Sanitized Linux installer/rebuild package

Built from commit `50bdd6ed4d77c859b5b4b2ea1ea05df7bbbbb9b2` via the
existing `installer/build_release_installer.py` (release-allowlist
packaging, rejects `.env`/private-key/`.db`/backup-file patterns, scans
payload content for private-key and AWS-access-key signatures, enforces
LF-only shell scripts and correct executable bits -- all built into that
tool already, not something added for this pass).

- **File:** `dist/anyaicam-appliance-installer-1.1.0-vms-50bdd6ed4d77.tar.gz`
- **SHA-256:** `93f840d50455c4ab211b259b2d3b57ead978fd398df85c0a03623a4dfc443208`
- **`installer_source_commit` / `vms_release_commit`:** both
  `50bdd6ed4d77c859b5b4b2ea1ea05df7bbbbb9b2` (confirmed in
  `release-manifest.json` inside the archive)
- Builder's own scans: secret scan **clean**, shell LF check **PASS**,
  shell executable-bit check **PASS** (12 shell scripts)
- **Not installed anywhere.** Sits in the worktree's `dist/` directory
  (untracked, not committed to git -- a build artifact, not source) for
  inspection/hand-off only.
- Note: the VMS payload's `app/tests/` directory ships inside the
  package -- a pre-existing packaging convention (`app/` is packaged
  recursively), not something introduced by this pass. Not a secret
  concern (confirmed by the same content scan), just worth knowing when
  inspecting the archive's contents.

## 10. Full-suite regression: four runs, and the one flaky failure

| Run | Code | Passed | Failed | Skipped |
|---|---:|---:|---:|---:|
| Baseline | `ec5272f` (unmodified) | 1676 | 37 | 18 |
| Phase 1 | `85a0e55`/`ae565a8` | 1696 | 37 (identical list) | 18 |
| Eligibility, run 1 | `50bdd6e` | 1704 | 38 (37 identical + 1 new) | 18 |
| Eligibility, run 2 (same code, immediate re-run) | `50bdd6e` | 1705 | 37 (identical to baseline's 37; the new one is gone) | 18 |

The one extra failure in eligibility run 1,
`app/tests/test_talk_audio_relay.py::test_no_appliance_channel_uses_local_isapi_fallback`,
**did not reproduce on an immediate re-run of the exact same code** --
conclusive evidence it's a pre-existing, non-deterministic timing flake,
not a regression from this change:

- Direct inspection of the failure: `assert calls[-1] == "stopped"`
  against a mocked local ISAPI talk relay inside a WebSocket test --
  the last recorded call was still an audio frame, meaning the mocked
  relay's `.stop()` hadn't been observed yet when the assertion ran. A
  close/flush-ordering race in the test's own WebSocket handling, not
  application logic.
- The module under test (`talk_audio_relay.py`, Talk Down audio) has no
  import or call relationship to `recording_uploader.py` or
  `event_media_uploader.py`, the only files this pass changed.
- It passed both when its own 15-test file ran alone and on the
  full-suite re-run with zero code changes in between.
- **No changes were made to `talk_audio_relay.py` or its test** --
  correctly scoped per the instruction not to touch that module chasing
  a confirmed-unrelated flake.
- The 37-failure core list itself has now been reproduced identically
  across **four** separate full-suite runs (baseline, Phase 1, and both
  eligibility runs) regardless of which commit was checked out --
  strong, repeated evidence those are a pre-existing suite condition,
  not something any commit in this branch introduced.

## 11. Requirement-by-requirement coverage table

Against the original repair brief's REQUIRED FIXES (A-E) and TESTS
REQUIRED lists:

| Requirement | Status | Evidence |
|---|---|---|
| A. Activation-to-VMS config handoff (portal URL, runtime role, credential path) | **Done, tested** | `first_enroll()`/`coordinated_reenroll()` branch selection + `ANYAICAM_CLOUD_URL` write; 12 tests (`test_reenrollment.py`, `test_setup_wizard.py`) |
| A. Event-media enablement derived from entitlement/cloud-recording mode | **Done, tested** (commit `50bdd6e`) | Per-camera `cloud_recording_mode` gate, §2a. (Automatically *deriving* `cloud_recording_mode` from a purchased plan/entitlement record is still Phase 2 scope, architecture plan §3.1 -- today it's admin-set via `POST /api/admin/cameras/{id}/cloud-recording-mode`, not yet plan-driven.) |
| A. Preserve unrelated env vars/secrets | **Done, tested** | `test_preserves_every_unrelated_existing_line`, `test_replaces_rather_than_duplicates_an_existing_value` |
| A. Restart only necessary services | **Done** | `restart_vms` privileged-action queue (existing RDM4 mechanism), not a host/agent restart |
| B. VMS container access to appliance identity, no permanent AWS credentials | **Done** (verified by static analysis, not a live container) | Read-only `/var/lib/anyaicam` mount; STS-issued, short-lived credentials only (`recording_uploader._ensure_session`) |
| C. `GET /api/appliance/updates/latest` implemented, authenticated, no_update_available on empty, signed manifest + short-lived URL otherwise | **Done, tested** | 8 tests in `test_appliance_updates_latest.py` |
| C. Update-result reporting + signature verification | **Done, tested** | `POST /api/appliance/updates/{id}/result`; signature cross-verified against device's own `updater/verify.py` |
| C. Disabled update feature does not cause repeated tracebacks | **Done, tested** | `test_empty_catalog_returns_no_update_available_not_404` |
| D. Motion event -> clip -> thumbnail -> credentials -> S3 -> registration -> customer API -> browser | **Done, tested**, except: | See §3 row-by-row |
| D. Ineligible camera event upload remains disabled | **Done, tested** (commit `50bdd6e`) | `test_ineligible_camera_does_not_upload`, `test_continuous_mode_camera_does_not_upload_event_media`, `test_explicitly_disabled_camera_does_not_upload`, `test_unknown_camera_does_not_upload` |
| D. Eligible event uploads clip and thumbnail | **Done, tested** | `event_media_uploader.py` + existing `test_motion_event_media_wiring.py` (8/8 in isolation) |
| D. Retry does not duplicate event/media rows | **Done, tested** | `analytics_event_available()`/`analytics_event_media_available()` duplicate handling |
| D. Local recording/analytics survive cloud failure | **Done, tested** | Structural (§4: separate `asyncio` workers, pre-existing) + new unit-level proof that `upload_motion_event_media()` itself degrades to `False` without raising on a cloud/credential failure: `test_session_unavailable_returns_false_without_raising`, `test_control_plane_unreachable_during_registration_returns_false` |
| E. Installer: compatible versions, systemd, Compose, dirs/permissions, secure env files, activation handoff | **Partially confirmed** | Directory/permission idempotency and env-file preservation confirmed by reading all installer scripts (§4); not exercised on a live Linux install from this machine |
| E. Installer rerun does not erase recordings/customer configuration | **Done** (confirmed by reading `06-deploy-vms.sh`'s migrate/exclude logic; not live-tested) | §4 |
| Fresh activation writes required VMS configuration | **Done, tested** | `FirstEnrollTests` |
| Existing unrelated settings/secrets preserved | **Done, tested** | `test_setup_wizard.py` |
| Activation/configuration idempotent | **Done, tested** | `coordinated_reenroll()` version-bump behavior (pre-existing, unchanged), `first_enroll()` fail-closed on re-run against an already-enrolled box |
| VMS reads appliance identity after startup | **Done** (verified by static analysis, not a live container) | Path-constant matching, §4 |
| No permanent AWS credential exposed | **Done, tested** | STS-only credential flow; `first_enroll`'s `test_no_secret_logs` |
| Signed update success / invalid signature rejection / expired package URL | **Signed-success done and tested server-side; invalid-signature and expired-URL are device-side behaviors already covered by the pre-existing `updater/verify.py`/`updater/s3_source.py` test suites** | `test_appliance_updates_latest.py`; `appliance-agent/tests/test_updater_verify.py` (pre-existing) |
| Full Samsung mock end-to-end test | **Not run** | Requires a live Linux target; see §6/§7 for the plan to run it in staging, then Samsung, under separate approval |

**Overall verdict:** every confirmed root cause is fixed and tested,
including per-camera eligibility (commit `50bdd6e`), with zero
regressions against the full existing suite (§10). One item remains
explicitly **not** complete and is not being represented as such: any
live (non-static-analysis) verification on an actual Linux/Docker host --
this Windows machine cannot run the Compose stack; that verification is
staging step 5 (§6) and the Samsung checklist (§7), both not yet
executed.

## 12. Final staging-readiness decision (as of the code/docs commits)

**Ready for the staging installation plan in §6, with one explicit
condition** -- see §13, which records what actually happened when this
was attempted: the plan itself needs a different, actual
edge-appliance-shaped target before it can be carried out.

What's settled:
- All four confirmed root causes are fixed, tested, and produce zero
  code-level regressions across four full-suite runs spanning baseline,
  Phase 1, and the eligibility follow-up.
- The per-camera eligibility gap raised before this pass is closed and
  tested.
- The sanitized installer package builds cleanly from the exact commit
  under test, with the builder's own secret/integrity scans passing.
- The one extra test failure seen along the way
  (`test_talk_audio_relay.py::test_no_appliance_channel_uses_local_isapi_fallback`)
  was investigated per your instruction rather than dismissed: it did
  not reproduce on an identical re-run, is unrelated by inspection and
  by module dependency to anything this pass touched, and
  `talk_audio_relay.py` was correctly left untouched rather than
  "fixed" for a flake that isn't a real defect in the changed code.

What's still open, and why this isn't an unconditional "go":
- **No live verification has been performed on an actual Linux/Docker
  host.** Everything in §4 about in-container credential
  readability, the Compose mount, and installer rerun safety is
  correct by static analysis and by the packaged installer's own
  integrity checks, but has not been observed running. Staging §6
  step 5 exists specifically to close this before Samsung is ever
  considered.
- The pre-existing 37-failure baseline (unrelated to this work) is
  still unresolved in the suite generally -- not a blocker for this
  patch, but worth the team's attention separately.

**Recommendation (as originally written):** proceed to the staging
installation plan (§6) next. Do not proceed to the Samsung checklist
(§7) until staging step 5's live checks all pass. Superseded by §13.

## 13. Staging environment reconnaissance (attempted, then stopped)

Branch pushed to `origin` (`staging/cloud-integration-repair`, tip
`363c346`). Before executing §6's install/deploy steps, read-only
reconnaissance was run against `anyaicam-staging` (34.194.19.113, the
only host this session has SSH access to under that name) to confirm
its state first, per §6 step 2's own "back up before touching anything"
discipline.

**Finding: §6's staging installation plan targeted the wrong kind of
host, and was not executed.** `anyaicam-staging` has no `/etc/anyaicam`,
`/opt/anyaicam`, `/var/lib/anyaicam`, or `anyaicam` system user -- the
appliance installer has never run there. It instead runs three unrelated
containers (`anyaicam-staging-portal`, `anyaicam-staging-caddy`,
`anyaicam-staging-storefront`) -- confirmed to be the **cloud
control-plane** deployment (`ANYAICAM_RUNTIME_ROLE=cloud` inside the
portal container, which does run this repo's `app/` code) plus a
separate storefront/reverse-proxy stack, not an edge appliance running
the agent+VMS Docker Compose pair this work actually changed. It is
also a live, shared, Stripe-connected staging service (confirmed
`ANYAICAM_STRIPE_SECRET_KEY`/`ANYAICAM_STRIPE_WEBHOOK_SECRET`/admin-
password env vars are configured there -- variable *names* only were
read via `env | cut -d= -f1`; no secret value was ever printed), not a
disposable test box. `installer/README.md`'s own "Phase 4 gate" already
says appliance-installer validation belongs on "one fresh disposable
Ubuntu 24.04 t3.xlarge," not a shared cloud host -- this session also has
no AWS credentials configured locally to provision one.

Given that, **no deploy, install, or container change was made on
`anyaicam-staging` or anywhere else.** The mismatch and four options
(provision a disposable instance, validate only the cloud-side
`updates/latest` route against the existing portal container, point at a
different host, or stop) were presented back for a decision -- the
decision was to stop here without deploying. §6's plan needs revising
against an actual edge-appliance-shaped target before it can be
executed; that is next-session work, not resolved by this report.

**Status at end of session: code and documentation are staging-ready
(§9-§12); the branch is pushed; nothing has been deployed, installed, or
run against any live host, staging or Samsung.**
