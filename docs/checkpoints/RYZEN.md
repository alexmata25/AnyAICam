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
