# Golden-branch reconciliation — 2026-09-11

Full record of the branch reconciliation performed under the instruction
"PROCEED WITH GOLDEN-BRANCH RECONCILIATION — PRESERVE ALL WORK" /
"GO — PROCEED WITH RECONCILIATION, BUT DO NOT DEPLOY". This document is the
detailed backing for the summary in `docs/PROJECT_CHECKPOINT.md`.

## Why reconciliation was needed

A read-only baseline check found two entirely unrelated Git histories in
this repository:

- `build/v1.2-modular-foundation` — an old, self-contained "Local + Hybrid"
  baseline, last touched 2026-08-17 (`f0ee662`), documented in full by
  `docs/AI_HANDOFF.md` at `8f72b36`. `git merge-base` against the other
  history fails outright (exit 1) — there is no common ancestor.
- The lineage that actually evolved into everything currently running in
  staging/production, rooted in `feature/phase6-logout-csrf-hardening` /
  `production-reconcile-20260820`, which itself forked at commit `ec5272f`
  ("appliance: coordinate identity re-enrollment", 2026-09-08) into three
  further-diverged branches:
  - `claude/customer-provisioning-phase1` (30 unique commits) — customer
    entitlements, Stripe checkout/webhooks, hardware fulfillment/returns,
    tenancy, the `/ready` fix via `camera_status(request=None)`.
  - `staging/cloud-integration-repair` (34 unique commits) — the
    Samsung-validated claim flow, installer/privileged-watcher fixes, the
    `AgentConfig.load()` precedence fix, the `_legacy_camera_status()`
    variant of the `/ready` fix, the `restart_count NOT NULL DEFAULT 0`
    migration.
  - `codex/motion-event-media-cloud-flow` (9 commits after `1facd21`) — this
    session's motion-event-media cloud transport (Phase C real-hardware
    validated on Ryzen), the Live View degraded-vs-offline fix, the
    malformed-JS-template-literal fix, the AWS_REGION fail-loud guard, and a
    (duplicate, since removed) `restart_count` migration.

Given the amount of valid, real-hardware-validated work on all three
branches, the decision was **not** to revert to the old `8f72b36` baseline,
but to reconcile the three current siblings into one branch, with every
merge/cherry-pick decision made deliberately rather than blindly.

## Reconciliation branch

`reconcile/golden-foundation-20260911`, built in an isolated worktree
(`AnyAiCam-VMS-reconciliation`), based on `claude/customer-provisioning-phase1`
at `7277f74`.

### Commits, in order

| Commit | What | Resolution |
|---|---|---|
| `7277f74` | Base = `claude/customer-provisioning-phase1` HEAD | — |
| `741d4ea` | Merge `staging/cloud-integration-repair` | 4 conflicted files, see below |
| `c8d740e` | Cherry-pick `4d75027` (harden cloud event media flow) | 2 conflicted files, see below |
| `338ec78`, `ddbd104`, `4f00dbb`, `4b8fa02`, `499037d`, `3a6aa51`, `349fe32` | Cherry-picks of `caacd90`, `80479f3`, `e25dd68`, `3023279`, `e9bd68c`, `31bbb3d`, `2f562f5` | Applied automatically, zero conflicts |
| `b913b59` | Cherry-pick `1facd21` (AWS_REGION fail-loud guard + restart_count migration) | Auto-merged with no *git-reported* conflict, but silently duplicated the `restart_count` migration — see below |
| `git merge claude/customer-provisioning-phase1` | — | "Already up to date" (branch started from this exact commit; no-op, confirms nothing from this branch was missed) |

### Conflict resolutions in `741d4ea` (staging merge)

- **`app/db_migrations.py`** (2 conflicts) — both resolved by keeping both
  sides' independent additive migrations: `hardware_orders` /
  `pending_analytics_links` (customer-provisioning) alongside
  `appliance_update_results` / `appliance_claims` (staging) in the first
  block; unrelated `stripe_price_id` entitlement columns
  (customer-provisioning) alongside staging's Samsung-validated
  `restart_count INTEGER NOT NULL DEFAULT 0` in the second. Nothing here
  was mutually exclusive — both sides were additive.
- **`appliance-agent/anyaicam_agent/config.py`** — kept staging's `f613b2b`
  full precedence-aware `AgentConfig.load()` (the fix that stops an
  untouched installer bootstrap placeholder from overriding a real
  activation value), and merged customer-provisioning's
  `entitlement_refresh_interval_seconds` alias into it (both the `aliases`
  dict and the int-conversion set) so neither side lost functionality.
- **`appliance-agent/anyaicam_agent/setup_wizard.py`** — kept staging's side
  entirely. Staging's version imports `first_enroll` (not
  `ensure_identity_files_exist`), adds `_upsert_vms_env_key()`, and has the
  full `main()`/`interactive_main()`/`claim_main()`/`_finish_enrollment()`
  claim-flow structure. Customer-provisioning's inline
  `ensure_identity_files_exist()` + `coordinated_reenroll()`-only flow was
  dropped as superseded — it's the less complete of the two, missing the
  claim flow and the `ANYAICAM_CLOUD_URL`/VMS-restart persistence step that
  directly prevents Ryzen's portal-URL bug.
- **`appliance-agent/tests/test_reenrollment.py`** — dropped
  customer-provisioning's `FirstRunBootstrapTests` (tests
  `ensure_identity_files_exist()`, the code just removed), kept staging's
  `FirstEnrollTests` (tests `first_enroll()`), for consistency with the
  `setup_wizard.py` resolution above.

### Conflict resolutions in `c8d740e` (event-media cherry-pick)

- **`app/event_media_uploader.py`** — both branches independently wrote the
  same `cloud_recording_mode != "motion"` gate with different comments;
  kept the more detailed, staging-consistent comment. No behavioral
  difference between the two sides.
- **`app/recording_uploader.py`** — two blocks in `_refresh_camera_map()`
  (docstring wording, and `cloud_recording_mode` vs. `recording_mode`
  variable naming); kept HEAD's/staging's `cloud_recording_mode` naming and
  fuller docstring for consistency with the rest of the merged file.

### Silent duplicate found and fixed after `b913b59`'s auto-merge

Cherry-picking `1facd21` produced no git conflict in `db_migrations.py`, but
left **two** separate `if 'restart_count' not in appliance_columns:` guard
blocks — my own nullable-column version (from tonight's earlier work) and
staging's `NOT NULL DEFAULT 0` version (already carried in from `741d4ea`).
A clean auto-merge does not guarantee semantic non-duplication when both
sides add textually non-overlapping but functionally identical blocks.
Fixed by removing the nullable-version block and keeping staging's
Samsung-validated `NOT NULL DEFAULT 0` version, then amending the commit
(final hash `b913b59`).

## The `/ready` "duplicate" — investigated, not removed

The approved reconciliation plan called for using "the architecturally
safer `/ready` solution" (implying dropping customer-provisioning's
`camera_status(request: Request = None)` in favor of staging's
`_legacy_camera_status()`). Direct inspection of the merged `main.py` found
**both are independently load-bearing for different call sites**:
`readiness_snapshot()`, `health_monitor()`, and
`site_monitoring_summary()` call `_legacy_camera_status()`; two other call
sites (~line 60281 and ~66869) call bare `camera_status()` relying on its
`None` default. Removing either would break its own call sites. **Deviation
from the approved plan, flagged here rather than silently done**: both
mechanisms were kept as-is; this is not a duplicate needing resolution, it
is two different fixes for two different code paths that happen to share a
name pattern.

## Duplicate recording-upload systems — status

`main.py` starts both the old `cloud_upload_worker()` (via
`asyncio.create_task(cloud_upload_worker_placeholder())`, gated
`ANYAICAM_CLOUD_UPLOAD_ENABLED`) and the new
`recording_uploader.recording_upload_worker()` (gated
`ANYAICAM_RECORDING_UPLOAD_ENABLED`) as independent background tasks.
`recording_uploader.py` is authoritative — proper tenant/IAM scoping,
codec-aware H.264 remux/HEVC transcode, a retention sweep, and extensive
real-hardware validation across the R1-R5 deployment log entries that
`cloud_upload_worker()` never received. **Retiring the old worker was
planned but not yet executed in this reconciliation pass** — it is called
out here explicitly rather than silently left in place; whoever picks this
up next should confirm both gating env vars' current values in
staging/Ryzen/Samsung before removing the old worker, since flipping a
worker off is itself a runtime-behavior change that needs its own
verification, not something to bundle silently into a source reconciliation.

## Diff vs. currently-deployed staging code

Compared against a local snapshot of staging's deployed `app/` taken
2026-09-10 01:59 — **before** tonight's own hot-patches, so this comparison
reflects staging's code as it stood before this session's live debugging,
not including the runtime-only fixes applied directly to the container
tonight.

- **Zero "only in staging" files remain.** Earlier in this session,
  `hardware_fulfillment.py`, `hardware_orders.py`, `hardware_returns.py`,
  `provisioning_api.py`, `purchase_notifications.py`,
  `analytics_entitlements.py`, and `customer_entitlements.py` were missing
  from every branch checked — all now present via the
  `claude/customer-provisioning-phase1` base.
- **Only in the reconciled branch** (staging doesn't have yet):
  `app/appliance_claims.py` (claim flow), `app/event_media_outbox.py`,
  `app/event_media_policy.py` (this session's motion-cloud work),
  `app/updates_storage.py` (from the staging branch).
- **11 files differ in content:**

  | File | Diff lines | What changed |
  |---|---|---|
  | `appliance_cloud.py` | 101 | claim flow + restart_count telemetry |
  | `appliance_protocol.py` | 37 | claim flow protocol additions |
  | `db_migrations.py` | 84 | all migrations listed above |
  | `event_media_uploader.py` | 162 | motion-cloud event media transport |
  | `live_relay_uploader.py` | 20 | AWS_REGION guard |
  | `live_view_page.py` | 29 | degraded-vs-offline + JS-syntax fixes |
  | `main.py` | 70 | wiring for the above |
  | `object_storage.py` | 4 | added `'updates'` to `ALLOWED_CATEGORIES` |
  | `recording_retention_sweep.py` | 9 | sweep now also covers `detection_event_media`, not just `recordings` |
  | `recording_uploader.py` | 44 | AWS_REGION guard + event-media hardening |

  Every difference is additive/explanatory — none represent functionality
  present in staging's deployed code but absent from the reconciled branch.

**Caveat:** this diff predates tonight's own hot-patches to the live
staging container, so it does not yet prove the reconciled branch's source
contains *everything* that's currently running on staging right this
moment. See the appliance checkpoints for what's runtime-only vs. sourced.

## Test results

Full relevant automated suite run against the complete reconciled state,
isolated inside the real `anyaicam-staging-portal` container on the actual
staging EC2 host (`anyaicam-staging`, `34.194.19.113`) — not the local
Docker Desktop `anyaicam-vms` container, which is an unrelated, long-stale
local artifact from 2026-08-06 and was not used for any of this testing.

**First run** (`/tmp/recon_full_output.txt`, stage-1 staging-merge-only copy
and then the complete reconciled copy) both completed:
- Stage-1 (staging merge only): 370 failed, 1243 passed, 22 skipped.
- Full reconciled state: **379 failed, 1267 passed, 22 skipped**, in 1021s.

Both numbers are far above the known pre-reconciliation baseline
("Failure count moved from 37 to 38" per staging's own commit `50bdd6e`),
which needed investigation before being trusted either way — a difference
this large could mean a real reconciliation regression, or it could mean
the test harness itself was invalid.

**Root cause found: test-harness artifact, not a reconciliation
regression.** The isolated copies were run *inside* the real
`anyaicam-staging-portal` container to reuse its installed dependencies,
which means they also inherited its real, production-shaped environment:
`ANYAICAM_TRUSTED_HOSTS=portal-staging.anyaicam.com`,
`ANYAICAM_ENV=staging`, `RUNTIME_ROLE` unset (so `edge_production` is
`False` and the edge exemption in `cloud_config.py`'s
`effective_trusted_hosts` never applies). `main.py` installs
`TrustedHostMiddleware` with that value, and Starlette's `TestClient`
sends `Host: testserver` by default — a host the real staging domain
allowlist correctly does not include. Every test that made an HTTP request
through `TestClient` got a blanket `400 Bad Request` ("Invalid host
header") regardless of what it was actually testing, which is exactly the
uniform, cross-cutting `assert 400 == <expected>` pattern seen across
unrelated files (`test_admin_customer_management.py`,
`test_notification_settings.py`, `test_website_partner_session_nav_links.py`,
etc.) — a real regression in the merged code would cluster in the files
that were actually touched, not spread evenly across the whole suite. This
is a flaw in *how the test was run*, not in the reconciled source, and it
would have affected a from-scratch checkout of the pre-reconciliation code
run the same way, too.

**Corrected run**, with `ANYAICAM_TRUSTED_HOSTS` overridden to
`testserver,localhost,127.0.0.1` for the pytest subprocess's own
environment only (not written to the container's persistent env, not
affecting the real staging service, which keeps serving
`portal-staging.anyaicam.com` throughout):

*(In progress at `/tmp/recon_full_output2.txt` on `anyaicam-staging` —
being tracked live. Fill in the final pass/fail/skip counts and compare
against the 37-38-failure baseline once a `DONE_EXIT_` marker is observed;
do not treat this section as final until then.)*

## Local + Hybrid, Motion Cloud, and customer-provisioning functionality — confirmed present

- Local ONVIF/RTSP → FFmpeg → HLS/recording pipeline: present, untouched by
  reconciliation (not part of any conflicted file).
- `RUNTIME_ROLE` (edge/cloud/combined): present, untouched.
- Multi-tenant DB model, `appliance-agent` control-plane package, PWA/
  billing/notification layer: present via the `claude/customer-provisioning-
  phase1` base.
- Motion Cloud event-media transport (Phase C real-hardware validated):
  present via the `codex/motion-event-media-cloud-flow` cherry-picks.
- Live Relay, dedicated Live View fixes: present, see diff table above.
- Customer provisioning, entitlements, Stripe, hardware fulfillment, claim
  flow: present via the customer-provisioning base and the staging merge.

## Runtime-only fixes not yet represented in Git

These were applied directly to Ryzen's or staging's live runtime tonight
and are **not** captured as source changes anywhere, including in this
reconciled branch:

- Ryzen's `vms.env`: manually set `AWS_REGION`, `ANYAICAM_STATE_DIR`,
  `ANYAICAM_CLOUD_URL` — these are environment values, not code, and would
  need to be set again (or handled by a real fix for the "stale env value"
  gap noted in `docs/checkpoints/RYZEN.md`) on any reinstall.
- Various one-off diagnostic/repair scripts left on the `anyaicam-staging-
  portal` container's `/tmp` (`fix_device_key.py`, `fix_license.py`,
  `fix_onboarding_and_license.py`, `fix_online_status.py`,
  `revert_online_status.py`, `reset_alejandro_password.py`, and several
  `trace_*.py`/`verify_*.py` diagnostic scripts) — these were exploratory
  tools used during tonight's live debugging, not deployment artifacts, and
  should not be treated as part of the product. They should be cleaned up
  once no longer needed for reference.
