# Installer track results — 2026-10-03

## Isolation and artifact

Prepared only in codex/investor-installer-validation-20261003. Runtime/installer
source is unchanged at d5af9c53cbef1f2c400f56e148c5cb080770dc07. Only validation/
preparation files are added. No Hyper-V enablement/restart, host networking change,
WSL shutdown, existing service interruption, full OS install, public upload,
portal provisioning, account/database creation or excluded-system access occurred.

Built investor artifact in ignored dist/installer-track/investor/:
anyaicam-appliance-installer-1.1.0-vms-d5af9c53cbef.tar.gz (installer 1.1.0)
36,465,857 bytes; SHA-256:
45ce04ff237d55faff194d791f3a6a0aef6a7aa8384f7ef152a51f18298fef87.
Embedded installer/VMS source commits are the exact requested full commit.
Embedded VMS release-source hash:
bc2b91c67d060c9b6c2330c862f7dc78b643a55a5329dc17e5c009e0c2f95392.
Verified embedded manifest (709 files), required launcher/install/recovery files,
root script POSIX executable bits and release identity. MediaMTX v1.21.0 binary
included; upstream release checksum and GitHub asset digest matched.
SHA256SUMS and candidate.json accompany the archive. Nothing was published.

## Preliminary results, not real-machine acceptance

WSL Ubuntu-24.04: Ubuntu 24.04.4, x86_64, PID1 systemd, 16 visible CPUs, ~15 GiB
usable RAM. Read-only OS/CPU/RAM/architecture/DNS and payload preflight passed.
Docker is active/enabled; no Docker container or VMS service was installed/started.
Root filesystem reported 508,713,451,520 bytes available (~473.77 GiB), but WSL's
thin VHD figure is not a guarantee of physical Dell disk space (~289 GiB observed).

- WSL Python: product modes 14, rollback 10, builder 16, agent claim 34,
  customer_setup 22: **96 passed**. Test fixtures simulate claim/OS installation.
- Existing shell harness: **165 passed, 4 failed** as shipped when unprivileged;
  failures diagnosed as unredirected /var/lib/anyaicam/rollback fixture writes.
  Temporary copy redirecting only ROLLBACK_DIR: **169 passed, 0 failed**.
  No pinned source was changed; do not describe the original harness as all-pass.
- Non-root launcher freshness/package verification and short-lived loopback claim
  bridge passed in WSL. Root invocation correctly refused before installation.
- Windows local fixture/browser/API regression: **19 passed**, covering download,
  authenticated owner/site confirmation, handoff and manual recovery, CSRF and
  device possession. These tests use a mocked OS-install step and isolated fixture
  databases; they are not the unprovisioned final test portal or real installation.
- Prepared simulator contract: **3 passed, 1 expected failure** in Windows and WSL;
  standard SOAP data, stable endpoint identity, unique discovery message IDs,
  response correlation, mutation refusal and narrow private network guards tested.
- MediaMTX --version executed in WSL: v1.21.0. No server started.
- FFmpeg generated a 1-second H.264 1280x720/15fps temporary sample; ffprobe matched.
  No RTSP listener, ONVIF multicast service or real-camera connection was started.
- Integer storage cases: 99 GiB refused; 100/101 GiB accepted. No disk was filled.

## Findings against the unchanged pinned candidate

1. Storage precision: free_gb_root uses df -B1G, which rounds up. A simulated
   99 GiB + 1 byte is reported as 100 and accepted. This is below a strict 100 GiB
   minimum. Boundary tests are read-only fixtures, not actual disk-pressure tests.
   A separate byte-precision installer fix is needed before asserting that strict
   requirement; the pinned archive was deliberately not changed.
2. Test isolation: the ownership shell fixture leaves ROLLBACK_DIR at the system
   default and fails under an unprivileged user before reaching ownership checks.
   Temporary isolated redirection proves the existing checks pass; fix the harness
   in a separate candidate before running it with privileges on a real machine.
3. Discovery identity: app/onvif_discovery.py searches the first UUID anywhere in a
   discovery response. A valid unique MessageID can be mistaken for the stable
   EndpointReference device UUID. This is the recorded expected-failure test.
   The agent discovery parser targets Address correctly; affected live path and
   actual discovery/add behavior must be checked on the VM. Do not mask the issue
   by replacing standard unique message IDs with a camera ID in the simulator.

## Prepared dependencies, portal and deferred VM

MediaMTX, FFmpeg main .deb, ONVIF implementation/config and exact pinned schema
source hashes are in dependencies.lock.json. ffmpeg-runtime.lock.json inventories
194 installed package versions and 214 shared-library checksums. Cached apt data
provides archive hashes for 193 packages; old installed libapparmor1 has no archive
hash in that cache. Transitive archives are not downloaded/authenticated as an
offline repository. Resolve this gap or approve a fully pinned fresh-guest
baseline before claiming offline readiness. Ubuntu ISO metadata is pinned but the
ISO and its signing-key verification remain pending.

Portal env/owner-location plan are non-deployable preparations only: guest-local
SQLite schema from the pinned source, new secrets/CA, exact HTTPS origin/host,
CSRF and secure cookies, scoped owner/site and 8-camera fixture license. No real
payment credentials, staging data or account bindings. No database initialization.
Claim proof encryption, owner authentication/authorization, site checks, possession,
nonce/origin/expiry and explicit confirmation remain unchanged.

CLEAN_MACHINE_PROCEDURE.md includes the deferred administrator commands and full
acceptance checklist. Proposed appliance VM: Ubuntu 24.04.4 x86_64 Desktop,
Generation 2, 4 vCPU, fixed 12 GB RAM, 180 GB disk, private switch; separate helper
VM serves portal and camera simulator. Helper dependency proxy/cache and temporary
Default Switch access require separate approval and validation. No host bridging.

Still unproven: real sudo install/package dependencies, guest identity permissions,
Docker/VMS/agent startup, actual owner enrollment after install, RTSP/ONVIF network
interoperability, live view/recording/playback/analytics, reboot persistence,
uninstall/reinstall and valid prior-image rollback. All need a real fresh VM.
WSL is smoke testing only. Windows native installer work remains gated on Ubuntu
acceptance. This archive is neither published nor production-ready.
