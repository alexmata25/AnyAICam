# Ryzen appliance — checkpoint

**Read `docs/PROJECT_CHECKPOINT.md` first.** This file assumes that context,
and in particular the "RC1 → RC2 on Ryzen" section there.

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

## Current real state (as of the 2026-09-11 clean install)

- **Disk**: was 100% full / 0 bytes free (blocking any install) before the
  wipe; ~13 abandoned validation Docker images (~9.5–9.9GB each, 2+ weeks
  old, 0 containers referencing them) were pruned, then the full purge
  above reclaimed the rest. Post-wipe: **346GB free, 21% used** — plenty
  of headroom now.
- **RC1** (`1dfcbf2`) was installed clean via the real installer artifact
  (`installer/build_release_installer.py` → scp → SHA-256 verify → `sudo
  bash install.sh`) — no manual patches. Install succeeded
  (`detected state=clean`), but `validate.sh` failed: `GET /ready` 503,
  root-caused to a real source defect (configuration_issues() not being
  RUNTIME_ROLE-aware — see PROJECT_CHECKPOINT.md). **RC1 was never
  claimed/activated and never had cameras discovered** — the validation
  failure was caught before either of those steps.
- **RC2** (`34d9d5c`) fixes that defect, is built and locally
  smoke-tested, but **has not been deployed to Ryzen yet**.
- **New appliance identity issued by the RC1 install**:
  `99c44cb8-428d-44e4-a1bf-41f95fd2e268` — this is `installer/09-identity.sh`'s
  generated UUID, not a cloud claim/activation. No `cloud_id`/`customer_id`
  exists yet; the appliance is unclaimed.
- **Cameras**: zero configured, zero discovered, zero recording. The
  previous 5-camera ONVIF/RTSP configuration was part of what got wiped
  and has not been recreated. Reconnecting the 5 physical cameras must go
  through the supported discovery workflow once RC2 (or later) is
  installed and reachable — **do not attempt to restore old camera config
  or the old cloud identity to "make it work faster"**; this was
  explicitly set up as a from-scratch new-appliance test.
- **Live Relay / Motion Cloud / customer portal / Live View**: none of
  this has been re-validated since the wipe — it was all real and working
  before, but that state is gone along with the recordings. Re-validate
  from scratch once cameras are reconnected and the appliance is claimed.

## Exact next step

1. Deploy RC2 (`34d9d5c`) to Ryzen via the same supported installer path
   used for RC1 (build artifact already exists — see PROJECT_CHECKPOINT.md
   for the exact SHA-256; transfer, verify, `sudo bash install.sh`, expect
   `detected state=existing` or similar repair-path detection since RC1's
   install left files in place — verify `install.sh`'s own state detection
   handles this correctly rather than assuming).
2. Run `validate.sh` again; confirm `GET /ready`'s `configuration_valid`
   check now passes (0 critical issues) for the production+edge shape.
3. Proceed through the actual claim/activation flow (`anyaicam-setup
   --claim` or interactive) — this establishes a **new** cloud identity;
   do not attempt to reuse `AIC-C90CF0C9`, which belonged to the wiped
   install.
4. Reconnect the 5 physical cameras through the supported discovery
   workflow (not manual CAMERA{n}_* env vars).
5. Validate, in order: recording, motion detection/event media, Motion
   Cloud upload (once camera + cloud identity + AWS config all exist),
   customer portal camera mapping/status, Live View (both grid and
   dedicated single-camera pages), restart persistence, and specifically
   **defect #3** (a stale `agent.env`/`vms.env` activation value must not
   override the current persisted activation after a restart — this is
   now closed in source as of `1dfcbf2`, but has never been checked
   against Ryzen's *actual* real hardware/restart behavior, only against
   the regression test suite).
6. Update this file again once any of the above changes real state on
   Ryzen — a checkpoint that isn't updated is worse than none, per
   `docs/PROJECT_CHECKPOINT.md`'s own standing rule.
