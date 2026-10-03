# Post-fix installer validation — 2026-10-03

## Candidate identity and scope

Immutable integration base: d5af9c53cbef1f2c400f56e148c5cb080770dc07.
Prior preparation: 48a02734a04c68df195228ba4b5f75aba1162edd.
Validated installer/VMS fix commit: a1c81e0f492633a80198503e721fc6c277e6a8b3.
Isolated branch: codex/installer-validation-fixes-20261003.

No Hyper-V enablement, Windows restart, networking changes, WSL shutdown, existing
service interruption, VM creation, actual OS installer execution, deployment,
publication, real camera access or excluded-system changes. No portal/account/
database provisioning. Existing claim, authorization, CSRF, origin, possession,
nonce, expiration and encryption behavior is unchanged. Fixture tests use temporary
databases; agent tests stub network calls (printed production/staging/example URL
literals do not represent requests to those environments).

## All three candidate findings closed

1. Disk preflight: documented minimum is binary **100 GiB = 107,374,182,400 bytes**
   available on the root filesystem. Read df byte count, validate numeric output,
   and floor-divide by 1,073,741,824; never round upward. One byte below 100 GiB
   refuses, exact minimum and one byte above pass. 99 GiB + one byte refuses.
   Invalid measurement refuses. The existing 15 GiB repair working-space policy
   and recorded total-capacity baseline remain in place, with equivalent exact
   free-space boundary regression coverage. No disk was filled or mounted.
2. Shell fixture: reset_fixture now redirects ROLLBACK_DIR into FIXTURE_ROOT.
   Explicit fixture-path regression added. Normal checked-in harness ran directly
   from this worktree as WSL nobody: **177 passed, 0 failed**. No temporary copied
   test tree or relaxed ownership checks. Scoped Git LF attributes also make the
   normal Windows checkout's installer/runtime scripts usable directly in WSL;
   normalized runtime/compose bytes have no content diff.
3. Discovery identity: namespace-aware XML parsing reads the endpoint Address and
   XAddrs from the SAME ProbeMatch. MessageID/RelatesTo never supply device identity.
   Validates/canonicalizes endpoint UUID; rejects invalid/missing endpoint, malformed
   XML and identities split across matches. Covers changing prefixes, both standard
   addressing/discovery namespace variants and dedup with changing message UUIDs.
   **9 new parser regressions passed**; simulator's expected failure is now a pass.

## Post-fix test results

| Suite | Result |
| --- | --- |
| WSL product-mode unittest | 14 passed |
| WSL rollback unittest | 10 passed |
| WSL release-builder unittest | 16 passed |
| WSL agent portal/setup claim unittest | 34 passed |
| WSL customer_setup pytest | 22 passed |
| WSL ONVIF endpoint identity pytest | 9 passed |
| WSL checked-in shell harness | 177 passed, 0 failed |
| Dell browser/auth/possession local regression | 19 passed |
| Dell ONVIF endpoint identity pytest | 9 passed (same cases as WSL) |
| WSL simulator contract unittest | 4 passed |
| Dell simulator contract unittest | 4 passed (same cases as WSL) |
| Extracted offline Python simulator contract | 4 passed (same cases) |

WSL Python smoke is **105 passed** (96 original + 9 new discovery cases), plus
4 simulator passes. Dell pytest combined execution is **28 passed** (19 + 9).
No test failures or expected failures remain. Counts across platforms are repeat
executions, not additional distinct tests. Temporary pytest 9.1.1 environment was
removed; no global Python/apt packages were installed or upgraded.

Actual rebuilt-package WSL checks: **710 manifest files verified**, 17 root shell
scripts parsed, Ubuntu 24.04/x86_64/16 vCPU/~15 GiB usable RAM preflight passed.
Reported root available bytes 508,793,200,640 (473 whole GiB after flooring). WSL's
thin VHD space is not physical host free-space or clean-appliance lifecycle proof.
Integer 99/100/101 GiB and four exact byte-boundary cases passed expected results.
Non-root freshness, rebuilt manifest and ephemeral loopback bridge passed.
Browser regression exercises owner sign-in/location/confirmation and manual-code
recovery with a mocked OS install. No real VMS/Docker/agent deployment occurred.

## Final unpublished investor installer

Installer version **1.1.1** (runtime changed, so the old 1.1.0 was not relabeled).
Both source pins: a1c81e0f492633a80198503e721fc6c277e6a8b3, derived from d5af9c53.
Filename: anyaicam-appliance-installer-1.1.1-vms-a1c81e0f4926.tar.gz
Size: **36,467,580 bytes**.
SHA-256: **9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac**.
VMS source-archive SHA-256:
5238ac80144c84d0cd92af469df2113177ec96653edde5b69736d3f9ebdd057b.
Location: ignored dist/installer-track/investor-fixed/, with SHA256SUMS,
candidate.json, install and uninstall/recovery instructions. Original pinned
1.1.0 archive is retained separately. No website upload/publication.

## Complete offline camera-simulator bundle

Scope is the previously prepared MediaMTX/FFmpeg/ONVIF test-camera simulator,
including Python and its complete Ubuntu package dependency closure. It does not
make the VMS appliance's existing Docker/Python/model installation offline.

Fresh Ubuntu noble/main+universe, noble-updates and noble-security metadata was
fetched into caller-owned TEMP apt state/cache/list/source directories. Ubuntu
signatures authenticated with the archive keyring; no system apt lists or dpkg
status changed. Empty-status apt simulation resolved **251 exact packages**.
All 251 .deb archives downloaded and SHA-256 verified. Offline apt --no-download
simulation with only local archives resolved all 251 without missing dependencies.
Three InRelease signatures reverified with gpgv. Metadata and keyring are bundled.

Missing AppArmor digest resolved by the available authenticated baseline package:
libapparmor1 4.0.1really4.0.1-0ubuntu0.24.04.8, SHA-256:
9c9ab793bcf772315fc754abb9b9a9f1ef4f828bd5d1c0a40c6e192624f3e86e.
WSL's installed AppArmor was not changed. FFmpeg itself stays 7:6.1.1-3ubuntu5,
SHA-256 1a23baace5f2688a47e28119aedae6993cd638e6279a7227dfc52d9a337a1c17.
MediaMTX stays v1.21.0; binary hash
9fac297a1b57762980fc1f7d91499211091366b3abecd0a7a0d8146387d901ad,
matching upstream release checksum/asset digest from the earlier preparation.

All 251 packages were extracted under an unprivileged temporary root, without
running maintainer scripts or installing them. The extracted dynamic loader and
libraries ran the bundled FFmpeg encoder successfully: H.264 Constrained Baseline,
1280x720, 15 fps; bundled ffprobe matched. Extracted Python 3.12 ran all four ONVIF
simulator contracts. No RTSP/ONVIF listeners or multicast probes were started.

Bundle filename:
anyaicam-camera-simulator-ubuntu24.04-ffmpeg6.1.1-mediamtx1.21.0-a1c81e0f4926.tar.gz
Size **209,869,407 bytes**; 251 .deb packages, 268 manifested files plus manifests.
SHA-256: **f1cab1a7c7488d2f8c501d1469ebb2ced437835067957651e42ea565f4f08c2b**.
Location: ignored dist/installer-track/offline-release/. offline-bundle.json,
offline-simulator.lock.json and OFFLINE_SIMULATOR.md record exact pins and future
guest-only usage. Dependency closure and extracted-runtime proof are complete;
actual apt/dpkg maintainer-script installation and service/network lifecycle still
require the disposable helper VM.

## Remaining gates before accepting an investor build

The three source/test defects and simulator checksum/dependency preparation gaps
are closed. No additional source-validation blocker was found in this scope.

Actual clean Ubuntu VM proof remains mandatory: real sudo installer and dependency
installation, identity permissions, Docker/VMS/agent startup, genuine enrollment,
RTSP/ONVIF network interoperability, live view, recording, playback, analytics,
reboot persistence, uninstall/reinstall and valid prior-image recovery. The full
checklist and deferred admin commands are in CLEAN_MACHINE_PROCEDURE.md.

Lab setup is still part of that proof: authenticate/download the chosen ISO,
approve full Hyper-V enablement plus a scheduled Windows restart, create private
VMs/switch/storage, and approve/provision the new isolated HTTPS portal, scoped
owner/location/license fixture and dependency access. The VMS installer retains
its existing online dependency requirements, so the helper proxy/cache allowlist
must be validated before the real install. No clean-machine or production-ready
claim, VM/account provisioning or Windows native installer completion is made.
