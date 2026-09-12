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

**Three source commits now exist that have NOT been applied to Ryzen's
currently-running software**: `88c87a1`, `55fa281`, `25e2fc1`. Ryzen's
`/opt/anyaicam-agent` venv still runs the pre-`25e2fc1` code — the
stale-drop-in cleanup was applied directly (see above), but the
`service.py` fix has not been. **Practical implication, reasoned through
but not yet acted on**: claiming Ryzen right now, even on this
not-yet-updated agent code, would very likely still work — the crash
loop is specific to the *pre-claim* window (`__init__` re-reads
`credential.json` fresh on every process start, so once a real claim
completes and `_finish_enrollment()` restarts the service, the OLD code's
own `if not credential: raise` check already passes cleanly, same as it
must have on every prior successful activation across this whole
project). What remains genuinely unresolved is *how* to get `25e2fc1`
onto Ryzen: a new RC + real repair-path install (matching this project's
own established discipline), vs. some faster in-place package update —
this has not been decided and needs an explicit decision, not an
assumption, before claim proceeds.

**Appliance identity has changed again**, independent of any of the
above: the RC3 install's own identity is `637ad320-daaa-436e-89c9-70a84f4f54a9`
(distinct from RC1's `99c44cb8-...`) — read live via `sudo cat
/etc/anyaicam/appliance_identity.json` to reconfirm before use, per this
file's own standing rule that a value which can change with a future
install isn't safe to trust without re-checking.

## Exact next step

1. **Decide how `25e2fc1` (and the already-two-commits-behind `88c87a1`/`55fa281`)
   reach Ryzen** before claiming — build a new RC and do a real
   repair-path install (consistent with every other fix this project has
   made), or make an explicit, deliberate decision to claim first on the
   current (not-yet-updated) agent code, reasoned through above as very
   likely still safe for the claim step itself. This is a real decision
   point, not something to default silently in either direction.
2. Proceed through the actual claim/activation flow (`anyaicam-setup
   --claim` or interactive) — this establishes a **new** cloud identity;
   do not attempt to reuse any prior `cloud_id`/`customer_id` from before
   either wipe (RC1's `99c44cb8-428d-44e4-a1bf-41f95fd2e268` installer
   identity, or the pre-reconciliation real activation
   `AIC-C90CF0C9`/`4efaf5153f` -- neither applies to this appliance
   anymore).
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
