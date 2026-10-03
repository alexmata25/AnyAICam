# Samsung appliance — checkpoint
## 2026-10-03 — fresh Samsung verified; real isolated lab preflight passed

User resumed after completing physical wipe/install. New SSH 100.76.177.87,
hostname alejandro-mata-930MBE, independently supplied ED25519 fingerprint matched.
Samsung model 930MBE and original NVMe serial 193077800308 positively matched.
Ubuntu 24.04.4 LTS amd64; i7-8565U 4 cores/8 threads; ~16 GB RAM; 465,443,811,328
root bytes free, exact 100 GiB gate PASS. No old AnyAiCam account/units/standard
state directories/Docker executable; no old identity restored.
Existing candidate installer remains unchanged and unpublished, SHA-256 9dd0fe3f...
Disposable EC2 lab now runs real HTTPS portal + synthetic camera with fresh
keys/database, one customer-scoped owner/location and 8-camera local fixture license.
18/18 actual helper preflight assertions pass; 45 changing video frames decoded.
Archive/710 installer manifest entries and simulator/269 checksum entries verified.
These are NOT Samsung VMS/claim/live/recording/playback acceptance.
Samsung GNOME preparation terminal currently awaits LOCAL sudo password entry;
user was asked to enter it there, sign in using local test credential file and
download the exact installer. No AnyAiCam install has begun. Camera tunnel client
is not activated yet. Do not bypass password, TLS, claim or CSRF protections.
Full current evidence: ../../validation/installer-track/SAMSUNG_FRESH_OS_LAB_PREFLIGHT.md.
Exact cleanup inventory: ../../validation/installer-track/AWS_LAB_RESOURCES.json.
All older pause/offline/old-OS notes below are historical. No excluded systems changed.

## 2026-10-03 — remote steps paused for physical USB installation

User reports Samsung is booting USB and explicitly requests remote steps pause
until fresh Ubuntu installation and network return. No further remote commands.
ISO download SSH session disconnected at ~97%; ISO checksum/USB write/readback
are unconfirmed. Operator must verify live USB Ubuntu 24.04.4 before system erase.
No agent system-disk erase or new OS/VMS install is claimed.
User authorized disposable isolated EC2 helper. New VPC/network/key/instance created,
no portal or simulator provisioned. Helper i-0360bac15559416c1 remains running.
Exact resource cleanup ledger: ../../validation/installer-track/AWS_LAB_RESOURCES.json.
Physical evidence: ../../validation/installer-track/SAMSUNG_PHYSICAL_RESULTS.md.
Do not resume remote steps until the operator reports installation/network ready.

## 2026-10-03 — authenticated media preparation; USB identified and authorized

Samsung remains positively verified, with the same NVMe serial 193077800308.
User confirmed physical-console help and erasure of dedicated USB. Exact USB is
General UDisk serial 2408200641348662589024, 31,457,280,000 bytes.
Ubuntu CD Image signing fingerprint 843938DF228D22F7B3742BC0D94AA3F0EFE21092
authenticated SHA256SUMS.gpg. Exact Ubuntu 24.04.4 Desktop ISO download is underway
on Samsung. Guarded media writer is open on Samsung's physical desktop; local sudo
password required, no password/private key through chat. Writes authorized USB only.
No internal system-disk erase, Ubuntu install, VMS install or physical acceptance
success is claimed. Isolated portal/helper/network placement remains unresolved.
Dell host/services/networking unchanged; no excluded systems accessed.
Current evidence: ../../validation/installer-track/SAMSUNG_PHYSICAL_RESULTS.md.
Earlier read-only/offline statuses below are historical, superseded by this entry.

## 2026-10-03 — Samsung now verified online; destructive execution not started

SSH via Samsung-specific key succeeded. Verified hostname anyaicam-samsung,
SAMSUNG model 930MBE/P02AHK, Intel i7-8565U (4 cores/8 threads), ~16 GB RAM,
Ubuntu 24.04.5 LTS, UEFI. System disk /dev/nvme0n1, SN520 SDAPNUW-512G-1004,
serial 193077800308, 512,110,190,592 bytes; root nvme0n1p2 ext4, EFI p1.
447,182,098,432 bytes currently available (~416.47 GiB); after-reinstall check pending.
VMS image anyaicam-vms:f5a6d87 healthy/running; existing agent/VMS/watcher/config state.
Read-only only: no file changes, stops, uploads, reboot, erase or installation on Samsung.
Sudo needs authentication; independent console/USB boot path and isolated helper
remain unconfirmed. Wipe authorization persists; blocked on these prerequisites.
Full evidence: ../../validation/installer-track/SAMSUNG_INVENTORY.md.
Earlier offline observations below are historical and superseded.

## 2026-10-03 — physical clean-test authorization; target currently unreachable

The user now explicitly authorizes wiping/reinstalling the dedicated Samsung for
clean installer validation and waives preservation. This supersedes the historical
no-reimage instructions below; do not infer that a wipe has happened.

Authorized SSH: alejandro-mata@100.99.177.7. Read-only Tailscale status identifies
anyaicam-samsung/Linux, offline, last seen 2026-09-30T13:19:13.1Z. Windows and existing
WSL SSH attempts timed out. DMI/hardware identity, system disk and Ubuntu version
have NOT been positively verified; no Samsung command or destructive action ran.
A physical console/independent boot path is required before root-disk erasure.
Dell host configuration and existing sessions remain unchanged; no Hyper-V/restart.

Active procedure: ../../validation/installer-track/CLEAN_MACHINE_PROCEDURE.md.
Exact archive remains unpublished installer 1.1.1/vms a1c81e0f4926, SHA-256
9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac.
Historical inventory below is unverified until actual Samsung access is restored.


**Read `docs/PROJECT_CHECKPOINT.md` first.** This file assumes that context.

## Do not restart Samsung setup from scratch

Samsung is **waiting at the cloud identity/activation stage** — it is not a
clean/unconfigured device, and it must not be treated as one. Every future
session must read this file, verify current state, and resume from the next
action below — never re-run the installer's clean-install path, never
re-image, never re-run camera discovery from zero, unless Samsung's actual
current state (checked live) turns out to genuinely be `clean` per the
runbook's own `detect_install_state` check.

This session (2026-09-11, reconciliation) did **not** touch Samsung in any
way — no SSH, no commands, nothing. Nothing here has changed since the last
session that actually worked on Samsung.

## What is documented and proven so far

- `docs/samsung-installation-runbook.md` is the step-by-step install/repair
  procedure for Samsung specifically. As of the runbook's own header, every
  command in it had been validated twice against the same installer package
  on disposable EC2 instances (`docs/phase1-edge-validation-report.md`,
  `docs/phase1-privileged-watcher-e2e-validation-report.md`) — **not**
  against Samsung itself at the time it was written.
- The claim/activation flow it depends on (`first_enroll`,
  `coordinated_reenroll`, `_finish_enrollment` in
  `appliance-agent/anyaicam_agent/setup_wizard.py`) is the
  `staging/cloud-integration-repair` implementation, carried into the golden
  branch (`reconcile/golden-foundation-20260911`) in this session's
  reconciliation work.
- A prior real incident on Samsung is on record: a suspend/hibernate bug
  took down LAN, SSH, and Tailscale simultaneously by putting the whole
  machine to sleep. `disable_system_suspend()` was the fix for that,
  referenced in the runbook's §0 step 2. Confirm this is still in effect
  before relying on remote access to Samsung.
- Samsung's `talk_isapi_diagnostic` setting must stay enabled throughout any
  work — this has been an explicit standing constraint across sessions, not
  something to toggle for a test.

## Current real-world state (per direct instruction this session)

Alejandro has stated directly that Samsung is currently sitting at the cloud
identity/activation stage of setup — i.e., past initial install/discovery,
not yet claimed/activated against the cloud (`anyaicam-staging` or
production, whichever is intended for Samsung). **This checkpoint has not
independently re-verified that against live Samsung console/SSH access this
session** (Samsung was explicitly off-limits this session) — the next
session that actually resumes Samsung work must confirm this first via the
runbook's own §0/§2 detection steps before proceeding, per this project's
standing rule of verifying a checkpoint against live state rather than
trusting it blindly.

## Exact next action

1. **Verify, don't assume.** Before doing anything else: confirm
   Tailscale + LAN SSH connectivity (runbook §0 steps 2-3), confirm the
   currently-running image (`docker inspect anyaicam-vms --format
   '{{.Image}}'`), and run `detect_install_state` (runbook §2) to confirm
   Samsung is actually still at `existing`/partial-activation and not
   something else.
2. **Back up before touching anything** — runbook §0 step 1
   (`partner_portal.db` backup, copy of `docker-compose.yml` +
   `vms.env` off-box, note the current image) — even though Samsung is
   mid-setup, it may already have real customer/camera state worth
   preserving.
3. Resume from wherever the claim/activation step actually is — do not
   re-run install from §1 unless step 1 above proves a clean/partial state
   that genuinely requires it.
4. Once the golden build (`docs/PROJECT_CHECKPOINT.md` → Authoritative
   branch) has an actual versioned artifact, Samsung should be brought up
   on **that** artifact through the normal installer/update mechanism —
   never hand-patched to resemble Ryzen or staging. If Samsung hits a bug
   that Ryzen already hit and fixed, check whether the golden release
   actually contains that fix before re-fixing anything on Samsung directly.
5. Update this file immediately after any session that changes Samsung's
   real state, before ending that session.
