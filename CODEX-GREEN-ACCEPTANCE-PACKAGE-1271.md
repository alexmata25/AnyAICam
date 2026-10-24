# Green independent acceptance package — Linux 1.2.7.1

Prepared 2026-10-24 from the isolated Codex review clone. This is an offline
review/test handoff, **not** a release approval, installer, deployment, or
physical-test authorization.

## Frozen source identity

- Frozen Linux 1.2.7 baseline: `b8c7a7edba7083ee703bb7a4d71bc8f27ea7dc5c`.
- Corrective application/test source to review: `dcace5778c56988d447bd8a182a8308e7fb21dc5`.
- At package preparation, local review branch: `codex/linux-1271-integrated`; branch tip `306df37006068799d1ec4ff09d3e0cb18c269f80` was documentation-only on top of the application/test source. A documentation commit for this handoff will advance the branch tip without changing application code.
- `git merge-base b8c7a7edba7083ee703bb7a4d71bc8f27ea7dc5c HEAD` is the frozen b8 commit. Tracked source/index were clean at verification. Local untracked `_evidence/`, pytest temp databases, and Red snapshot patch were deliberately preserved; they are not release payload inputs.
- Corrective areas represented in the branch: R-REC-1 event-only recording; G2 accepted authorization continuity through clock rollback/offline cloud; TRUST-1 revocation/rotation/anti-rollback; D-HW-PLAN-1 guest plan persistence; D-AUTHZ-3 tenant isolation; D-AUTHZ-1 revocation/session versioning; D-AUTHZ-2b administrator MFA.
- Core change commits include `5fc25fa8857b38606fdd780c6ff2a788ac570ada` (edge event-only recording), `84fb4707321764a7a2f114c386cc423ec3a3d8cd` (cloud recording policy), `3b198918049b500a205ce4486c36625a10dc1b91` (Red TRUST-1/G2 snapshot), `2ffbc4050f69cf7397db22cbd631a60125c8d42f` (saved guest plan), `625498f897215bf0e3ba5746145342cc1f50d8a4` (tenant isolation), `17f3b2d54a1269dc54c479295007070366060599` and `3977394a5110b235afd9ffd993470a4a7264e2ad` (admin session/MFA), `87e2ca3ba5f305acba6fc3f63caf46072903bed7` (Red authorization snapshot), `8973023c50c893eaa90d87ed1db420a68b4ce34e` (MFA setup grant guard), `0ae25115179778f1ef0233e0da4a7258d2965aa3` (transactional session migration), `0e09ac6d7514a45036630e644e149868b65c682b` / `4dbbc18d7a25e72c642f1da6f2435fc0854213b0` (Ultralytics opt-out), and `dcace5778c56988d447bd8a182a8308e7fb21dc5` (customer fetch-wrapper and browser harness correction). See `git log --oneline` for the isolated test-only corrections and merge commits.

The four isolated corrections are included: recording-test fixture uses an MFA-authorized admin; trust-registry fixture preserves canonical bytes on Windows; MFA setup requires a live admin grant; guest-plan test isolates host identity. The final browser exercise uncovered and fixed a malformed CSRF/fetch wrapper in `dcace577`; test assertions were not weakened.

## Release-input inventory

Inventory was limited to the isolated source tree. `git ls-files` found no approved production trust registry, no approved update public-key PEM, and no MediaMTX executable. Trust/public-key files under test and pytest fixture directories are synthetic fixtures and **must not** be supplied to the release builder.

| Input | Present/approved here? | Required supplier/action |
|---|---|---|
| Canonical TRUST-1 entitlement trust registry JSON and its independently sourced SHA-256 | **Missing**. No tracked release registry; test fixtures are not acceptable. | Release owner / authorized cloud trust administrator provides the public-only canonical registry and a digest obtained independently through the approved release channel. Confirm production environment, no lab/test key IDs, public keys only, a valid active capacity signer, and reviewed active/retired/revoked entries. No private signing key is needed by this build. |
| Software Update verification public key PEM and independently verified fingerprint | **Missing** from tracked release inputs. | Release owner / update-signing custodian supplies only the public half and its independently verified fingerprint. Never copy or request the private signer key. |
| MediaMTX Linux binary, platform/version identity, and SHA-256 | **Missing**; only installer logic is tracked. Builder requires the binary and matching digest unless `--no-mediamtx` is selected, which disables fresh-install P2P and is not acceptable here. | Red/release engineering supplies the approved Linux amd64 artifact from the designated upstream/release source, its version and independently verified checksum. Builder verifies the supplied hash before packaging. |
| Exact VMS application/test source | **Present** at `dcace5778c56988d447bd8a182a8308e7fb21dc5`. | Codex branch snapshot; Red must review integration and Green independently validate before a release build. Use a clean worktree and record both VMS payload commit and installer-source commit in the manifest. |
| Optional non-secret env template | **Not required by builder; no approved template supplied.** | Omit unless release owner supplies a reviewed secret-free template. Never pass `.env`, `aws.env`, production config, database, or customer state. |
| Preloaded VMS Docker image | **Not built**; Docker is on shared HOLD. | Optional builder input. If selected, after Red releases the shared lock and resource preflight passes, build only from the exact approved payload and record platform, payload SHA, archive SHA, image ID/config digest and size. Otherwise installer performs the normal appliance-side image build; no remote `latest` tag or invented digest. |

Builder requirement was verified via `python installer/build_release_installer.py --help`. For the intended full 1.2.7.1 package, do not use `--no-entitlement-signing-keys`, `--no-update-signing-key`, or `--no-mediamtx`. The release registry is passed with `--entitlement-trust-registry` and `--entitlement-trust-registry-sha256`; update public key with `--update-signing-public-key`; MediaMTX with `--mediamtx-binary` and `--mediamtx-sha256`. The release version and exact VMS commit are mandatory. `--preload-vms-image` requires Docker and is a deliberate choice, not a substitute for the checks above.

**Immediate owner/release decisions:** assign the approved public trust-registry/digest source, public update-key/fingerprint source, and MediaMTX artifact/checksum source; confirm whether the release installer should preload the image (coordinate lock/resource window); obtain legal approval for LPR/Ultralytics distribution or an approved open-source compliance path. These are real release gates, not reasons to weaken builder validation.

## Focused evidence and reproducible commands

Evidence is in the sibling `_evidence/` directory. The current `CODEX-SHA256SUMS.txt` has 45 entries and was independently checked with zero missing files or mismatches. Manifest SHA-256: `7c833b0add60a745f326ec416e6bd32337351ac45a70d3072634fa81c6ea565f`.

Results below are offline Windows/Python 3.14.6 runs with SQLite or mocked media, not supported Ubuntu/Python 3.12/Docker acceptance. Red/Green should rerun in a clean Ubuntu 24.04/Python 3.12 environment using the same exact application commit and pinned dependencies.

From repository root on a disposable Linux test host, use an isolated temp directory and database, then run the following focused groups. Do not point `ANYAICAM_PARTNER_DB` at a production/customer database.

```bash
set -eu
ROOT="$PWD"
TESTROOT="$(mktemp -d /tmp/aac-1271-review.XXXXXX)"
cd "$ROOT/app"
export ANYAICAM_PARTNER_DB="$TESTROOT/partner.sqlite"
python3 -m pytest -q --basetemp "$TESTROOT/pytest-product" \
  tests/test_r_rec_1_event_only_cloud.py \
  tests/test_r_rec_1_event_only_edge.py \
  tests/test_entitlement_trust_1271.py \
  tests/test_entitlement_keyset_tool.py \
  tests/test_hardware_checkout_guest_plan_1271.py \
  tests/test_lpr.py tests/test_lpr_stale_buffer.py \
  tests/test_lpr_retire_camera_state.py \
  tests/test_lpr_independent_cadence.py tests/test_ppe.py \
  tests/test_ultralytics_privacy.py
python3 -m pytest -q --basetemp "$TESTROOT/pytest-authz" \
  tests/test_admin_mfa_enforcement_cloud.py \
  tests/test_admin_mfa_enrollment_recovery.py \
  tests/test_cloud_legacy_bootstrap.py tests/test_auth_audit_sanitized.py \
  tests/test_partner_session_version.py \
  tests/test_subscription_portal_tenant_isolation.py \
  tests/test_runtime_role_lock.py
python3 -m pytest -q --basetemp "$TESTROOT/pytest-p2p" \
  tests/test_live_view_p2p_authz.py tests/test_live_view_p2p_signaling.py \
  tests/test_live_p2p_session_lease.py tests/test_webrtc_publisher.py
node "$ROOT/tests/test_csrf_browser_wrapper.js"
```

The accepted local results to compare against are:

- Product/security group: **254 passed, 0 failed, 2 skipped**; `_evidence/full-focused-final-current.log`, SHA-256 `d16567e189b968e6e2a18d622b13c355a2190aeba7a140c7a51fee10e606e242`.
- MFA/admin/tenant group on the current code source: **139 passed, 0 failed, 4 skipped**; `_evidence/authz-exact-app-tip-authorized.log`, SHA-256 `c274495dbf5839cff8ecafab5179a97e4e2a0ded3c4faf35579fa9e2ac040f15`.
- P2P/local authorization subset: **55 passed, 0 failed, 7 skipped**; signaling/relay workflow **24 passed, 0 failed**; browser matrix **36 passed** across Chromium, Edge and WebKit. Browser media is mocked; this does not prove physical-camera or routed-network delivery.
- Installer trust registry tests: **8 passed, 3 skipped, 2 subtests passed**. Appliance-agent trust/update tests: **51 passed, 11 skipped, 39 subtests passed**. Run from their respective `installer` and `appliance-agent` directories with isolated temp storage. The skipped root-owned Linux checks remain pending and must not be counted as passes.
- Node wrapper test: `node tests/test_csrf_browser_wrapper.js` => `actual-shell-fetch-wrapper=passed`.

One restricted-session test attempt failed during collection because the command ran from repository root and app modules were not on `sys.path`; a second hit the sandbox pytest temp path, and a third stalled on loopback. These were not product failures. The final auth run passed from `app/` with `ANYAICAM_PARTNER_DB` and `--basetemp` redirected into the worktree using an authorized local loopback test session. Logs are preserved in the manifest.

## Required acceptance after release materials and environment are supplied

No Docker, Ubuntu/Python 3.12, root-owned test, installer build/install, PostgreSQL 16, rollback, physical camera, or live network acceptance ran in this Codex session. Do not call these passed.

1. **Preflight/identity:** record host/VM identity, Ubuntu 24.04 point release, Python 3.12, Docker Engine/Compose versions, free RAM/disk, image platform, active shared lock owner and clean exact commit. Respect existing resource gates (at least 4 GiB RAM and 8 GiB free disk to start; stop below 3 GiB RAM or 6 GiB disk) and do not run competing Docker work.
2. **Material validation:** independently verify all supplied public hashes and formats, reject test/lab keys and private key material, verify MediaMTX binary architecture/version/checksum. Save reviewed input hashes in test evidence.
3. **Build:** from a clean worktree, build against one exact commit, use the approved public trust registry, update public key and MediaMTX binary. If preloading, build image only after lock release; record OCI/image identity and archive SHA/size. Verify artifact manifest, script LF/modes, absence of secrets, payload provenance and final installer SHA/size. Do not overwrite earlier installer artifacts.
4. **Fresh install / repair / upgrade / rollback:** use a disposable Ubuntu 24.04 VM, snapshot/export before changes, synthetic identity/config/data only. Test Local and Hybrid fresh setup, reboot/service behavior, same-release repair, supported prior-version upgrades, failed/interrupted image or dependency acquisition, validation fail-closed paths, rollback, and byte-for-byte preservation of identity, configuration and generated test recordings. Verify `/version` matches exact commit.
5. **PostgreSQL:** use two disposable PostgreSQL 16 databases (never customer/live DBs); run migrations, backup/restore and repeated-start zero-new-migration checks; compare synthetic customer, subscription, entitlement, payment-history and session rows before/after rollback.
6. **R-REC-1:** provision with every recording mode missing, malformed, legacy continuous, or explicit event. Assert the database, API, UI, config, cloud payload and recorder all resolve to motion/event-only. Switch an actively recording test camera to event-only and verify immediate cessation of continuous segments while event clips remain. Restart, upgrade, repair, and transition Local↔Hybrid; confirm no continuous upload path or customer continuous option appears.
7. **G2/TRUST-1:** only after the verified trust registry is supplied, test an already accepted camera grant across device-clock rollback and cloud outage while local event recording continues. Separately reject new invalid/expired/not-yet-valid/replayed grants. Test current/revoked/retired/rotated signers, offline cache behavior, monotonic trust-state, stale registry/state restore, downgrade and interrupted update; demonstrate safe recovery without accepting revoked keys or stopping legitimate recording unnecessarily.
8. **Basic Local remote viewing / P2P physical run:** on an isolated lab LAN with one authorized test camera and synthetic account, verify login, actual changing camera frames and playback, not merely page load. Record browser console/network errors, negotiated ICE candidate pair/path, WHEP/MediaMTX session, packet/frame timestamps and corresponding event recording. Then test an isolated routed/two-network path only if the approved VPN/relay test setup exists; confirm customer isolation and relay auth. Verify loss/reconnect and offline Local behavior. Do not expose camera ports publicly or use Ryzen/Samsung until owner separately authorizes the window after backup/thermal gate.
9. **Restore proof:** before any physical test, create and checksum a full backup of DB, recordings, configuration, appliance identity, accepted authorization/trust state, and service definitions. Restore into a disposable copy; verify hashes/counts and login/recording. Preserve an independently tested rollback image and exact prior installer. Physical restore itself requires explicit owner authorization.
10. **Final evidence:** Green independently repeats agreed acceptance cases on the exact candidate; Red reviews integration; keep test logs, environment versions, skips and hashes, with no sensitive credentials or footage. Any source change requires rerun of affected tests and exact installer rebuild; no evidence transfers automatically across commits.

## Licensing and runtime verification — distribution gate

- Repository pins `ultralytics==8.3.40` in `requirements.txt`; the checked Windows/local telemetry smoke used Ultralytics `8.4.115`, so it does **not** prove the pinned release runtime. Runtime opt-out code is `app/ultralytics_privacy.py`, imported by `app/main.py`, `app/ppe.py`, and `app/lpr.py`; test the exact image/package that will ship.
- Dockerfiles fetch `morsetechlab/yolov11-license-plate-detection` at a pinned repository commit and verify model SHA-256 `0aec75976c56eb6f26dfb274c430620ec65137915ff1ae47c3a48c7af8afb7b2`. The Dockerfile identifies the source model as AGPL-3.0. The model is not tracked in this clone; no model was loaded in this session.
- Upstream Ultralytics describes AGPL-3.0 and Enterprise licensing and specifically presents Enterprise as the route for commercial/proprietary embedding without AGPL source obligations; its YOLOv8 documentation says YOLOv8 models are available under AGPL-3.0 or Enterprise. The model author's repository README labels its plate detector AGPL-3.0. These are the licensors' statements, not a legal opinion: [Ultralytics licensing](https://www.ultralytics.com/license), [Ultralytics YOLOv8 licensing](https://docs.ultralytics.com/models/yolov8), [plate-model source repository](https://github.com/morsetechlab/yolov11-license-plate-detection).
- Before commercial customer distribution, owner/legal counsel must either document full AGPL compliance for the actual combined product and corresponding source/model obligations, or obtain written commercial rights that cover the runtime, YOLOv8 model, fine-tuned YOLOv11 plate weights, SaaS/appliance/customer distribution and intended VMS use. Independently confirm model-training dataset and weight redistribution rights; the upstream AGPL label alone does not establish those provenance facts.
- In the exact Linux image, verify `ultralytics.__version__ == 8.3.40`, pinned CPU `torch==2.5.1+cpu`/`torchvision==0.20.1+cpu`, LPR weight digest above, and that `YOLO_CONFIG_DIR` resolves to the intended protected config path. Before importing Ultralytics, confirm `sync=False` and events disabled; after import confirm persisted settings remain disabled. Run a permitted local inference smoke with a redistributable fixture and prove no analytics or telemetry endpoint is contacted (network deny/monitor), no customer video leaves the host, and AI imports fail closed if privacy settings cannot be set. Measure startup, per-camera CPU/RAM and cleanup. This verification has not yet run against the pinned package/image.

## Current recommendation

**Source/test preparation: ready for Red/Green independent review. Linux 1.2.7.1 release candidate: NO-GO until required approved public materials, Docker/Ubuntu lifecycle evidence, root-owned tests, pinned runtime/telemetry verification, independent review, and LPR licensing decision are complete.** Physical video acceptance and all appliance access remain separately gated by the owner. No production or physical system was changed by this handoff.
