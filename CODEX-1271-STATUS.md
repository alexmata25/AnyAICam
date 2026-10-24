# AnyAiCam Linux 1.2.7.1 isolated corrective status

Date: 2026-10-24

## Source identity and preservation

- Frozen baseline: `b8c7a7edba7083ee703bb7a4d71bc8f27ea7dc5c`.
- Isolated Codex branch: `codex/linux-1271-integrated`.
- Latest source commit: `dcace5778c56988d447bd8a182a8308e7fb21dc5` (`fix: repair customer fetch wrapper and P2P browser fixtures`). The report-only commit that follows does not change application or test code.
- `git merge-base b8c7a7ed HEAD` resolves to the frozen baseline. The frozen source and the original Red worktrees were not modified.
- Red's trust/G2 and authorization tracked work was snapshotted into local review commits `3b198918049b500a205ce4486c36625a10dc1b91` and `87e2ca3ba5f305acba6fc3f63caf46072903bed7`. The only untracked package/build copies inspected were byte-identical to tracked source (34/34 files); generated build and egg-info output was excluded. Original worktrees remain untouched.

## Corrective areas assembled

1. R-REC-1 event-only recording policy and route/migration/provisioning checks.
2. G2 accepted authorization continuity during clock rollback/offline conditions.
3. TRUST-1 key lifecycle, revocation and trust-state rollback protections.
4. D-HW-PLAN-1 guest purchase-plan persistence and test isolation.
5. D-AUTHZ-3 customer/tenant subscription and support-ticket isolation.
6. D-AUTHZ-1 revoked administrator/session-version enforcement.
7. D-AUTHZ-2b MFA enforcement on administrator authentication, setup, bootstrap and protected routes.

Additional isolated fixes in this branch:

- R-REC tests now obtain an MFA-authorized admin session rather than relying on a legacy bare-token bypass (`557a4240`).
- The AUTHZ session-version migration is in the migration transaction (`0ae25115`).
- The MFA setup page refuses unauthorized active-grant requests (`8973023c`); the guest plan test isolates host identity (`62b3527b`).
- A Local-view regression fixture now uses a persisted MFA-verified administrator session, so the role-routing assertion tests a valid wrong-role user under D-AUTHZ-2b (`d44db2c5`). This did not relax authentication.
- Same-origin CSRF wrapper strips quoted cookie values (`e2501657`).
- Ultralytics opt-out is written to the actual `Ultralytics/settings.json` config location before import, and inference remains fail-closed if telemetry cannot be disabled (`4dbbc18d`).
- Final browser validation found the wrapper's generated HTML had an unclosed function, and the previous Node test duplicated rather than executed the template. Commit `dcace577` fixes the wrapper to always delegate GET/cross-origin requests, normalize URL/Request origins, preserve Request headers, and add CSRF only to unsafe same-origin requests. The test now parses and executes the extracted f-string template.
- The same commit corrects the Playwright fixture to model a valid `MediaStreamTrack` and HLS `playing` event; fallback assertions wait for relay success rather than only player creation. These corrections preserve the product acceptance assertions.

## Focused automated evidence

All results below are local offline tests on Python 3.14.6 with isolated SQLite databases unless explicitly stated otherwise. They are not Linux/Ubuntu or installer acceptance.

- Final product/security suite on application/test source `dcace5778c56988d447bd8a182a8308e7fb21dc5` (run from report descendant `a890fc0bf50f9bae33907da316710e0843c9c254`): **254 passed, 0 failed, 2 skipped, 33 warnings, 179.33 s**. Includes R-REC cloud/edge, trust/G2, keyset tooling, guest checkout plan, LPR stale-frame/camera cleanup/cadence, PPE and telemetry privacy tests. Log: `_evidence/full-focused-final-current.log`.
- Administrator authorization suite on integrated auth code: **139 passed, 0 failed, 4 skipped**, 665 warnings, 386.26 s. Log: `_evidence/authz-full-integrated.log`.
- Final authorization/MFA rerun on `d44db2c5`: **139 passed, 0 failed, 4 skipped**, 665 warnings, 290.49 s. Log: `_evidence/authz-final2.log`. An earlier exact-tip attempt was interrupted and is excluded.
- Final authorization/MFA/tenant-isolation suite on application/test source `dcace5778c56988d447bd8a182a8308e7fb21dc5` (final report commit `81f0231422c942c58c38925c92538faecc625c14`): **139 passed, 0 failed, 4 skipped, 665 warnings, 238.48 s**. It used an isolated SQLite file and worktree-local pytest temp directory. Log: `_evidence/authz-exact-app-tip-authorized.log`. Restricted-session attempts that failed during collection/setup or stalled on loopback are retained but excluded from the result.
- Installer trust-registry tests: **8 passed, 3 skipped, 2 subtests passed**. Log: `_evidence/installer-trust.log`.
- Appliance-agent trust/update tests: **51 passed, 11 skipped, 39 subtests passed**. Log: `_evidence/agent-trust.log`.
- Node/Python-wrapper browser tests: **41 passed, 0 failed**. Log: `_evidence/node-focused-pytest2.log`; direct same-origin CSRF wrapper check reports `same-origin-login-variants=passed` on `e2501657`.
- Actual shell CSRF wrapper: `node tests/test_csrf_browser_wrapper.js` passed, and `py_compile app/main.py` passed. Log: `_evidence/csrf-wrapper-final.log`.
- Local-view page authorization: **7 passed, 0 failed** after correcting an old test fixture to use a persisted MFA-verified administrator session. The initial unverified cookie was correctly refused by the new auth middleware. Log: `_evidence/local-view-auth-fixed.log`.
- Local/P2P route authorization, WireGuard policy, local customer access/playback, and cloud-relay playlist paths: **55 passed, 0 failed, 7 skipped**. Log: `_evidence/local-view-p2p-final.log`.
- P2P signaling and relay-session workflow: **24 passed, 0 failed**. Log: `_evidence/local-view-signaling-final.log`.
- Cross-browser P2P UI transition matrix on `dcace577`: **36 passed, 0 failed** across Chromium, Edge, and WebKit, 6 warnings, 184.09 s. Log: `_evidence/p2p-browser-final.log`. This is a mocked-media browser test, not a physical camera or real-network acceptance run.
- Ultralytics privacy tests: **4 passed**; local installed Ultralytics 8.4.115 opt-out smoke reported `sync=False`, `events.enabled=False`; runtime preflight passed. No model was loaded and no external telemetry request was made. Logs: `_evidence/ultralytics-privacy-final.log`, `_evidence/runtime-preflight-final.log`.
- Python syntax compilation passed for `app/ultralytics_privacy.py`, `app/ppe.py`, `app/lpr.py`, `app/main.py`, and `installer/windows/runtime-preflight.py`.
- The Local-view and authorization test summaries were captured as focused evidence notes under `_evidence/` and exercised the source content committed as `d44db2c5`.
- `_evidence/CODEX-SHA256SUMS.txt` contains 45 evidence-file entries. All paths and SHA-256 values are independently verified; the final authorized auth suite and the two restricted-session retry logs are included. Manifest SHA-256: `7c833b0add60a745f326ec416e6bd32337351ac45a70d3072634fa81c6ea565f`. Final focused product log SHA-256: `d16567e189b968e6e2a18d622b13c355a2190aeba7a140c7a51fee10e606e242`. Final auth log SHA-256: `c274495dbf5839cff8ecafab5179a97e4e2a0ded3c4faf35579fa9e2ac040f15`. SQLite databases and temporary fixtures are preserved locally but excluded from the hash list and Git commits.

## Environment and unverified acceptance

- Python 3.12 is absent (`C:\Program Files\AnyAiCam\runtime\python\python.exe` does not exist); the `py` launcher cannot resolve it. Local route tests ran on Python 3.14.6. Two earlier combined attempts in the restricted session stalled at TestClient socket operations; the focused suites were rerun with approved loopback access and completed. Interrupted attempts are not counted.
- The release builder was inspected with `installer/build_release_installer.py --help`. A proper 1.2.7.1 build still needs the approved trust-registry JSON and independent SHA-256; verified MediaMTX Linux binary and hash; update verification public key; exact release commit; release version; and the shared Docker lock for `--preload-vms-image`. The `--no-mediamtx`, `--no-update-signing-key`, or `--no-entitlement-signing-keys` options would omit required product behavior and were not used.
- No Docker, WSL, Ubuntu VM, root-owned Linux check, installer build, image build, installer install/upgrade/repair/rollback, camera, or physical acceptance test was run. Shared Docker work is on HOLD and engine/VM access is unavailable from this session; do not bypass the shared lock or start a VM.
- No release installer, image digest, or artifact hash was produced.
- Local/P2P implementation exists: `app/live_view_p2p.py` brokers authenticated offer/answer/ICE signaling, while `app/webrtc_publisher.py` bridges the appliance's camera RTSP stream to local MediaMTX WHEP/WebRTC. Focused mock-server and browser transition suites pass. The publisher module documents historical real MediaMTX v1.21/camera/Ryzen verification, but that historical transcript was not independently rerun for this 1.2.7.1 branch; current physical/network video acceptance remains pending. Do not state that the publisher is absent or that new product work is necessarily required.
- Telemetry behavior was smoke-tested against installed Ultralytics 8.4.115, not the release-pinned 8.3.40 package. Run pinned-package verification in an approved isolated Linux build/test environment.
- Do not use test trust keys for release builds. Builder needs the approved public entitlement keyset and hash, verified MediaMTX binary/hash, and authorized update-verification public key material. No private signing key was accessed.
- AGPL/commercial licensing for the LPR model/runtime must be resolved by the owner/legal counsel before customer distribution; this branch does not assert legal clearance.
- Red/Green independent validation is still required on this exact candidate before integration or release acceptance.

## Recommendation

The corrective source work has substantial focused offline coverage, but Linux 1.2.7.1 is **NOT READY for physical installation or customer distribution**. Remaining gates: supported Ubuntu/Python 3.12 Docker and installer lifecycle validation; approved trusted release materials and immutable image digest; pinned Ultralytics opt-out check; full backup/restore and appliance acceptance after separate owner approval; Red/Green independent review; and LPR licensing clearance. The local route/signaling tests do not demonstrate actual camera media delivery over a physical P2P link.

## Release-preparation checkpoint — 2026-10-24

- Exact application/test source remains `dcace5778c56988d447bd8a182a8308e7fb21dc5`; the current local review branch includes report-only descendants. The tracked source/index are clean; untracked local evidence and temporary SQLite/test artifacts remain preserved and excluded from commits.
- The Green reviewer handoff is committed at `7d5d212672c786f34d46e8fdef9f96831b1aa021`: `CODEX-GREEN-ACCEPTANCE-PACKAGE-1271.md`. It records exact test commands, hashes, release-input inventory, lifecycle and physical-camera procedures, license/runtime gates, and unresolved acceptance work.
- No approved production trust registry/digest, update public key/fingerprint, or MediaMTX binary/hash is present in the tracked source. Release build must wait for the authorized suppliers and an explicit Docker lock window if preloading is chosen.
- Ultralytics `8.3.40` is pinned, but the telemetry runtime smoke used local `8.4.115`; pinned-image behavior remains unverified. The LPR weights are fetched from a pinned model repository revision and hash, but model/runtime commercial distribution rights remain a legal/owner gate.
- No installer, Docker image, PostgreSQL 16 test, Linux lifecycle test, root-owned Linux check, physical camera run, or appliance installation was performed. Frozen b8, Red worktrees, production, Ryzen, and Samsung remain unchanged.

## Release inputs / preservation plan — 2026-10-24

- Detailed offline input and backup plan: `CODEX-LINUX-1271-RELEASE-PREPARATION.md` (current report-only descendant of this status). It records the documented TRUST-1 registry provenance, update-verification public-key provenance, both Docker image delivery paths, and an untracked-evidence exclusion plus history-aware secret-scan gate for any future GitHub backup.
- TRUST-1 registry JSON and independent digest are missing. Update-verification public PEM and separately checked fingerprint are missing. No cloud credentials or private signing keys are needed by Codex or should be transferred to the build workspace.
- Historical MediaMTX candidate is v1.21.0 Linux amd64, documented as checked from the official upstream release and used for prior P2P validation. The tracked records abbreviate both Linux binary and archive hashes; the exact full Linux binary SHA-256 is not in this checkout. Obtain and independently verify the upstream release checksum/attestation and reconcile it with the approved release record before packaging. v1.21.2 is a later upstream version, not an approved AnyAiCam substitution.
- Docker HOLD remains active, so no image was built or preloaded. The existing installer supports both appliance-side build from the exact staged source payload and optional checksum/identity-verified preloaded image; no GHCR publication is required by either current path.
- `aws.env` remains present in Git history even though it is ignored in the current tree. The proposed review-branch backup/push is blocked pending owner-approved encrypted storage and credential-owner assessment; this work neither read nor exposed the file contents, rewrote history, or pushed refs.
- No new application changes, builds, downloads, Docker calls, production access, or appliance access occurred.
- Owner decisions now recorded: release version `1.2.7.1`; MediaMTX `v1.21.0` Linux amd64 subject to upstream artifact/full-SHA verification; initial image path is appliance-side build, with Docker HOLD still active and no execution authorized.
- Read-only `aws.env` history assessment found five file-containing commit trees (`4a521409`, `1c5d053b`, `d89d681d`, `08441d1a`, `b5ebc430`) with nonempty secret-like application fields. Commits are reachable in this clone from four cached remote-tracking branches and multiple cached tags. Current live remote refs were not fetched. The file is deleted later, but history remains. Treat the credential material as exposed; validity is unknown. No values were printed. No push or bundle until credential-owner assessment and owner-approved encrypted destination.
