# Windows 1.3.2 acceptance environment: options and recommendation (PREPARED 2026-10-20; nothing created or changed)

Owner constraints (2026-10-20):
- do not change Dell Smart App Control (SAC), delete files or disturb existing VMs;
- no storage purchase, VM creation, EXE build or software install without separate authorization.

## Facts (verified read-only on the Dell, 2026-10-20)

| Fact | Evidence |
|---|---|
| SAC is **enforced** on the Dell (`VerifiedAndReputablePolicyState=1`), and Windows Sandbox inherits it | registry `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy`; the Sandbox diagnostic in every report below |
| An **unsigned** setup is blocked in Sandbox | `anyaicam-release-windows\sandbox-1.3.1-ba3df90-fresh\results\report.txt`: "An Application Control policy has blocked this file" |
| A **signed** setup (Azure Artifact Signing, "Any AI Cam", plus the signed runtime catalog) installs and runs in Sandbox under SAC | `sandbox-1.3.1-7c66928-signed-fresh` (63 PASS), `-signed-simulated-upgrade` (66 PASS), `-signed-upgrade` (63 PASS); each reports "setup signature=Valid" and "runtime catalog registered" |
| Windows Sandbox is enabled; Hyper-V is running with one VM worker, whose owner is not visible to this session (`Get-VM` denied) | `WindowsSandbox.exe` present; `vmms` running |
| C: is the only drive: 32.8 GB free; the shared floor is 8 GB | `Get-PSDrive` |
| The Azure signing client and its metadata are present; no certificate-store code-signing certificate | prerequisite probe (BLUE.md 18:00) |
| Upgrade baselines on disk | 1.3.0 `aedecb6` (unsigned, `c6039f5f…`) and 1.3.1 `7c66928` (signed, `eabddd31…`) |

## Options

| # | Environment | Runs an unsigned 1.3.2? | Changes to the Dell | Needs | Coverage |
|---|---|---|---|---|---|
| **A (recommended)** | **Windows Sandbox on the Dell, with a TEST-signed 1.3.2** (same signing identity as 1.3.1; never published) | not needed (the build is signed) | none: Sandbox is disposable and SAC is untouched; ~4 GB RAM while it runs | owner authorization for a test signing session, the shared lock / time window, Codex review of the exact commit | the full `validate-in-sandbox.ps1` + `acceptance-127.ps1` path already proven with 1.3.1; fresh install, repair, signed 1.3.1 → 1.3.2 upgrade, simulated 1.3.0 leftovers, Local/Hybrid via `test-cloud.py`, downgrade/unknown-version guard, ACLs, Vosk model; discovery/live/recording/playback/reconnect with the ONVIF simulator and MediaMTX test streams inside the Sandbox |
| B | A SAC-off Hyper-V guest on the Dell | yes | a new VM, a private switch and ~25–45 GB of disk; C: would drop toward the 8 GB floor | owner approval to create a VM, a Windows image + licence, and disk (external disk or cleanup) | everything in A **plus** the real unsigned 1.3.0 → 1.3.2 upgrade and a true routed second subnet (with a second "router" guest) |
| C | A separate physical Windows PC with SAC off (owner-provided spare) | yes | none on the Dell | owner provides and authorizes the PC; isolated network | like B; the real unsigned 1.3.0 upgrade |
| D | Turning SAC off on the Dell | yes | **forbidden by the owner** (on Windows 11, turning SAC off has historically not been reversible without a reset) | — | — |

**Recommendation: A**, then B or C only for the two items A cannot cover.
1. The real **unsigned 1.3.0 → 1.3.2** upgrade (U1). A covers it with the existing simulated-1.3.0 leftovers (`SIMULATE-1.3.0`) and the real signed 1.3.1 upgrade. Any PC that installed the public unsigned 1.3.0 necessarily had SAC off or in evaluation mode (SAC blocks that setup otherwise), so U1 belongs on a SAC-off machine: a single targeted run.
2. A **routed** second subnet (below).

**The two unsigned builds the owner asked to prepare** remain useful for static verification without executing them: hashes, `artifact` contents, the packaged-file list, the Inno log, the keyset pin. They also serve options B/C. **Only signed builds can be executed on this Dell.** The test-signing step therefore needs its own owner decision; it is not part of this preparation.

## Simulated cameras and the second subnet

**Gap G1 (ONVIF camera): CLOSED as tooling.**
- `installer/windows/sandbox/onvif-camera-sim.py` (stdlib only; never packaged) answers WS-Discovery Probe (unicast and, with `--multicast`, the 239.255.255.250 group) and GetCapabilities / GetDeviceInformation / GetProfiles / GetStreamUri, with optional WS-Security digest authentication.
- `installer/tests/test_onvif_camera_sim_132.py` runs the agent's **real** `onvif_media.resolve_media_uri` and `discovery._onvif_probe_host`/`_inspect` against it over loopback: 7 tests pass. They cover the full chain, credentials required, wrong password, unauthenticated-first, an ephemeral reply port, and a foreign identity ignored.
- Pair it with MediaMTX (vendored) + FFmpeg `testsrc2` (vendored) publishing `rtsp://<camera-ip>:8554/cam1` in the same guest.

**Gap G2 (second subnet):**

| Level | How | Proves | Where |
|---|---|---|---|
| G2a (Sandbox, option A) | inside the disposable Sandbox, add a second address in another subnet to its adapter (`New-NetIPAddress 10.20.30.40/24`) and run the simulator + test stream bound to it | add-by-IP to an address that is **not** in the ARP table (the `ip-` device_key path), unicast probe, RTSP probe, verification at the stored address, Reconnect | Sandbox only; nothing changes on the Dell |
| G2b (true routed subnet) | two Private Hyper-V switches and a small router guest (or two guests), with the camera simulator behind the router | real L3 routing, ARP absence across a router, the Windows Firewall behaviour for a reply from another subnet | needs option B (VM creation authorization) |

G2a is the safe default. G2b is needed only if Codex/owner require proof across a real router.

## Proposed acceptance sequence (each step needs its own go-ahead)

1. Owner: authorize the two unsigned builds (static verification) **and** decide on test signing for option A.
2. Build from the exact Codex-reviewed commit (`docs/windows-1.3.2-exe-build-checklist.md`, gates 1–4), shared lock, ≥ 12 GB C:.
3. Codex verifies the artifacts (hashes, contents, signature if signed).
4. Option A runs in Sandbox:
   - fresh / repair / guard cases (`acceptance-127.ps1`);
   - the signed 1.3.1 upgrade;
   - the simulated 1.3.0 upgrade;
   - Local + Hybrid against `test-cloud.py`;
   - cameras with the simulator, MediaMTX and G2a.
5. Results with SHA256SUMS to Codex for the final acceptance verdict.
