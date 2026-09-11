# Ryzen appliance — checkpoint

**Read `docs/PROJECT_CHECKPOINT.md` first.** This file assumes that context,
and in particular the "RC1 → RC2 on Ryzen" and "RC2 → RC3 on Ryzen"
sections there.

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

## Current real state (as of the 2026-09-11 RC2 install)

- **Disk**: was 100% full / 0 bytes free before the first wipe; freed to
  346GB. After RC1's uninstall + RC2 install + removal of two stale,
  unrelated `test1`-project Docker volumes (`anyaicam-test1_test1_hls`/
  `_recordings`, ~962MB, confirmed 2+ weeks old): **405GB free, 8% used
  (28GB)**, fully accounted for (Docker build cache ~2.85GB + systemd
  journal ~1.8GB, both left alone as unrelated to AnyAiCam).
- **RC1** (`1dfcbf2`) was installed clean, but its `validate.sh` failed on
  a real source defect (`configuration_issues()` not being
  `RUNTIME_ROLE`-aware). Ryzen was purged again (`uninstall.sh
  --purge-all`) before RC2.
- **RC2** (`34d9d5c`) fixes that defect and **is currently installed and
  running on Ryzen right now** (`anyaicam-vms.service`/
  `anyaicam-agent.service` both active). Confirmed live via raw `/ready`
  diagnostics: `self_test.ok: true`, `configuration_valid: true`, 0
  critical issues -- the RC1 defect is genuinely fixed. RC2's own
  `validate.sh` still reported one failure, but it was `validate.sh`
  itself that was wrong (see PROJECT_CHECKPOINT.md's "RC2 → RC3 on
  Ryzen"), not the running software.
- **RC3** (`4ade235`) fixes that `validate.sh` defect (installer-only
  change; VMS image identical to RC2's) and is built, but **has not been
  transferred to or installed on Ryzen yet**.
- **Ryzen has NOT been touched since RC2's `validate.sh` failure was
  diagnosed** -- explicitly no patches, no restart, no config changes, no
  camera discovery, no claim attempt. It is sitting exactly as RC2's
  installer left it.
- **Appliance identity**: RC2's install issued a fresh
  `installer/09-identity.sh` UUID (distinct from RC1's
  `99c44cb8-428d-44e4-a1bf-41f95fd2e268`, which no longer applies -- get
  the current one via `sudo cat /etc/anyaicam/appliance_identity.json` if
  needed; not re-recorded here since it will change again once RC3 goes
  through its own clean install). This is installer identity only, not a
  cloud claim/activation -- no `cloud_id`/`customer_id` exists.
- **Cameras**: zero configured, zero discovered, zero recording -- by
  design, this is still a pre-claim, pre-discovery installer validation
  appliance, not yet a functioning camera system.
- **Live Relay / Motion Cloud / customer portal / Live View**: not yet
  validated against RC2 or RC3 -- blocked behind claim + camera discovery,
  neither of which has happened yet.

## Exact next step

1. Deploy RC3 (`4ade235`) to Ryzen. Since RC2 is currently installed
   (not wiped), `install.sh`'s `detect_install_state()` should detect
   `existing` and go through the repair path automatically -- confirm
   this is what actually happens rather than assuming; do not manually
   force a mode. Transfer the artifact (SHA-256:
   `f8797d627a580363f2f1807c9a7d9b91f29c73df84a4348b30fee49afc56e5c7`,
   see PROJECT_CHECKPOINT.md), verify on both ends, `sudo bash
   install.sh`.
2. Run `validate.sh` again; confirm ALL checks pass this time, including
   the fixed ready-endpoint check.
3. Proceed through the actual claim/activation flow (`anyaicam-setup
   --claim` or interactive) — this establishes a **new** cloud identity;
   do not attempt to reuse any prior `cloud_id`/`customer_id` from before
   either wipe.
4. Reconnect the 5 physical cameras through the supported discovery
   workflow (not manual CAMERA{n}_* env vars).
5. Validate, in order: recording, motion detection/event media, Motion
   Cloud upload (once camera + cloud identity + AWS config all exist),
   customer portal camera mapping/status, Live View (both grid and
   dedicated single-camera pages), restart persistence, and specifically
   **defect #3** (a stale `agent.env`/`vms.env` activation value must not
   override the current persisted activation after a restart — this is
   closed in source as of `1dfcbf2`, but has never been checked against
   Ryzen's *actual* real hardware/restart behavior, only against the
   regression test suite).
6. Update this file again once any of the above changes real state on
   Ryzen — a checkpoint that isn't updated is worse than none, per
   `docs/PROJECT_CHECKPOINT.md`'s own standing rule.
