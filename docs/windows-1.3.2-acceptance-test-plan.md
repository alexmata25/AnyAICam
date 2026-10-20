# Windows VMS 1.3.2 acceptance-test plan (PREPARED 2026-10-20; nothing run)

Blue, for Codex and the owner. This plan covers the areas the owner listed: installation, upgrades, Local/Hybrid,
camera discovery, Live View, recording, playback, reconnect and offline licensing. It also covers premium analytics,
restarts, security and Software Update.

Each case names:
- the automation that already exists (`installer/windows/sandbox/validate-in-sandbox.ps1` = **VS**,
  `acceptance-127.ps1` = **AC**, or the host unit suites);
- or what is still manual.

Nothing here runs without the owner approvals listed in section 1.

## 1. Entry criteria (all required)

| # | Criterion | Status 2026-10-20 |
|---|---|---|
| E1 | Exact source commit reviewed by Codex (PASS) | requested; see `CODEX-REVIEW-WINDOWS-1.3.2.md` (anyaicam-blue / HANDOFF) |
| E2 | Heavy regression of that commit: Linux 8 shards of the Windows tree and the Windows installer suite with a real Inno compile | not run (Red's RC8 work has priority) |
| E3 | Owner go-ahead for **two unsigned test builds** (`docs/windows-1.3.2-exe-build-checklist.md`): (a) production public keyset `2389a655…`; (b) synthetic test keyset `629ac8e4…`, labelled DO-NOT-SHIP | not given |
| E4 | A machine that can run an unsigned setup. Smart App Control is ON on the Dell (`VerifiedAndReputablePolicyState=1`) and in Windows Sandbox, so either: (a) the Hyper-V guest with SAC **off** (`docs/windows-1.3.2-acceptance-vm-plan.md`); or (b) an authorized signed build, with Sandbox | owner decision |
| E5 | Resources for the VM, a time window, and the shared lock taken | VM needs 6 GB RAM + ~45 GB disk; Dell had 6–11 GB RAM / ~35 GB C: free on 2026-10-20 → **disk is short** |
| E6 | Upgrade baselines on hand, never re-downloaded: published 1.3.0 `aedecb6` (`c6039f5f…`) and signed 1.3.1 `7c66928` | present in the release folders |

## 2. Environment

- **Guest:** Windows 11, Gen 2, vTPM, 4 vCPU, 6 GB, SAC off, Defender on; one local administrator and one standard user.
- **Network:** a Private Hyper-V switch only. Production hostnames are pinned to 127.0.0.1 in the guest. The production
  cloud, AWS, Stripe and Bluehost are never reachable.
- **Cloud:** `sandbox/test-cloud.py` (real claim and appliance routes, HTTPS) for Hybrid. It signs snapshots and analytics
  documents with the synthetic key only.
- **Cameras (simulated):** in the guest, MediaMTX 1.21.0 plus FFmpeg `testsrc2`/`sine` publish two authenticated RTSP
  streams. A person/vehicle public-domain clip feeds the YOLO cases.
- **Gaps that must be closed before the discovery cases:**
  - **G1:** there is no ONVIF WS-Discovery responder in the repo. Write and review a small in-guest responder (Probe →
    ProbeMatches with an EndpointReference UUID and scopes, plus GetProfiles/GetStreamUri), or the owner approves one
    real camera.
  - **G2:** the off-subnet manual-add case (CAM-5) needs a second private switch and a guest-side route, or a second
    guest acting as a router.
- **Checkpoints:** `00-clean`, `01-with-1.3.0`, `02-with-1.3.1`. Every run starts from one of them.
- **Clock:** offline-licensing cases move the guest clock (`Set-Date`, guest only, with time sync disabled in the
  guest). The host clock is never changed.

## 3. Test cases

Result columns are filled in at run time: PASS / FAIL / BLOCKED, plus an evidence path.

### 3.1 Installation (build a: production keyset), from `00-clean`

| ID | Case | Expected | Automation |
|---|---|---|---|
| INS-1 | Silent install `/VERYSILENT` | exit 0; VC++ runtime installed and registered; runtime preflight passed (torch, cv2, ultralytics load) | VS |
| INS-2 | Interactive install, Local and Hybrid choices | the mode page appears; the choice is recorded in vms.env; no claim code is shown | manual |
| INS-3 | Services | AnyAiCamVMS and AnyAiCamAgent are installed, automatic and running; /health within 3 min; /version = 1.3.2 + commit | VS |
| INS-4 | Data location and ACLs | everything is under `%ProgramData%\AnyAiCam` (SYSTEM/Administrators only); **no `C:\app`, `C:\opt` or `C:\var`** is created | VS + manual check of `C:\opt`/`C:\var` |
| INS-5 | Trust anchor | keyset in `update\` with digest `2389a655…`; setup refuses a wrong digest (build-time) | VS / installer tests |
| INS-6 | Firewall | exactly two inbound rules (web 8000 TCP, WebRTC UDP), program-scoped; nothing for discovery | VS |
| INS-7 | Runtime catalog | registered (relevant only to a signed build) | VS |
| INS-8 | Repair reinstall, including a damaged vms.env | exit 0; secrets byte-identical (`ANYAICAM_APP_SECRETS`, camera key); permissions restored; still linked | VS |
| INS-9 | Downgrade guard | installing 1.3.1 over 1.3.2 is refused and nothing changes | AC (d0d59019) |
| INS-10 | Uninstall | services and catalog removed; data and the cloud link kept | VS |
| INS-11 | Reinstall after uninstall | data, cameras and link come back | manual |

### 3.2 Upgrades, from `01-with-1.3.0` and `02-with-1.3.1`

| ID | Case | Expected | Automation |
|---|---|---|---|
| UPG-1 | 1.3.0 → 1.3.2 | recordings, accounts, settings and cameras kept; 1.3.0 identity kept; the permanent claim code is retired and links nothing | VS (upgrade mode) |
| UPG-2 | 1.3.1 → 1.3.2 | same as UPG-1; secrets byte-identical | VS (upgrade mode) |
| UPG-3 | Premium analytics after an upgrade (deferred edge seed, ae23d024) | an analytic that was ON and is still entitled is ON after the first valid signed grant; ON-but-not-entitled → OFF; OFF stays OFF | manual with synthetic build b + test cloud |
| UPG-4 | Capacity after the upgrade | Hybrid-linked: signed capacity accepted from the cloud; never-linked Local: 0 cameras (documented behaviour; owner wording) | AC + manual |
| UPG-5 | Recovery path (no in-place rollback on Windows) | uninstall 1.3.2, then install 1.3.1: data readable, or the documented limitation is recorded | manual |

### 3.3 Local / Hybrid modes

| ID | Case | Expected | Automation |
|---|---|---|---|
| MOD-1 | Local, never linked | VMS runs; **0 cameras** licensed; the UI explains why | AC |
| MOD-2 | Hybrid link | an admin link request gives a one-time link; owner confirms; agent saves the credential; the VMS reads it from `%ProgramData%\AnyAiCam\agent` (STATE_DIR fix 2161758c); heartbeat shows platform `windows` | VS |
| MOD-3 | Hybrid features use the credential | cloud backup upload, event media, live relay and P2P run against the test cloud (none fall back to `C:\var\lib`) | manual with test cloud |
| MOD-4 | Switch Local → Hybrid → Local | the product-mode page and installer switch keep data; licences follow the signed snapshot | AC + manual |
| MOD-5 | Remote commands | restart_vms and restart_service work; reboot and install_update are refused | VS |

### 3.4 Camera discovery and management (needs G1/G2)

| ID | Case | Expected | Automation |
|---|---|---|---|
| CAM-1 | WS-Discovery multicast through Defender | the simulated ONVIF camera is found, with the endpoint UUID as device_key | manual (G1) |
| CAM-2 | RTSP scan without ONVIF | found on port 554/8554; device_key `mac-` (on-link) | manual |
| CAM-3 | Add by IP, same subnet (CAM-5) | probe of that address only; same record as a scan; `manual_target` | manual |
| CAM-4 | Add by IP, other subnet (CAM-5, G2) | `ip-` device_key; found again at its stored address at verification | manual |
| CAM-5 | Unicast probe reply from a port other than 3702 | either accepted, or ONVIF is lost but RTSP still adds the camera (record which) | manual |
| CAM-6 | Password entry | masked field, never a `prompt()` (CAM-6) | manual (browser) |
| CAM-7 | Remove, re-add, replace (CAM-R1/R3) | slot and analytics seats freed; a removed camera cannot be revived by a client `PUT` | manual |
| CAM-8 | Capacity bar | N licensed → the (N+1)th camera is refused | AC |

### 3.5 Live View

| ID | Case | Expected | Automation |
|---|---|---|---|
| LIV-1 | Local HLS | stream plays; segments advance; < 10 s latency | manual |
| LIV-2 | Local WebRTC through the firewall rule | plays from a second guest / host browser on the private switch | manual |
| LIV-3 | Hybrid remote live (relay / P2P via test cloud) | plays; transport reported | manual |
| LIV-4 | Camera offline, then back | the error state is shown; it recovers without a service restart | manual |

### 3.6 Recording

| ID | Case | Expected | Automation |
|---|---|---|---|
| REC-1 | Basic (pixel) motion recording ON by default, without any licence | clips appear under `%ProgramData%\AnyAiCam\recordings` | manual |
| REC-2 | Continuous mode | segments are contiguous; durations correct (ffprobe) | manual |
| REC-3 | Across a VMS restart and a guest reboot | recording resumes; no corrupt final segment | manual |
| REC-4 | Low disk | retention frees space; no crash | manual |

### 3.7 Playback

| ID | Case | Expected | Automation |
|---|---|---|---|
| PLY-1 | Timeline playback, seek, export clip | plays; the export opens | manual |
| PLY-2 | Recordings of a removed camera (CAM-R2) | still playable | manual |
| PLY-3 | Hybrid: a cloud-backed clip plays from the portal (test cloud) | plays | manual |

### 3.8 Reconnect

| ID | Case | Expected | Automation |
|---|---|---|---|
| RCN-1 | The camera's IP changes (move the simulated stream to a new address) | Reconnect with the new IP finds it; `ffprobe` resolves from `runtime\tools\ffmpeg` (PATH) | manual; ffprobe-bundle unit test PASS |
| RCN-2 | New credentials | Reconnect with new credentials succeeds; the old ones are discarded | manual |
| RCN-3 | A different device now at the address | refused (device_key mismatch) | manual |

### 3.9 Offline licensing and premium analytics (build b: synthetic keyset; guest clock moved)

| ID | Case | Expected | Automation |
|---|---|---|---|
| LIC-1 | Fresh install | every premium analytic OFF; basic motion, recording, live and playback ON | AC + manual |
| LIC-2 | Entitled but not switched ON | stays OFF; entitled + customer ON → runs | manual |
| LIC-3 | Cloud unreachable, 24 h 1 m | Talk Down / Voice Call stop (24 h snapshot); premium analytics continue | unit PASS (host); manual in VM |
| LIC-4 | Offline 167 h 59 m vs 168 h | still ON vs OFF | unit PASS; manual |
| LIC-5 | Guest clock set back after the high-water | premium OFF until the next valid sync | unit PASS; manual |
| LIC-6 | Standard user edits or deletes `config\analytics_entitlements.json` / `feature_entitlements.json` | access denied (ACL) | manual |
| LIC-7 | Administrator replaces the analytics state with an older copy | refused (MAC / older-document clear); camera keeps recording | unit PASS; manual |
| LIC-8 | Reconnect after a revocation | analytic OFF at the first sync | unit PASS; manual |
| LIC-9 | Capacity snapshot N / N+1, expiry, tamper, other identity | as Linux: refused / limited | AC |
| LIC-10 | Analytics not available on Windows (LPR without Tesseract, PPE without a model, facial opt-in) | "Not available on this computer"; enable/assign → 409 | manual + unit PASS |
| LIC-11 | **Voice Call listen (STT)**: open item W-1 | today no Vosk model is installed on Windows, so speech-to-text is unavailable. Expected after W-1 is decided: either works, or is reported unavailable | blocked on W-1 |

### 3.10 Resilience, security, Software Update

| ID | Case | Expected | Automation |
|---|---|---|---|
| RES-1 | VMS crash (kill python) | WinSW restarts it; healthy in 3 min | VS |
| RES-2 | Guest power-off during recording | boots back, records, DB intact | manual |
| SEC-1 | Standard user plants `C:\opt\anyaicam-ppe-model\yolov8n-ppe.pt` | never loaded (path under ProgramData); no code runs as SYSTEM | manual (e1e79e5b) |
| SEC-2 | Agent opens no listening port | none | VS |
| SWU-1 | A newer Linux release exists in the test cloud | Settings → System shows "run the new AnyAiCam setup"; an install request → 409 | manual (931b164c, cloud code with test cloud) |

## 4. Exit criteria

- Every case is PASS, or FAIL with an owner-accepted disposition. BLOCKED cases (G1, G2, W-1) are listed explicitly.
- Evidence (logs, `report.txt`, screenshots, hashes) sits in one results folder, with a SHA256SUMS file, handed to Codex.
- The synthetic-keyset build is deleted or quarantined after the run. It is never published.
