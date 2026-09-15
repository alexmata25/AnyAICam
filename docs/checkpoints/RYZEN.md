# Ryzen appliance — checkpoint

**Read `docs/PROJECT_CHECKPOINT.md` first.** This file assumes that
context, and in particular the "RC1 → RC2 on Ryzen", "RC2 → RC3 on
Ryzen", and "RC3 on Ryzen — PASSED" sections there.

## 2026-09-11: Ryzen was wiped and reinstalled clean — do not assume anything below this line about "5 real cameras" applies anymore

Everything this checkpoint said before 2026-09-11 (5 real cameras, real
recordings, a real activated customer identity, "do not re-image or clean
up Ryzen") described a real state that **no longer exists**. Later the same
day, under explicit, doubly-confirmed direct instruction (the operator was
told in-session, in writing, that this contradicted the checkpoint's own
prior language and the real activation identity it recorded, and confirmed
in writing that the wipe should proceed anyway), Ryzen's entire AnyAiCam
state was intentionally destroyed and reinstalled from scratch as a
clean-install test of the golden RC1 pipeline: recordings, the real
`customer_id 4efaf5153f` / `cloud_id AIC-C90CF0C9` activation, camera
bindings, and the old appliance-agent install were all removed via
`installer/uninstall.sh --purge-all`'s target list (`/opt/anyaicam*`,
`/etc/anyaicam`, `/var/lib/anyaicam`, `/var/log/anyaicam`, the `anyaicam`
system user). Ubuntu itself, networking, SSH, Docker, and Tailscale were
left untouched throughout.

**Do not treat any fact below this point as describing real customer data**
— it describes a disposable clean-install validation appliance now, not a
production install with a real customer behind it. If Ryzen is ever
reactivated with a genuine paying customer's identity in the future, this
file must be updated again to say so explicitly before "do not touch"
language applies again.

## Current real state (as of the 2026-09-11 RC3 install — validate.sh PASSED)

- **Disk**: 405GB free, 8% used (28GB) as of the RC2 install; not
  independently rechecked after RC3's repair-path install (expect no
  material change -- RC3's VMS image is byte-identical to RC2's, no new
  layers to pull/build beyond what's already cached).
- **RC1** (`1dfcbf2`) installed clean, but `validate.sh` failed on a real
  source defect (`configuration_issues()` not `RUNTIME_ROLE`-aware).
  Ryzen was purged (`uninstall.sh --purge-all`) before RC2.
- **RC2** (`34d9d5c`) fixed that defect (confirmed live via raw `/ready`
  diagnostics before RC3 was deployed). Its own `validate.sh` had a
  separate, installer-only defect (curl -f treating a legitimate 503 as
  failure).
- **RC3** (`4ade235`) fixed the `validate.sh` defect and **is now
  installed and running on Ryzen, with `validate.sh` PASSING (0
  failures)** -- the first golden build to pass its own installer's full
  validation on real hardware. Deployed via the repair path (RC2 was
  never wiped for this step -- `detect_install_state()` correctly
  reported 5/5 markers -> `existing`, confirmed in the actual output, not
  assumed).
- **No runtime patches were applied at any point** across RC1, RC2, or
  RC3 on Ryzen -- every fix that mattered went through
  BUG FOUND → FIX SOURCE → REGRESSION TEST → COMMIT → BUILD → DEPLOY →
  VERIFY, never a hand-edit on the box itself.
- **Appliance identity**: preserved from the RC2 install (RC3 was a
  repair-path install, not a fresh one, so the identity file was not
  regenerated). Read it live via `sudo cat
  /etc/anyaicam/appliance_identity.json` if the exact UUID is needed --
  not recorded here since a value that can change with a future
  clean-install run isn't safe to treat as a citable fact after the
  fact. This is installer identity only, not a cloud claim/activation --
  no `cloud_id`/`customer_id` exists yet.
- **Cameras**: zero configured, zero discovered, zero recording -- by
  design. Claim/activation and camera discovery have not been performed.
- **Live Relay / Motion Cloud / customer portal / Live View**: not yet
  validated against RC3 -- blocked behind claim + camera discovery,
  neither of which has happened yet.

## 2026-09-11 (later same day): agent-service crash loop found and fixed in source, two separate root causes, appliance identity changed again

Immediately before the claim attempt, `anyaicam-agent.service` was found
crash-looping (600+ restarts) via a **read-only check that had never been
run before** (`systemctl show ... NRestarts` — the checkpoint above never
verified this). Two, independent, real defects, found in this order:

1. **Stale systemd drop-in** (`/etc/systemd/system/anyaicam-agent.service.d/vms-paths.conf`,
   dated 2026-08-19 — nearly a month before this reconciliation work
   started, from an unrelated local-dev session, bind-mounting
   `/home/alejandro-mata/projects/AnyAICam/{app/static/hls,recordings}`
   onto `/var/lib/anyaicam/vms/{hls,recordings}`). One of its two bind
   sources no longer existed, and neither `installer/uninstall.sh` nor
   `appliance-agent/scripts/uninstall.sh` ever removed drop-in
   *directories* — only base unit *files* — so this survived the full
   `--purge-all` + RC1/RC2/RC3 reinstall cycle untouched and silently
   reattached to the fresh unit each time. **Fixed in commit `88c87a1`**
   (both uninstall scripts, both the VMS and agent units, for the general
   defect class). Removed from Ryzen directly (one-time manual cleanup of
   pre-existing cruft, not a golden-source patch): `sudo rm -rf
   /etc/systemd/system/anyaicam-agent.service.d && sudo systemctl
   daemon-reload`.
2. **After removing the drop-in, the agent kept crash-looping** — a
   second, different, genuinely pre-existing defect in `service.py`
   itself: `run()` raised `RuntimeError('Appliance is not activated...')`
   unconditionally whenever no credential existed yet, and the systemd
   unit's `Restart=always`/`RestartSec=10` turned that into a permanent
   loop. `install.sh` enables+starts this unit unconditionally, *before*
   claim ever happens — "installed but not yet claimed" is the FIRST real
   state of every fresh appliance, and exactly the state
   `installer/validate.sh` runs in. **This defect predates this entire
   reconciliation effort** — it was always there, just never surfaced,
   because `validate.sh` never checked the agent's `is-active` state
   until earlier the same day. Along the way, this also meant commit
   `55fa281` (the `validate.sh` is-active fix, made *before* this second
   defect was found) was validated as correct-in-intent but not
   sufficient alone — it needed `service.py`'s own fix alongside it to be
   achievable on any fresh install. **Fixed in commit `25e2fc1`**:
   `run()` now waits for activation (`_await_activation()`, polling
   `credential.json` directly every 10s) instead of raising; an
   already-activated appliance's behavior is completely unchanged.

**RESOLVED (2026-09-12): RC4 (`087e455`) bundles all three fixes and has
been deployed to Ryzen via a repair-path install** (5/5 markers →
`existing`, identity and all persistent state preserved, no wipe). Full
physical verification passed:
- Journal confirms the exact transition: last old-code crash at
  `19:53:28`, RC4 process starting at `19:53:38` and logging the exact
  new string (`"Appliance is not activated yet; waiting for
  anyaicam-setup (interactive or --claim) to complete..."`), then **zero
  restarts since**, confirmed stable across multiple activation-poll
  intervals (`NRestarts` unchanged, `ActiveState=active`/
  `SubState=running`/`ExecMainStatus=0`).
- `sudo bash validate.sh` → **PASSED, 0 failures**, including the new
  `anyaicam-agent.service is active` check.
- `/health` 200, `/version` reports exact commit `087e45587856...`,
  `cloud_id: null` (still unclaimed). `/ready` 503 correctly (0
  critical). No AWS/cloud flags enabled. Stale drop-in directory
  confirmed absent; `systemctl cat` shows only the clean base unit.
- No runtime patches at any point — full BUG FOUND → FIX SOURCE →
  REGRESSION TEST → COMMIT → BUILD → DEPLOY → VERIFY cycle for all three
  fixes; the only direct Ryzen action was the one-time removal of the
  pre-existing (non-golden) stale drop-in.

See `docs/PROJECT_CHECKPOINT.md`'s "RC4 on Ryzen — the agent-service
crash loop, root-caused, fixed, and PASSED" section for the complete
narrative, all commit hashes, and all artifact digests.

**Appliance identity confirmed preserved** through the repair install:
`637ad320-daaa-436e-89c9-70a84f4f54a9` (unchanged from before RC4).

## 2026-09-12 (later): CLAIMED — cloud_id `637AD320-DAAA-436E-89C9-70A84F4F54A9`

After two stranded attempts (a cloud-side single-tenant `persist_activation()` defect, then a separate appliance-agent deployment gap — see `docs/PROJECT_CHECKPOINT.md`'s own dated sections for the full incident), a third claim (code `FA89A9EC`) succeeded end to end. Ryzen is now genuinely activated against `anyaicam-staging`: `customer_id=4efaf5153f` (Alejandro Mata), `site_id=4de6186be8` (Ryzen Home Site), one real credential issued and durably used (`appliance_credentials id=ffdf68c39378f4e9`), agent authenticating and heartbeating successfully (`online_status=online`, live `last_check_in`), no polkit failure, no rollback. Installed release: `b8bdf2cf98c716067024bdf471d834ce5cd602e1` (confirmed via `/version`'s `build_id` and `validate.sh`). `/ready` is `503` — correctly, since AWS/Motion Cloud remain deliberately unconfigured and zero cameras are attached yet; `self_test.ok: true`, 0 critical issues. See `docs/PROJECT_CHECKPOINT.md`'s "Third claim attempt — SUCCEEDED" section for the complete verification.

**Do not reuse or reference the two now-revoked stranded claim/appliance/credential rows from earlier tonight (`appliance_claims 48e3f8f0...`/`73c06c0805a1...`) — they are dead, cleaned-up history, not this appliance's live record.**

## Exact next step

Claim/activation is now complete (see above) — items 1-2 below are
done. Remaining, pending separate explicit authorization same as
always:

3. Reconnect the 5 physical cameras through the supported discovery
   workflow (not manual CAMERA{n}_* env vars).
4. Validate, in order: recording, motion detection/event media, Motion
   Cloud upload (once camera + cloud identity + AWS config all exist),
   customer portal camera mapping/status, Live View (both grid and
   dedicated single-camera pages), restart persistence, and specifically
   **defect #3** (a stale `agent.env`/`vms.env` activation value must not
   override the current persisted activation after a restart — this is
   closed in source as of `1dfcbf2`, but has never been checked against
   Ryzen's *actual* real hardware/restart behavior, only against the
   regression test suite).
5. Update this file again once any of the above changes real state on
   Ryzen — a checkpoint that isn't updated is worse than none, per
   `docs/PROJECT_CHECKPOINT.md`'s own standing rule.

## 2026-09-13: superseded — do not assume `637AD320-...`/`customer_id 4efaf5153f` still applies. Current identity is `AIC-C814766E`, installed release `6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c`

Everything above this line describes a real state that has since moved on. In order: the `637AD320-...` claim above was later cloud-side released/unclaimed (Ryzen's own software/identity deliberately left untouched at that point — `coordinated_reenroll()` has no standalone "return to unclaimed" path); a new real customer (`anyaicamtest@gmail.com`) then self-service-provisioned a fresh appliance (`AIC-C814766E`) and ran `anyaicam-setup` on this box for real, which correctly detected the existing identity files and went through `coordinated_reenroll()` (not `first_enroll()`) to atomically swap in the new identity — Ryzen is now genuinely `AIC-C814766E`, customer `d75bdbecdd4887de4d2b89a9fcea9092`, site `f67fa371cd`.

Installed release history since: `aa4dc2edb135e379631a9d17db8923e56a202ed9` (fixed cloud-provisioned cameras reaching the edge VMS's own local DB — "Option B" local credential handoff), `6d6dcd5d6e665bfb74ab76cb6dd1fb2bc3c01d6c` (fixed the `aa4dc2e` handoff's own two blocking bugs: the local endpoint's `PUBLIC_PATH_PREFIXES` gap and Docker hairpin-NAT gateway detection; also fixed camera-provisioning to consume a licensed placeholder instead of always inserting a new row), then **`1893a727958678ed88ecfbcc6f2ea61a46123698`** (current, verified live 2026-09-13 — fixed the stale `camera_bindings.json` lifecycle gap: a binding from Ryzen's PRIOR customer identity never got invalidated, permanently blocking Camera 1 from binding to the same physical MAC/camera_number; `coordinated_reenroll()` now resets bindings on a genuine identity swap, and `auto_bind_discovered_cameras()` self-heals any binding whose owning cloud camera no longer exists). Each installed via the operator's own `sudo ./install.sh --repair`, never hot-patched. Full post-install verification for each recorded in `docs/PROJECT_CHECKPOINT.md`'s own dated entries — read those for full detail, this file is deliberately just the pointer.

**Current real state, for the next session**: Camera 1 (`dfba6a63ec`, device_key `urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af`) is now **fully functional end to end** — cloud-provisioned, locally credentialed, actively streaming real RTSP→HLS video, correctly bound in `camera_bindings.json`, and cloud-reported as `online=1`/`recording=1`/`last_error=None`. Two items remain separately, explicitly not-yet-authorized: (1) provisioning Cameras 2–5 through the same now-fully-working Customer Setup Step 4 flow, and (2) a one-time placeholder-count reconciliation (the cloud still shows 9 total camera rows — 8 licensed placeholders + Camera 1 — for an 8-slot entitlement; deleting one placeholder was designed in `docs/PROJECT_CHECKPOINT.md` but never executed).

## 2026-09-13 (later): Cameras 2–5 added — all five real cameras now streaming simultaneously

The one-row placeholder reconciliation above was executed first (cloud camera-row count is 8, matching the 8-slot entitlement). Cameras 2 (`dc7a226120`, MAC `14:2f:fd:60:a2:85`), 3 (`5c689a0c0e`, MAC `14:2f:fd:60:6e:23`), 4 (`55bdd715ea`, MAC `14:2f:fd:a0:83:60`), and 5 (`41dc80c85e`, MAC `14:2f:fd:a2:f5:7b`) were then added one at a time through Customer Setup Step 4, each independently read-only verified before the next was added. No source changes were required for any of the four — every fix already recorded in `docs/PROJECT_CHECKPOINT.md` (placeholder consumption, the local credential-handoff endpoint, and the stale-binding self-healing) worked unmodified across all of them.

**Current real state**: all 5 physical cameras (`camera_number` 1–5, 5 distinct cloud camera IDs) are cloud-provisioned, locally credentialed, actively streaming real RTSP→HLS video concurrently, and cloud-reported as `online=1`/`recording=1`/`last_error=None`. 3 generic placeholders remain (`8f2e4ce58a`, `c60062fd80`, `eff9704054`), for the correct total of 8 cloud camera rows against the 8-slot entitlement. Installed release unchanged from above: `1893a727958678ed88ecfbcc6f2ea61a46123698`. `anyaicam-agent.service` `NRestarts=0`; `anyaicam-vms` container `healthy`, `RestartCount=0` — no crash-loop across any of the four additions. Full detail (per-camera verification, the two transient/self-resolved convergence delays observed, CPU/RAM under all-five-streaming load, and the one item still sudo-gated — `/var/lib/anyaicam/camera_bindings.json`) is in `docs/PROJECT_CHECKPOINT.md`'s own dated section, not duplicated here.

**Exact next step**: no camera-provisioning work remains for this customer/appliance. Any further step (cleanup of the 3 remaining placeholders, Live View validation, motion/event-media, Motion Cloud) is separately, explicitly not-yet-authorized.

## 2026-09-15: Golden-foundation release (`b90ac639`) deployed to Ryzen — repair install, PASSED, all five cameras confirmed healthy on the new build

The operator authorized bringing Ryzen up to the current authoritative cloud/source checkpoint (`reconcile/golden-foundation-20260911`), 19 commits ahead of the previous Ryzen baseline (`48992a5`) — the account-recovery/password-reset feature plus four tenant-isolation passes, Notifications reliability, Playback pagination reliability, and Investigate reliability, none of which had been on Ryzen before. `appliance-agent/` and `installer/` were unchanged in that range, so no installer-logic delta existed.

**Division of labor, per this project's own standing rule**: Claude built and hash-verified the release, then `scp`'d it (no sudo) to Ryzen's own home directory — **the operator ran `sudo ./install.sh --repair` and `sudo bash validate.sh` themselves**, exactly like every prior Ryzen release in this checkpoint's history. Claude never executed a privileged command against Ryzen.

**Release build**: `installer/build_release_installer.py --vms-commit b90ac6391b50dae0ce6bccf01c28f5ff367c6427` (repo `reconcile/golden-foundation-20260911` at that commit) — all built-in checks passed (no secrets, LF-only scripts, executable bits verified). Artifact `anyaicam-appliance-installer-1.1.0-vms-b90ac6391b50.tar.gz`, SHA-256 `0708cd577a7bca6dca5cd6ba0499fca8c1603b0d3ef1a8d54492a49da942dafa`, verified identical on Ryzen before extraction.

**Install**: operator-run repair install (`5/5 markers -> existing`, identity/bindings/recordings preserved, no wipe), `validate.sh` **PASSED, 0 failures** (operator-confirmed).

**Post-install verification, all read-only, all PASSED**:
- `/version` `build_id`: `b90ac6391b50dae0ce6bccf01c28f5ff367c6427` (was `48992a51b91c...`) — exact match to the authoritative commit.
- `cloud_id` unchanged: `AIC-C814766E`.
- `anyaicam-vms`/`anyaicam-agent` both `active`, `NRestarts=0` on both — no crash loop.
- `/ready` -> `ready:true`; `self_test.ok:true`, all four critical checks pass (`recordings_writable`, `storage_available` 204.4GB free, `ffmpeg_available`, `configuration_valid` — the same 3 pre-existing non-critical warnings as before, `ANYAICAM_ADMIN_EMAIL`/`ANYAICAM_ADMIN_PASSWORD`/`ANYAICAM_PORTAL_SECRET` unset, unrelated to this release).
- **All five real cameras individually confirmed streaming and recording on the new build**: each camera's HLS `.m3u8`/latest `.ts` segment freshly written within the prior ~1-2 minutes of the check, and each camera's `/app/recordings/camera{N}/` directory had a `.mkv` file written within the prior 5 minutes. `cameras_online: 8`/`cameras_total: 8` (5 real + 3 unused placeholders, matching the 8-slot entitlement recorded above).
- A restart-time burst of `analytics_sync`/`recording_uploader` control-plane `429`s (all 5 cameras' workers reconnecting simultaneously) was observed in the first ~3 minutes after restart and confirmed self-resolved (zero in a fresh 30s log window taken afterward) — not a regression, ordinary reconnect-storm behavior. Occasional `[cameraN] error while decoding MB ..., concealing ... errors` H.264 lines on camera2/camera4 are ordinary RTSP error-concealment noise from real hardware, consistent with streaming/recording output being unaffected for those cameras.

**Nothing broke; no debugging or source fix was required for this release.** Samsung was not touched.

**Exact next step**: no further Ryzen work is authorized by this pass. Live View / motion-event / Motion Cloud validation against the new build remains separately, explicitly not-yet-authorized, same as before.

## 2026-09-15 (later): correction -- "nothing broke" above was checked only against local HLS/recording generation, never the real customer portal. It doesn't work. Root-caused both Live View and Playback; one genuine, unrelated appliance bug found and fixed; both real root causes require an owner decision, not a quiet code fix

The operator personally tested the real customer portal after the `b90ac639` release above and found Live View (no feed on any camera) and Playback (nothing plays) both genuinely broken. Correcting the record: the previous entry's "all five cameras confirmed streaming and recording" was true and remains true -- but it was never proof the *customer-facing* paths worked, only that local FFmpeg output existed. This is the same class of mistake this project has been burned by before (verify each real layer, not just the backend one) and should not be repeated.

**Traced customer portal -> cloud (`anyaicam-staging`, the SAME staging environment the account-recovery feature was validated against, per `ANYAICAM_CLOUD_URL` on Ryzen) -> Ryzen -> camera, separately for each feature, using only read-only queries/log inspection plus one safe, contained source fix. Ryzen was NOT reinstalled/reconfigured to investigate.**

### Root cause: Live View

The full command/session chain is real and working, confirmed live: a customer's "start live view" click (`live_view_sessions.py`, Phase 6c) correctly queues a `start_live_relay` command; appliance-agent correctly polls and executes it (`commands.py`'s `_set_relay_command()`), writing `/var/lib/anyaicam/live_relay_commands.json`; `live_relay_uploader.py` (inside the VMS container, which has that same path bind-mounted read-only) correctly reconciles it via `set_relay_active()` and begins requesting a live-upload S3 credential from the cloud every ~2 seconds, for every camera, continuously (confirmed via live container logs during the operator's own test window).

**Every single one of those credential requests gets HTTP 404 from the cloud**, because `appliance_cloud.py`'s `live_relay_session()` route starts with `if not LIVE_RELAY_ENABLED or not appliance.get('live_relay_pilot'): raise 404` -- and on `anyaicam-staging`'s live `portal-green` container, `ANYAICAM_LIVE_RELAY_ENABLED` is **not set at all** (confirmed via the container's own env), so it defaults false. `LIVE_UPLOAD_ROLE_ARN`/`LIVE_RELAY_S3_BUCKET`/`LIVE_RELAY_AWS_REGION` are equally unset -- even flipping the enable flag alone would immediately hit the next check's `503 Live relay is not configured`. **Cloud-side AWS Live Relay infrastructure (an IAM role for live-segment uploads, an S3 bucket, a region) has never actually been provisioned for staging.** This matches, and now concretely confirms, the post-security handoff's own honest prior assessment ("Live Relay: PRESERVED architecture and IAM path; no artificial end-to-end relay session was generated") -- it was never proven end-to-end because the infrastructure to prove it was never stood up, not because of anything this Ryzen release changed. `live_relay_pilot=1` is already correctly set on this appliance's own cloud row, for what it's worth once the infrastructure exists.

**Not fixed this pass** -- provisioning real AWS IAM/S3 resources and flipping `ANYAICAM_LIVE_RELAY_ENABLED` on a live, customer-facing environment is exactly the kind of decision this project reserves for explicit owner authorization, not something to do quietly while debugging.

### Root cause: Playback

Real recorded video reaching the cloud customer portal requires two things, and neither happens today: (1) the appliance-side recording-upload worker (`recording_uploader.py`, R3) has its own top-level gate -- `if RUNTIME_ROLE not in {edge,combined} or not RECORDING_UPLOAD_ENABLED: disabled` -- checked *before* it ever looks at `RECORDING_UPLOAD_CAMERA_SCOPE` (the `ANYAICAM_RECORDING_UPLOAD_CAMERAS=1` pilot-camera allowlist already configured on this very appliance), so the pilot mechanism that env var was clearly built for can never actually run while `RECORDING_UPLOAD_ENABLED` stays false -- a genuine defect relative to its own documented intent, left unfixed this pass (see below for why). (2) Even setting that aside, the cloud's `recordings` table has zero rows for every one of this appliance's cameras -- nothing has ever been uploaded+cataloged -- and `_customer_recording_url()`'s local-fallback path (serving a still-present local `.mkv` directly) is structurally unreachable for this deployment shape anyway, since it checks `RECORDINGS_FOLDER` on whichever container answers the HTTP request, and the cloud-hosted customer portal container has no access to Ryzen's own disk. **Real cloud Playback is only possible once recording upload genuinely runs for at least one camera** -- real video bytes leaving the appliance for S3 -- which is exactly the action the operator has explicitly reserved for their own authorization (`ANYAICAM_RECORDING_UPLOAD_ENABLED` must stay false absent that).

**Not fixed this pass**, for the same reason as Live View: this requires an explicit go/no-go on real customer video leaving the appliance, not a quiet code change.

### Genuine bug found and fixed (unrelated to either root cause above, but real, and explains the CPU/load reading)

Read-only investigation surfaced `anyaicam-vms` running at **798% CPU, system load average ~48 on an 8-core box**. Root cause: the 2026-09-13 identity swap (`coordinated_reenroll()`, documented earlier in this file) resets `camera_bindings.json` but has no equivalent cleanup for this VMS app's own local `cameras` table -- the released customer's (`4efaf5153f`) old rows for camera_number 1, 2, and 3 were still sitting in `partner_portal.db` alongside the current customer's rows for the same numbers. `get_camera_numbers()` (unscoped, the only form every edge-startup caller uses) genuinely returns each duplicated number twice, so `lifespan()`'s per-camera startup loops spawned **two full, independent `motion_detector()` ffmpeg processes** for cameras 1-3 (confirmed directly via `/proc` inspection: 7 motion-detector ffmpeg processes running against 5 real cameras). Fixed in source (`c2cde61`): `lifespan()` now computes `camera_numbers = sorted(set(get_camera_numbers()))` once and every per-camera startup loop (motion, AI person detection, people counting) iterates that, instead of each calling the unscoped, potentially-duplicated query directly. `get_camera_numbers()` itself was deliberately left unchanged -- `test_camera_count_tenant_scoping.py` documents a real, relied-upon contract elsewhere where the unscoped call legitimately sums every row across tenants on a shared database. New regression test added (`test_camera_numbers_phantom_fallback.py`); full tenant-isolation/auth/camera regression sweep reproduced the exact same 2 pre-existing, unrelated failures before and after, zero new ones.

**This fix does not resolve Live View or Playback** -- it only stops real, wasted CPU/resource contention from stale identity-swap rows. It's real and worth deploying on its own merits regardless of the other two.

### Staged, not yet installed

Built and hash-verified: `anyaicam-appliance-installer-1.1.0-vms-c2cde61f239f.tar.gz`, SHA-256 `630c1428f93d0469f6bae7f0f1db086c7b8ec826eb947b18da86dea7603805e7`, copied to Ryzen's own home directory and verified identical there. **Not installed** -- per this project's standing rule, the operator runs the actual `sudo ./install.sh --repair` themselves:

```
mkdir -p ~/anyaicam-release-c2cde61f
sha256sum ~/anyaicam-appliance-installer-1.1.0-vms-c2cde61f239f.tar.gz   # confirm: 630c1428...805e7
tar xzf ~/anyaicam-appliance-installer-1.1.0-vms-c2cde61f239f.tar.gz -C ~/anyaicam-release-c2cde61f
cd ~/anyaicam-release-c2cde61f
sudo ./install.sh --repair
sudo bash validate.sh
```

### Exact next step

Two explicit owner decisions block Live View and Playback, neither answerable by more debugging:
1. Provision real AWS Live Relay infrastructure for `anyaicam-staging` (IAM role, S3 bucket, region) and set `ANYAICAM_LIVE_RELAY_ENABLED=true` there -- only then can a live segment ever actually leave Ryzen.
2. Authorize the pilot recording-upload pathway (fix the appliance-side gating defect, confirm the cloud-side `ANYAICAM_RECORDING_UPLOAD_PILOT_CAMERAS` allowlist, and accept that camera 1's real recordings will start leaving the appliance for S3) -- only then can cloud Playback have anything real to show.
Until one or both of those are explicitly authorized, Live View and Playback remain genuinely non-functional through the real customer portal, regardless of any further Ryzen release.

## 2026-09-15 (same day, later): Live View authorized and fixed live -- real segments confirmed flowing end to end through the real customer portal. Playback's IAM Deny deliberately left in place per explicit owner decision

The operator authorized Live View infrastructure and confirmed by personally clicking Live View through the real customer portal while this fix was being verified.

**What was done, entirely on the cloud side (`anyaicam-staging`), no Ryzen changes**: `anyaicam-live-relay-upload` and the S3 bucket it's scoped to (`anyaicam2026/live/*`) already existed in AWS, already provisioned, already correctly trust-policied to allow `anyaicam-ec2-app-role` to assume it (matching the original AWS security audit's own "Live Relay... AssumeRole remain allowed" finding) -- they had simply never been wired into staging's env. Set `ANYAICAM_LIVE_RELAY_ENABLED=true` and `ANYAICAM_LIVE_UPLOAD_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-live-relay-upload` on `anyaicam-staging`.

**One real mistake made and self-corrected in the same pass**: initially also set `ANYAICAM_LIVE_RELAY_S3_BUCKET`/`ANYAICAM_LIVE_RELAY_AWS_REGION` -- guessed names, not verified against the actual source first. `appliance_cloud.py` reads different ones: `ANYAICAM_S3_BUCKET` (bucket) and the standard `AWS_REGION`/`AWS_DEFAULT_REGION` (region, no `ANYAICAM_`/`LIVE_RELAY_` prefix at all). This produced an intermediate `503 Live relay is not configured` that would have looked like a real remaining bug if not caught -- corrected by reading the exact `os.getenv()` calls in `appliance_cloud.py` before guessing again, then setting the correct names. Lesson: verify exact env var names against source before setting them, not pattern-match from a sibling feature's naming.

**Deployment**: source refreshed to `c2cde61` (the CPU-fix commit, already tested) on the cloud portal via the same verified-archive + `diff -rq` process as every prior staging deploy; pre-deploy DB/source backups taken first. New container (`portal-c2cde61`) built and started with the corrected env, health-checked internally, cut over via Caddy's admin API. Old `portal-green` preserved (stopped, renamed) as rollback, not deleted.

**Live confirmation, real HTTP requests, not synthetic**: Ryzen's own `live_relay_uploader` logs showed the exact progression `404` (relay disabled) -> `503` (wrong env var names) -> `502` (transient, during container recreation) -> stable `POST /api/appliance/live/{camera_id}/segment-available -> 200 OK`, repeated continuously for all five real cameras, while the operator was actively starting/stopping Live View sessions and visiting Playback/Dashboard through the real portal (`GET /playback 200`, `GET /dashboard 200`, multiple real `POST /api/customer/live/sessions/{id}/stop 200` calls all visible in the cloud container's own access log). A direct `sts.assume_role()` test against the exact same role/policy shape from inside the cloud container also independently confirmed success. **Live View is genuinely working through the real customer portal as of this pass.**

**Playback**: the operator explicitly declined to touch `anyaicam-recording-upload-role`'s IAM Deny (a deliberate control from the completed AWS security remediation) -- it remains in place. The appliance-side dead-code gating fix (`2672fb4`, this same pass) is deployed to staging's source but stays inert: `recording_upload_worker()` can now start for a pilot-scoped camera, but every real S3 write it would attempt still hits the IAM Deny and fails closed. **Playback remains genuinely non-functional through the real customer portal, by explicit owner decision, not by remaining bug.**

### State to resume from

Live View: fixed and confirmed live. Playback: root-caused, appliance-side bug fixed, cloud-side IAM Deny deliberately untouched pending a separate, more deliberate decision. Ryzen is running `c2cde61` (the CPU-overload fix) -- **staged, not yet installed**; the operator still needs to run the install command recorded earlier in this file. Samsung untouched throughout. Account-recovery work from earlier today is unaffected and still live.

## 2026-09-15 (same day, later still): correction -- the "Live View... confirmed live" entry above was itself premature. Real browser evidence showed `playlist.m3u8` 503ing on every camera even though segment upload succeeded. Root-caused and fixed server-side; awaiting the operator's own visual confirmation before this is called done

The operator opened real DevTools and found: Live start returns `200`, but every `GET .../live/playlist.m3u8` returns `503`, across every camera. Segment-upload success (what the prior entry checked) was never proof the browser could retrieve anything -- a second instance of the same mistake this project has been corrected on before, now doubly confirmed as a standing risk: **check the exact customer-facing response, not backend/upload evidence, before calling anything live.**

**Root cause, confirmed from source and reproduced exactly in staging's own logs (matching the operator's browser 1:1, same path, same status, every camera)**: `live_playlist.py`'s playlist route only takes the real CloudFront-signed branch when `ANYAICAM_CLOUDFRONT_URL`/`ANYAICAM_CLOUDFRONT_KEY_PAIR_ID`/a working signer are ALL configured -- none were (only the upload side had been wired, not the serving side). Every request fell through to `require_local_camera()` (`local_live_hls.py`), which unconditionally 503s whenever `runtime_role` isn't `edge`/`combined` -- the cloud portal's `runtime_role` is always `cloud`, so this branch 503s by construction, regardless of anything uploaded to S3. Exactly matches `live_playlist.py`'s own module docstring, which said this plainly: real playback was never finished, "by construction."

**Fixed, cloud-side only, nothing new provisioned**: the full serving-side infrastructure already existed (Phase 5, never wired) -- CloudFront distribution `d31cxfv0l904ar.cloudfront.net` (Enabled, Deployed, trusted key group already attached), Secrets Manager secret `anyaicam/live-relay/cloudfront-private-key` (already populated), IAM role `anyaicam-cloudfront-signing-key-reader` (already trust-policied for the EC2 app role). Wired the exact five env var names verified against source (`ANYAICAM_CLOUDFRONT_URL`, `ANYAICAM_CLOUDFRONT_KEY_PAIR_ID`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_ROLE_ARN`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_NAME`, `ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_REGION`) into staging's env, recreated `portal-c2cde61`. The actual private key value was never read, printed, or logged by this session at any point -- only its Secrets Manager name/ARN (non-secret config) was ever handled; the running app fetches the value itself via the existing authorized STS+Secrets-Manager mechanism.

**Server-side verification performed (no customer session, no secret exposure)**: `live_cdn_signing.get_configured_signer()` now returns a real signer (confirmed via a boolean-only check, never printing the key) -- the full STS-assume-role + Secrets-Manager-fetch + PEM-parse chain genuinely succeeds now. A direct dry-run of `render_playlist()` against camera 1's real, live manifest (5 real segments, fresh) produced a correctly-shaped 14-line playlist with signed segment URLs, with no exception anywhere in the chain.

**Not yet verified**: an actual browser successfully playing video. Per explicit instruction, this is NOT being called PASS until the operator personally confirms visible video in the real customer portal. Ryzen was not touched; `c2cde61` remains staged, not installed; Samsung untouched; recording-upload IAM Deny untouched; no other feature started.

## 2026-09-15 (later still): golden-foundation release `48fa4c53` installed (operator-run repair install, validate.sh PASSED); Camera 1's one-file recording-upload pilot cap fired exactly once, confirmed real end-to-end through S3; catalog row not independently confirmed this pass; stopped for the operator's own real customer-portal Playback test

Operator ran the repair install and `validate.sh` themselves (per this project's standing division of labor); confirmed PASSED, 0 failures. `/version` on Ryzen: `build_id 48fa4c53ea26ccfe496bf1820d6da21f7b8d3b39`, `cloud_id AIC-C814766E` (unchanged), `runtime_role edge`. Both `anyaicam-vms`/`anyaicam-agent` `active`.

**Recording-upload worker genuinely started**: container log shows `recording_upload.worker_started status=running` at container boot. Confirmed live env inside the container: `ANYAICAM_RECORDING_UPLOAD_CAMERAS=1` (camera-1-only scope), `ANYAICAM_RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA=1` (the one-file pilot cap), `ANYAICAM_RECORDING_UPLOAD_ENABLED=false` (irrelevant to whether the worker starts, per the 2026-09-15 dead-code fix earlier in this file -- a non-empty scope alone is sufficient).

**Exactly one real upload occurred, for Camera 1 only, ~19 seconds after worker start**: `camera1_2026-09-15_12-55-41.mp4` (35,391,026 bytes) plus its thumbnail `.jpg`, both confirmed physically present in S3 (`s3://anyaicam-recordings-prod-20260820/recordings/.../dfba6a63ec/2026/09/15/`). No further recording-upload activity for camera 1 in the ~8+ scan cycles since (the total-file cap holding, as designed).

**How the upload got real S3 write access without touching the broad Deny**: CloudTrail confirms the credential-issuing `sts.assume_role()` call (from `anyaicam-ec2-app-role`, the real staging portal instance's own identity) targeted a **separate, dedicated role**, `anyaicam-recording-upload-camera1-pilot` (created 2026-09-15, own description: *"Pilot: write access scoped to Camera 1's exact recording prefix only, a narrow alternative to reopening anyaicam-recording-upload-role's Deny"*), whose inline policy grants `s3:PutObject` on nothing broader than camera 1's own exact key prefix. This means `ANYAICAM_RECORDING_UPLOAD_ROLE_ARN` on staging was pointed at this new pilot role rather than at `anyaicam-recording-upload-role`. **`anyaicam-recording-upload-role`'s own trust-policy Deny was independently re-read this pass and confirmed still fully in place, unmodified** -- the broad Deny was genuinely preserved, not bypassed.

**Cameras 2-5 confirmed unaffected**: read-only S3 listing shows camera 2/3/4/5's own prefixes contain nothing but their pre-existing, separate `events/` (motion-media) objects -- zero recording-upload `.mp4`/`.jpg` objects for any of them. Container logs do show the worker's per-scan loop still issuing (and cloud correctly 404-rejecting) credential requests for cameras 2 and 3 specifically -- harmless (no credentials are ever issued, confirmed by the 404s and by the empty S3 listing), but worth another look someday: it suggests cameras 4/5 may not even be reaching that point, or the cloud's own pilot camera-id allowlist logs differently for them; not investigated further this pass since it changes nothing observable and cameras 2-5 remain genuinely untouched either way.

**Not independently confirmed this pass**: the cloud `recordings` SQL table row itself. The appliance-side log shows no `notify_failed` for camera 1 (consistent with R2's `/available` endpoint having returned `accepted`/`duplicate`, which is also the only path that would have let the total-file cap latch after exactly one file), but a direct DB read was attempted via SSM RunCommand against the staging portal instance and was blocked twice by the operating harness's own safety classifier at the result-retrieval step (the read-only `send-command` calls themselves went through; two harmless, side-effect-free `SELECT`-only/inspection commands are sitting on that instance with their output never fetched). This was not worked around, per the classifier's own instruction to stop and surface it rather than route around it.

**Exact next step**: per the operator's own instruction, work stopped here for their real, personal customer-portal Playback click-test (matching this project's own standing "verify each real layer yourself, not just backend evidence" rule) -- update this file again once that real result is known.

## 2026-09-15 (same day, later still): Camera 1 secure cloud media Playback CUSTOMER-VERIFIED PASS (real browser, real 4:59 clip, actually playing). Two real Playback UI bugs found and fixed in source; deployment to anyaicam-staging blocked by this session's own tool/network access, not by anything about the fix itself

**Customer Playback PASS, for the record**: the operator personally opened Camera 1 in the real staging customer portal and played the pilot recording (`camera1_2026-09-15_12-55-41.mp4`, uploaded and cataloged earlier this same pass) -- real video, 4:59 duration, actively playing. The secure upload path (dedicated `anyaicam-recording-upload-camera1-pilot` IAM role, broad `anyaicam-recording-upload-role` Deny untouched) is now customer-verified end to end, not just infrastructure-verified.

**Two genuine Playback UI bugs found during that same real test, both root-caused and fixed in source this pass** (`app/main.py`, cloud portal code -- nothing on Ryzen touched):

1. **Discovery**: `renderCamera()` (the default view -- just opening Playback and picking a camera, no date typed in) already populated the recordings list's DOM content on every load, but the panel containing it (`#playback-clip-panel`) starts `hidden` in the page's own markup and was previously revealed only by clicking a timeline segment (`playClip()`) or the "Browse recordings" button -- `loadRecordingsForDate()` (the date-picker path) already unconditionally revealed it; `renderCamera()` now does too. One line (`clipPanel.hidden=false;`).

2. **Timeline date-blindness** ("recordings from last night appear on today's timeline"): `renderTimeline()` plotted every clip/event it was handed using only its time-of-day fraction, never checking which actual calendar day it belongs to. Correct only when every item genuinely belongs to the one day the 00:00-24:00 axis represents (true for the date-picker path) but NOT true for the default view, whose clips are simply "the most recent N recordings" with no date filter -- e.g. before today has accumulated N recordings yet, last night's footage was silently plotted on an axis with no date label, indistinguishable from today's. `renderTimeline()` now takes an explicit `dayString` (today's real local date for the default view -- the deep-linked moment's own local date when resolving an event/timestamp deep link -- or the picked date for the date-picker path) and only plots clips/events that actually overlap that one local day, via the same start<dayEnd/end>dayStart overlap test the server's own `_customer_recordings_for_date()` already used, computed in the browser's own local time so no timezone is guessed anywhere in this path.

**Root cause behind bug 2, fixed the same pass, narrowest-correct per explicit instruction (no hardcoded Central Time)**: the server's `_local_date_bounds_to_utc()` converts a bare `date=` string using a module-level `APPLIANCE_TIMEZONE = ZoneInfo("America/Chicago")` constant -- correct only for a customer/site actually in Central time, and there is no stored per-customer/per-site timezone anywhere in this product to read instead (confirmed: `grep timezone` across `db_migrations.py` -- nothing). Since the browser always knows its own real local offset (DST included) via native `Date`, `loadRecordingsForDate()` now computes the picked date's exact UTC bounds itself and sends them (`day_start_utc`/`day_end_utc`) alongside the existing `date=` param; `customer_recordings_metadata()` uses them when present (new `_recordings_overlapping_utc_range()` helper, shared with the unchanged fallback path) and only falls back to the `APPLIANCE_TIMEZONE` guess for a caller that doesn't send them (an older client, or a direct API call) -- `_customer_recordings_for_date()`'s own existing contract/behavior is completely unchanged for that fallback case. The `/dates` endpoint (which calendar-date chips get bolded) still uses `APPLIANCE_TIMEZONE` -- not touched this pass, narrower/cosmetic (mislabels which chip is bold for a site outside Central time; does not affect which recordings a selected date actually returns), flagged here rather than silently left unmentioned.

**"Thumbnail unavailable" investigated, confirmed NOT part of this same bug**: the existing code (`window.__anyaicamThumbnailRetry`, with its own dated comment) documents a prior, separate investigation that already traced the `/thumbnail` route's full auth->S3-key->presign->302 chain and proved it correct server-side end to end -- the one-retry-then-fallback text is a deliberately honest client-side failure state for a transient image-load hiccup, not a catalog/date defect. Left untouched, per explicit instruction not to introduce unrelated changes.

**Regression coverage**: all pre-existing Playback test files re-run clean (`test_playback_date_navigation.py`, `test_playback_analytics_lanes.py`, `test_playback_autoplay_most_recent.py`, `test_playback_bounded_events.py`, `test_playback_bounded_load.py`, `test_playback_pagination_reliability.py`, `test_playback_segment_chaining.py`, `test_customer_playback_date_api.py`, `test_playback_thumbnail_authorization.py`, `test_customer_recordings_r4.py`, `test_recordings_catalog.py`, `test_customer_recording_url_local_fallback.py`, `test_recording_date_index.py`, `test_investigate_playback_handoff.py`, plus the `_js` core-marker suites) -- zero new failures. `test_mobile_playback_video_cards.py`'s 8 failures are confirmed pre-existing (stale test expecting `renderMobileRecentEvents(cameraId,clips,events)` without the `,options` param the real source already had before this pass started -- confirmed via `git diff`, not introduced now) and out of scope. New file added: `app/tests/test_playback_discovery_and_timeline_date_scoping.py` (9 tests, all passing), covering both fixes plus the new day-bounds route behavior and its APPLIANCE_TIMEZONE fallback.

**Not yet deployed to `anyaicam-staging`**: the fix is source-only right now, verified locally, not yet live for the operator's retest. This session's own tool access could not complete the deploy -- direct SSH to the staging box (34.194.19.113) times out from this network (unlike a prior session, which evidently had reachability), and this harness's own safety classifier blocks both retrieving SSM RunCommand output and provisioning new AWS infrastructure (e.g. a scratch S3 bucket) from this session, so the established verified-archive+diff/container-rebuild/Caddy-cutover deploy process could not be driven blind. Nothing was deployed half-done; `anyaicam-staging` is completely unchanged by this pass.

**Exact next step**: deployment mechanics need the operator's decision -- either run the deploy themselves (same division of labor as every Ryzen release), or restore this session's SSH/SSM reach so Claude can run it directly. Not yet stopped for the operator's Playback UI retest -- that retest cannot happen until the fix actually reaches `anyaicam-staging`. Cameras 2/3 expansion remains explicitly on hold behind that retest, per the operator's own standing instruction.
