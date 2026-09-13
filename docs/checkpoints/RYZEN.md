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
