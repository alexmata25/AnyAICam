# Windows 1.3.2 acceptance VM: requirements and setup plan (PREPARED; nothing created or started)

Owner instruction 2026-10-19: prepare only. The VM is created and started only after resource availability and ownership
are confirmed.

## Why a VM and not Windows Sandbox

Smart App Control is enforced on the Dell and inherited by Windows Sandbox (`VerifiedAndReputablePolicyState=1`).
The unsigned 1.3.2 setup is blocked there ("An Application Control policy has blocked this file",
`anyaicam-release-windows\sandbox-1.3.1-ba3df90-fresh\results\report.txt`). So is the published unsigned 1.3.0,
which the upgrade test needs as its starting point. A dedicated Hyper-V guest with Smart App Control **off** runs both
without any signing.

## Dell resources measured 2026-10-19 ~20:40 CDT

| Resource | Available | VM needs | Gap |
|---|---|---|---|
| RAM | 31.5 GB total, **5.6 GB free** (`AnyAiCam-Linux-Test` holds 8 GB; Docker/WSL and other sessions) | 6 GB static | **Blocked**: starting it would leave < 1 GB and break the shared ≥ 4 GB rule. Needs that VM stopped by its owner, or a scheduled window. |
| Disk C: | **36.6 GB free** (only drive) | ISO/VHD 6 GB + guest disk ~30 GB used (dynamic VHDX, 80 GB max) | **Blocked**: it would cross the shared 8 GB floor. Needs an external disk, or approved cleanup that frees ≥ 40 GB. |
| CPU | 16 logical | 4 vCPU | OK |
| Hyper-V | enabled; switches `Default Switch`, `AnyAiCam-Release-Test-NAT`, `FSE Switch` (all owned by others) | its own **Private** switch | Create `Blue-Win132-Isolated` (Private: no host or internet path). |

Existing VMs `AnyAiCam-Clean-Install-Test` and `AnyAiCam-Release-Test` belong to other sessions (created 2026-10-13 for
Linux work) and are never reused or modified.

## Guest specification

- **Image:** Windows 11 Enterprise evaluation (90 days, Microsoft Evaluation Center) or a Windows 11 Pro ISO with an
  owner-provided licence. Owner decision: which image and who downloads it.
- **Hyper-V:** Generation 2, Secure Boot (Microsoft Windows template), **vTPM on** (Windows 11 requirement), 4 vCPU,
  6 GB static RAM, 80 GB dynamic VHDX on the approved disk. Name `Blue-Win132-Acceptance`.
- **In the guest:**
  - local administrator plus one standard user (for the ACL, planting and claim-link checks);
  - **Smart App Control set to Off** (Windows Security > App & browser control). This is irreversible for that guest,
    which is acceptable for a disposable test VM;
  - Defender on;
  - Windows Update paused;
  - no Microsoft account.
- **Network:** Private switch only. The in-guest test cloud (`installer\windows\sandbox\test-cloud.py`) serves Hybrid;
  the production names (`app`/`portal`/`portal-staging.anyaicam.com`, `anyaicam.com`) are pinned to 127.0.0.1 in the
  guest hosts file. The production cloud is never contacted, and no AWS, Stripe or Bluehost endpoint is reachable.
- **Checkpoints:**
  - `00-clean`: OOBE done, SAC off, no AnyAiCam;
  - `01-with-1.3.0`: the published 1.3.0 `aedecb6` installed (SHA-256 `c6039f5f…`, from the existing candidate folder,
    not re-downloaded);
  - every run starts by applying one of them.

## Simulated cameras (no LAN cameras, no real customer devices)

- Inside the guest, MediaMTX (the pinned 1.21.0) plus FFmpeg `testsrc2`/`sine` publish two RTSP streams with
  authentication. This is enough to drive recording, HLS/WebRTC live view, playback/export, motion events and YOLO
  analytics (a person or vehicle clip from a public-domain sample).
- ONVIF WS-Discovery has no simulator in the repository yet. A small in-guest responder for discovery and media profiles
  needs to be written and reviewed, or discovery is checked by hand against one real camera with owner approval.

## Acceptance runs (each one needs owner approval)

| Run | From checkpoint | Covers |
|---|---|---|
| A1 production keyset, Local | 00-clean | silent and interactive install; services; ACLs; firewall; keyset pin; 0 cameras before a link; downgrade guard; uninstall keeps data |
| A2 production keyset, Hybrid | 00-clean | the above plus the agent and ANYAICAM_STATE_DIR (the credential is found); a foreign-signed snapshot is refused |
| A3 synthetic keyset, Local | 00-clean | signed capacity N/N+1, expiry, tamper, identity, replay; simulated cameras: record, live view, playback, motion events; analytics ON/OFF and availability (LPR/PPE "not available on this computer") |
| A4 synthetic keyset, Hybrid | 00-clean | test-cloud link; cloud backup, event media and P2P against the test cloud; Software Update shows "run the new setup" |
| U1 upgrade | 01-with-1.3.0 | 1.3.0 → 1.3.2 keeps recordings, accounts and settings; the claim code is retired; capacity starts at 0 until linked (documented) |
| R1 restarts | after A3 | VMS and agent restarts, guest reboot, crash recovery |

The existing Sandbox scripts (`validate-in-sandbox.ps1`, `acceptance-127.ps1`, `make-wsb.ps1`) run unchanged inside
the guest from a mapped ISO or VHD (no shared folder to the host). Results are copied out by the owner or a reviewed
script.

## What must be confirmed before creation

1. Owner approval to create the VM and its private switch.
2. A disk with ≥ 45 GB free, or approved cleanup.
3. RAM: `AnyAiCam-Linux-Test` stopped by its owner, or a time window.
4. The Windows image and licence.
5. The Docker/heavy-job lock is respected while the VM runs; the VM counts as one heavy job.
