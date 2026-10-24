# Linux 1.2.7.1 release-input and preservation plan

Date: 2026-10-24
Application/test source: `dcace5778c56988d447bd8a182a8308e7fb21dc5`
Review branch: `codex/linux-1271-integrated`
Frozen baseline: `b8c7a7edba7083ee703bb7a4d71bc8f27ea7dc5c`

This is an offline preparation record. No installer, Docker image, registry,
or signing key was built, acquired, or used. The official MediaMTX artifact
was downloaded and independently verified as documented below. Docker HOLD
remains in force.

## 1. TRUST-1 and update-verification release materials

### TRUST-1 registry

The approved source procedure is in `docs/entitlement-signing-keys.md` and the
builder enforces it in `installer/build_release_installer.py`:

1. An authorized trust administrator on the cloud host runs
   `python -m entitlement_keyset --trust-registry` for the initial generation,
   or uses `--previous <approved-prior-registry>` and the documented add,
   retire, or revoke operations for a later generation.
2. The administrator reviews the production environment, generation, signer
   states and purposes; confirms every signer that has signed capacity still
   held by appliances is represented; and confirms that no lab/test signer or
   private key material is present.
3. The canonical public-only JSON registry is supplied to release engineering.
   Its SHA-256 must be obtained independently on the authorized cloud host, not
   copied from the same file, chat, ticket, or release bundle.
4. Release engineering records both inputs and verifies the builder's
   canonicalization and digest check before packaging.

**Missing here:** the approved production registry JSON and its independently
obtained SHA-256. The checkout contains test fixtures, not release materials.
The release owner/cloud trust administrator must provide them through the
approved access-controlled release-materials channel. No AWS/cloud credential
needs to be given to Codex or placed in the source tree. If the release
administrator cannot use that channel, the owner must identify the authorized
operator and approved transfer location; do not ask for a private signing key.

The registry and `/etc/anyaicam-update/entitlement_trust_state.json` must be
backed up and restored together with accepted capacity, as documented in the
TRUST-1 design. This is an appliance restore requirement, not a reason to put
customer trust state into the installer.

### Software Update verification key

The builder requires `--update-signing-public-key` unless the explicitly
unsafe-for-this-release `--no-update-signing-key` path is selected. The missing
input is the public PEM used by the authorized release verifier, plus its
independently verified fingerprint and provenance. The public key may be
transferred via the approved release-materials channel; verify its fingerprint
against the release-signing authority separately. The private signing key is
not a build input and must remain in the signing custodian's protected system.
Codex does not need private-key access or a cloud credential.

### Values not acceptable as substitutes

Do not use synthetic pytest trust keys, test registries, lab keys, a digest
computed only from a downloaded file, a key copied from an unverified prior
installer, `--no-entitlement-signing-keys`, or `--no-update-signing-key` to
make the build proceed. These options would not satisfy TRUST-1/update
acceptance.

### Sanitized release-input checklist

| Input | Authorized source | Independent validation before build | Current state |
|---|---|---|---|
| TRUST-1 production registry | Authorized cloud trust administrator, using `python -m entitlement_keyset --trust-registry` on the approved cloud host against production public signer records; preserve prior generation history | Review canonical production JSON, generation and signer lifecycle; verify retained signers cover existing accepted grants; compare canonical registry bytes to a separately obtained cloud-host SHA-256; builder must accept both registry and digest | Registry and digest missing. No individual custodian name/contact is present in the reviewed repository or handoff; identity must be confirmed by the owner/Red. No cloud access performed |
| Software Update verification public key | Existing release-signing custodian's trusted offline signing environment; export only the public PEM corresponding to the already approved release-signing identity | Custodian supplies separately confirmed fingerprint and provenance; compare using the custodian's documented fingerprint procedure; builder canonicalizes PEM, rejects private key material, records canonical PEM SHA-256, and `installer/12-update-signing-key.sh` verifies it | Public PEM and fingerprint missing. No individual custodian name/contact is present in the reviewed repository or handoff; identity must be confirmed by the owner/Red. Do not generate replacement key material |
| MediaMTX Linux binary | Official `bluenviron/mediamtx` GitHub v1.21.0 release asset `mediamtx_v1.21.0_linux_amd64.tar.gz` | Obtain official `checksums.sha256`; verify archive SHA; verify GitHub attestation with repository `bluenviron/mediamtx`; extract the Linux amd64 executable; record and independently verify its full SHA-256; reconcile against an approved full-hash release record before passing the binary/hash pair to builder | **Verified 2026-10-24**; evidence at `_evidence/mediamtx-v1.21.0-official-verification/`; release-use approval still requires Red/release engineering to accept the evidence |
| Release identity | Owner-approved version `1.2.7.1` and frozen application/test source commit `dcace5778c56988d447bd8a182a8308e7fb21dc5` | Clean isolated worktree; verify commit ancestry and builder-reported source commit; never overwrite the accepted earlier installer | Identified; no artifact built |

Transfer only these public release inputs via the owner-approved, access-
controlled release-materials location. Store private cloud or signing
credentials nowhere in the build workspace. The release builder's checks are
necessary but do not replace independent source/fingerprint/hash verification.

## 2. MediaMTX pin and provenance

Repository history documents the Linux appliance integration with official
MediaMTX **v1.21.0, Linux `amd64`**. `docs/PROJECT_CHECKPOINT.md` records that a
real `linux_amd64` release binary was historically checked against the
upstream `checksums.sha256`, GitHub release asset digest, and an independently
computed binary SHA; the binary was used for historical Ryzen P2P validation.
The same source documents the upstream release URL family and the production
installer consumes an extracted binary, not the archive. The exact platform
artifact is named `mediamtx_v1.21.0_linux_amd64.tar.gz` from the official
[`bluenviron/mediamtx` v1.21.0 release](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.0).
The project's Windows dependency lock independently pins v1.21.0 for Windows
amd64, but its Windows ZIP hash is not valid for Linux.

**Official artifact independently verified 2026-10-24:** downloaded the
v1.21.0 Linux amd64 archive and upstream `checksums.sha256` from the official
release; the archive SHA-256 is
`e02e34c3337a35f20ac9e5aa31524566108964e6e37dbc46cf8292169f6c792b`
(27,192,682 bytes), matching the upstream checksum file. `gh attestation
verify` passed with repository `bluenviron/mediamtx`, signer workflow
`bluenviron/mediamtx/.github/workflows/release.yml`, and source ref
`refs/tags/v1.21.0`; the verified attestation subject includes the exact
archive hash. The archive listing was checked before extraction and contained
only `mediamtx`, `mediamtx.yml`, and `LICENSE`. The extracted executable is
ELF64 little-endian x86_64, 55,239,741 bytes, with SHA-256
`9fac297a1b57762980fc1f7d91499211091366b3abecd0a7a0d8146387d901ad`,
matching the full hash behind the previously documented abbreviated binary
pin. The executable was not run on Windows. Full verification output,
attestation JSON and a SHA256SUMS manifest are preserved under
`_evidence/mediamtx-v1.21.0-official-verification/`.

This closes the MediaMTX provenance/hash gap for the builder input; it does not
authorize a build, installer test, Docker use, or release. Upstream's official
release page documents its checksum and attestation verification procedures:
[`bluenviron/mediamtx` v1.21.0 release](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.0).

Upstream now lists v1.21.2 as a later release. That does not make it an
approved AnyAiCam pin. Keep v1.21.0 as the historical compatibility candidate
unless Red/release owner explicitly approves a version change and completes a
compatibility review. Upstream recommends verifying release checksums and
attestations; see its [release security instructions](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.0).

## 3. Docker image delivery choices (no Docker run performed)

There are two existing installer paths. The image is built from the exact
release payload in either case; neither path uses an unverified `latest` tag
or requires GHCR publication.

### A. Online/small installer; build on the appliance

- Build the installer without `--preload-vms-image`.
- During install, `installer/06-deploy-vms.sh` builds from the staged, pinned
  VMS payload using the appliance's Docker Engine/Compose.
- The appliance needs compatible Docker/Compose, free disk for build layers,
  and network access to the pinned base/package/model sources used by the
  Dockerfile. The installer must surface failed downloads/builds and preserve
  its existing retry/rollback behavior; do not claim this mode is fully
  offline.
- Advantages: smallest installer, no GHCR account/credential or image upload.
  Trade-offs: first-install CPU, disk and elapsed time; reproducibility depends
  on every build input being pinned and available.
- Acceptance must cover clean install, retry/interrupted download, checksum or
  dependency failure, upgrade, repair, rollback, Local/Hybrid and image rebuild
  identity on Ubuntu 24.04.

### B. Larger offline installer; preload the exact image

- After Docker HOLD is explicitly released and resource preflight passes, run
  the existing `installer/build_release_installer.py --preload-vms-image`
  path for the exact approved source payload and target `linux/amd64` (or an
  explicitly approved alternate platform).
- Builder records payload SHA, image ID/config digest, platform, archive
  checksum/size and release labels. Installer verifies those values and the
  archive SHA before `docker load`, then checks loaded image identity and
  release labels before use. It does not rely on a mutable registry tag.
- This is self-contained for the VMS image but still needs Docker Engine on
  the customer's host. It is the offline-image option; it does not bundle the
  Docker Engine or OS dependencies.
- Acceptance must exercise corrupt/truncated archive, wrong platform, wrong
  commit/payload label, load failure, adequate/insufficient disk, upgrade,
  repair, rollback and Local/Hybrid transitions.

The source documents a prior 1.2.5 example of roughly 827 MiB compressed and
3.4 GB extracted; these are historical measurements, **not** measurements of
the 1.2.7.1 image. Do not publish current package-size or storage claims until
the authorized build measures the exact candidate.

### Shared Docker HOLD owner and release condition

The available handoff identifies **Red** as the agent expected to release the
shared lock. The latest visible lock-order note (`BLUE-TO-GREEN-LOCK-ORDER-20261023.md`)
says the Blue sequence released the lock at 22:46 CDT on October 23 and Green
could begin. A read-only check on October 24 found no marker at the historically
documented `C:\Users\Alejandro Mata\anyaicam-red\.docker-test.lock` path.
Neither item proves the current job queue is idle or identifies the individual
currently responsible for the HOLD. The current holder/custodian and status
remain **NOT VERIFIED**. Before a Docker job, Red/current lock custodian must
post an explicit release/status handoff that confirms: (a) the existing job
queue is idle and logs are preserved, (b) the shared lock is free and names
the next authorized job/agent, and (c) the lock is returned after that job.
The runner must also check the established resource gate immediately before
start (at least 4 GiB RAM and 8 GiB disk) and stop below 3 GiB RAM or 6 GiB
disk. No Docker command is authorized by this preparation note. The owner must
separately authorize the first Docker build/test window; no one should infer
HOLD release from available machine capacity alone.

For the owner's selected initial appliance-side build route, producing the
installer archive does not require `--preload-vms-image` or a Docker build on
the release workstation. The later disposable Ubuntu install/lifecycle
acceptance does require Docker on that test guest and therefore still waits for
the explicit lock/owner authorization above. Do not switch silently to image
preloading.

## 4. GitHub review-branch backup and secret-scan plan

The current worktree has preserved untracked test/evidence items. They are not
part of a Git commit and must remain out of any review backup/push:
`_evidence/`, `_tmp-authz-tests/`, `_tmp-trust-tests/`,
`app/.pytest-temp/`, and `red-trust-uncommitted-review.patch`. Do not delete,
stage, or copy them into a source archive.

`aws.env` is ignored now, but Git history contains a prior `aws.env` path.
The corresponding historical blobs contain nonempty, non-placeholder values
assigned to secret/password-named application fields. The file also contains a
nonempty secret-manager identifier. This confirms historical credential-like
material was committed; it does **not** establish whether those values are
production credentials or whether any remain valid. No conventional
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, or `AWS_SESSION_TOKEN` assignment
was found by field-name inspection. No value, value hash, or raw blob was
printed or copied into this report.

The path-history commits are:

| Commit | Path action / exposure |
|---|---|
| `4a521409a23cf6c5cea330004ebf3e38c57aa8ac` | Added `aws.env`; credential-like fields present |
| `1c5d053b89628d4d87d6e14fb9d20de99a0a91d0` | Modified file; credential-like fields present |
| `d89d681d37c4ad45e308059ab41f67194054a187` | File present in commit tree; credential-like fields present |
| `08441d1a51726fb4698df53e0efa0d26e0194da0` | Modified file; credential-like fields present |
| `b5ebc430c95b6c5ff328fea16e18c6469344c7bb` | File present in commit tree; credential-like fields present |
| `ee69f97c7e1f89f61e2fa9998bea7b0f0545081e` | Deleted `aws.env` from that tree; prior blobs remain in ancestry |
| `2e2524d07cbeec9823fadb084b718039c4579bad` | Later path-history commit; file absent from this tree |

In this clone, each of the five file-containing commits is an ancestor of all
four cached remote-tracking refs: `origin/claude/red-1271-rrec`,
`red/claude/red-1271-authz`, `red/claude/red-1271-hwplan`, and
`red/claude/red-1271-trust`. The file-containing history is also reachable
from cached tags `blue-win-1.3.3-test-53e8b41`,
`blue-win-1.3.3-test-906f781`, `checkpoint-before-camera-ui-unification-20260827`,
`green-activation-21372fd0-hold`, `release-candidate-5c459c4-20260827`,
`v0.1.3`, `v0.9.0`, and `v0.9.1`. This is a read-only local-ref assessment;
refs were not fetched, so it does not assert the current live server state.

A Git bundle or branch push carries reachable history and can carry those
blobs even though the current file is ignored. Keep the remote review-branch
backup **blocked** until the credential owner determines whether the exposed
values are still valid, handles any necessary revocation/rotation under
separate approval, and the owner approves a secure backup destination. Do not
rewrite history or push to a public repository.

When the owner authorizes backup preparation, use this gated procedure:

1. Confirm destination repository visibility, branch name, access list and
   retention; preserve the exact source/report refs and verify ancestry.
2. Run a local history-aware secret scan over all commits reachable from the
   proposed refs (including `aws.env`) with scanner output redirected to a
   restricted local report. Configure the report to redact matched values;
   never paste raw findings into chat, logs or commits. Review file paths and
   commit IDs, not credential values.
3. Run a current-tree scanner on the staged review diff and verify ignored and
   untracked evidence is not staged (`git status --short`, `git diff --cached
   --name-only`). Confirm no `.env`, database, test fixture secret, recording,
   key or private runtime artifact is included.
4. If a history finding remains, do not push/bundle the history to a public
   remote. Ask the owner/security custodian to assess and, if still valid,
   rotate/revoke through their approved process. Any history rewrite requires
   separate owner authorization and a preservation plan; none is performed
   here.
5. After an explicitly approved secure destination exists, create the backup
   from named reviewed refs only, encrypt it with an owner-controlled key
   stored separately, verify bundle/list/ref integrity and SHA-256, and test
   restore into a new isolated directory. Record only hashes, refs and scan
   status in the handoff. Never overwrite the original repository.
6. A GitHub push remains a separate action requiring explicit approval of the
   exact repository, branch, visibility and commit range.

### Credential-owner remediation requirements

1. The credential custodian must assess the exposed portal, administrator and
   SMTP values by credential identity in the protected secret systems, without
   copying the historical values into a report. This checkout cannot verify
   whether they are live.
2. If any value is current or validity is uncertain, the owner must approve
   revocation/rotation and the custodian must rotate it through the normal
   secret-management process, then verify dependent services using sanitized
   status only. This review did not rotate or test credentials.
3. Record the custodian's sanitized determination and remediation completion
   date. Retain the historical exposure classification even after rotation.
4. Until the owner approves backup handling for a repository whose reachable
   history includes these blobs, do not generate or transfer a history-bearing
   bundle and do not push a review branch.

## 5. Exact remaining inputs and owner/release actions

No password, token, cloud credential, or private key should be sent to Codex.
The build is blocked until release engineering receives these verified
non-secret materials and decisions:

1. Production TRUST-1 canonical registry JSON plus its independently obtained
   SHA-256; supplied by the authorized cloud trust administrator via the
   approved release-materials channel.
2. Software Update verification public-key PEM plus independently checked
   fingerprint/provenance; supplied by the release-signing custodian. No
   private signing key is needed for installer construction.
3. MediaMTX version approval (historical candidate v1.21.0 Linux amd64 versus
   any explicitly approved update), official archive/checksum/attestation and
   exact extracted-binary SHA. Release engineering supplies the verified
   artifact; this checkout does not contain the full Linux digest.
4. Owner approved product version `1.2.7.1`, historical MediaMTX target
   `v1.21.0` Linux amd64 subject to official artifact/full-hash verification,
   and preference for the appliance-side Docker build initially. Do not
   substitute v1.21.2.
5. Appliance-side image build is the selected initial path, but Docker HOLD
   remains active: no Docker execution until Red releases the hold and the
   owner separately authorizes a test/build window. Preloading remains a
   documented alternate for review; it requires the same lock/resource gates.
   No GHCR publication is needed for either current path; if GHCR is later
   proposed, repository visibility, immutable tag/digest and publication
   approval are separate gates.
6. Before customer distribution, owner/legal determination of the pinned
   Ultralytics and LPR model distribution terms remains outstanding; it is not
   resolved by an installer build.

Green's existing acceptance package was copied unchanged to the shared handoff
as `CODEX-GREEN-ACCEPTANCE-PACKAGE-1271.md`; the source and copy both hash to
`E6DF8D5CE290713DAA3B2F815B72E95F85CE118160F7D76C4AD89436D7FDFB0E`. A
coordination request for Red's lock/custodian confirmation and Green's
read-only review is at `CODEX-RELEASE-PREP-REQUEST-20261024.md` in that same
handoff. No production credentials were requested or accessed. No Docker
command, installer build, download, GitHub push, appliance access, or
production action was performed for this preparation note.

## 6. Safe Ubuntu 24.04 / Python 3.12 installer acceptance sequence

This is a prepared procedure, not authorization to install. Use a fresh,
disposable Ubuntu 24.04 test VM with a verified snapshot/export and a separate
scratch data disk. It must not be Ryzen, Samsung, production EC2, or a VM with
customer data. Keep Docker HOLD in place until its custodian and the owner
separately release the test window. Apply the existing RAM/disk start and stop
limits. Use synthetic accounts, cameras, events, and recordings only.

1. **Identity and input gate:** verify OS release, Python 3.12, amd64
   architecture, Docker/Compose versions and host resources. Confirm exact
   source commit/version and clean build checkout. Verify and record hashes of
   registry, independently sourced registry digest, update public key and
   fingerprint, MediaMTX archive and executable. Refuse to proceed if any
   source, signer, environment, or digest is missing or inconsistent.
2. **Offline source suites:** run `bash installer/tests/run_tests.sh`,
   `python3 -m unittest discover -s installer/tests -v`, and the Green
   acceptance package's exact Python product/security, MFA/admin/tenant,
   P2P/signaling and Node wrapper test groups. Preserve raw logs and status;
   do not count skips as passes. Run installer trust-registry tests and
   appliance-agent trust/update tests in the same Linux environment.
3. **PostgreSQL 16:** create two separately named, disposable PostgreSQL 16
   databases and prove their endpoint/database names before tests. Run all
   migration tests against both; first startup must apply expected migrations,
   second startup must apply zero. Seed only synthetic customers, purchase
   history, entitlements, subscription, and sessions. Exercise backup, restore,
   rollback and compare row counts/content to the synthetic baseline. Never
   use a production/customer URL or credential.
4. **Build immutable test artifact:** build version `1.2.7.1` from the exact
   source commit and verified release inputs. Initially omit
   `--preload-vms-image` per owner decision. Record installer SHA-256, size,
   manifest, package file hashes, Linux target and input provenance in a new
   versioned output directory; preserve existing artifacts.
5. **Fresh Local install:** snapshot/export the test VM first. Install the
   candidate with test-only mode/config. Verify the exact `/version` commit,
   basic motion/event recording, playback, and Local offline login. Verify
   premium analytics start OFF, cannot run without entitlement, and remain
   OFF after entitlement grant until an authorized user explicitly enables
   them. Verify MFA for admin login/setup/recovery and protected APIs.
6. **Fresh Hybrid install:** use a separately reset disposable snapshot or
   second disposable VM. Verify event-only recording, allowed cloud sync and
   customer isolation; ensure a service restart or cloud configuration sync
   does not change a disabled analytics setting or enable continuous
   recording. Never connect to live cloud services.
7. **Motion/event-only enforcement:** exercise new-camera provisioning with
   missing, malformed, legacy-continuous and event-only settings; upgrades,
   restarts and Local↔Hybrid transitions; then switch a synthetic active camera
   to event-only. Confirm immediately that continuous segments stop while
   motion/event clips continue, and that no continuous upload path or UI option
   is present.
8. **G2/TRUST-1:** install the supplied production-format registry only in
   this disposable test appliance. Verify root ownership/modes and registry
   hash; test active, retired, revoked and rotated keys; reject tampered,
   replayed, lower-generation, same-generation-different, rebound-key and
   revoked-key restoration. Test accepted local event recording during clock
   rollback/cloud outage while rejecting new invalid grants. Backup/restore
   registry, protected trust state and accepted capacity together; test
   mismatched restore fails closed and recovery with the exact approved
   registry succeeds.
9. **Upgrade/repair/rollback:** exercise supported 1.2.7-to-1.2.7.1 upgrade
   using the installer/repair path (the older 1.2.7 update applier does not
   understand TRUST-1 registry). Verify same-version repair, interrupted
   dependency/image download and retry, install failure rollback, manual
   rollback and restored `/version`. Confirm database, synthetic recordings,
   settings, identity, authorization/trust files and mode are preserved as
   specified. Do not test against appliances.
10. **Independent closeout:** Green repeats targeted acceptance on the exact
    artifact; Red confirms integration/source provenance. Store checksummed
    logs and screenshots without secrets or customer media. Any application,
    installer, or dependency change changes the candidate identity and
    requires affected tests plus a new exact-commit artifact.

## 7. Retrieval and exact build commands (prepared; not executed)

The official v1.21.0 release documents both the checksum-file check and GitHub
artifact-attestation verification. From a clean Ubuntu 24.04 amd64 staging
directory, after the release-materials owner authorizes the downloads:

```bash
set -euo pipefail
umask 077
mkdir -p "$HOME/release-inputs/mediamtx-v1.21.0"
cd "$HOME/release-inputs/mediamtx-v1.21.0"
version=v1.21.0
archive="mediamtx_${version}_linux_amd64.tar.gz"
base="https://github.com/bluenviron/mediamtx/releases/download/${version}"
curl --fail --location --proto '=https' --tlsv1.2 -o "$archive" "$base/$archive"
curl --fail --location --proto '=https' --tlsv1.2 -o checksums.sha256 "$base/checksums.sha256"
awk -v f="$archive" '$2 == f { print }' checksums.sha256 > selected.sha256
test "$(wc -l < selected.sha256)" -eq 1
sha256sum --check selected.sha256
gh attestation verify "$archive" --repo bluenviron/mediamtx
tar -tzf "$archive" > archive-files.txt
```

Before extraction, inspect `archive-files.txt`; reject absolute paths, parent
traversal, symlinks, unexpected executable paths, or a non-amd64 payload.
Extract into a new mode-0700 staging directory only after that check. Verify
the extracted `mediamtx` with `file`, `mediamtx --version`, and
`sha256sum`; compare the complete binary hash to the separately approved
release record, then preserve the archive hash, attestation result, binary
hash, version output, and operator identity in the release evidence. The
archive checksum and GitHub attestation establish upstream artifact
provenance; the builder's `--mediamtx-sha256` must be the full hash of the
extracted executable. The Ubuntu-side `mediamtx --version` and execution check
remain pending because the Linux binary was not run on Windows. Do not proceed
if the independent approved binary-hash record is unavailable or disagrees.

Once the public TRUST-1 registry/digest and public update key/fingerprint are
delivered and verified, build into a new, versioned output directory using
the frozen application commit. This is a command template, not an executed
build; replace only the bracketed file paths and digest values with the
verified release inputs:

```bash
python installer/build_release_installer.py \
  --vms-commit dcace5778c56988d447bd8a182a8308e7fb21dc5 \
  --vms-repo /path/to/clean/AnyAiCam-VMS \
  --release-version 1.2.7.1 \
  --entitlement-trust-registry /path/to/approved/production-trust-registry.json \
  --entitlement-trust-registry-sha256 <independently-obtained-registry-sha256> \
  --update-signing-public-key /path/to/approved/update-verification-public.pem \
  --mediamtx-binary /path/to/staged/mediamtx \
  --mediamtx-sha256 <independently-verified-executable-sha256> \
  --output-dir /path/to/new-output/linux-1.2.7.1-test
```

Do not add `--preload-vms-image`: the owner selected appliance-side image
build for the initial path. This installer build does not itself run Docker;
the later disposable Ubuntu installation/lifecycle test does. The builder
must fail if any approved trust/key/media input is missing or mismatched. Do
not use the `--no-*` options to bypass required release inputs. Record the
builder output manifest, source commit, exact command with local paths
redacted from any shared report, installer SHA-256 and size. Keep public
material hashes and provenance in evidence; never include credentials or
private signing material.

## 8. Current verification checkpoint — 2026-10-24

### Completed

- Source under test remains pinned to application commit
  `dcace5778c56988d447bd8a182a8308e7fb21dc5`. This checkpoint is documentation
  and verification only; no application source files were changed.
- Official MediaMTX v1.21.0 Linux amd64 archive checksum and GitHub attestation
  independently verified; extracted executable hash is recorded in §2 and
  evidence is preserved in `_evidence/mediamtx-v1.21.0-official-verification/`.
- Focused installer-builder regression file passed on Windows/Python
  `3.14.6`: **23 passed, 1 skipped, 17 subtests passed** in 8.34 seconds.
  Command: `pytest installer/tests/test_build_release_installer.py -q`.
  Pytest initially failed during fixture setup because its temporary shim was
  directed to a sandbox-protected system temp path. The identical test command
  passed after setting `TEMP` and `TMP` to `_pytest-scratch/` inside this
  isolated worktree. The test file exercises release manifest/payload checks,
  `git archive` line endings, tar executable modes and builder fail-fast
  behavior; inspection confirmed it does not invoke Docker. The one skipped
  test requires POSIX executable-bit behavior and is not counted as a pass.
- TRUST-1 installer input/manifest regression file passed on the same
  Windows/Python `3.14.6` checkout: **8 passed, 3 skipped, 2 subtests passed**
  in 1.63 seconds; command: `pytest installer/tests/test_entitlement_trust_registry_installer.py -q`.
  The
  skips are the root-owned/POSIX provisioning cases unavailable on Windows;
  they remain pending Linux acceptance and are not counted as passes. This
  suite used synthetic keys/registry fixtures only and did not load release
  keys or invoke Docker.
- Green's existing acceptance package was preserved unchanged at the shared
  handoff path recorded in the coordination note; its copied-file SHA-256 is
  `E6DF8D5CE290713DAA3B2F815B72E95F85CE118160F7D76C4AD89436D7FDFB0E`.
  Its referenced `_evidence/CODEX-SHA256SUMS.txt` manifest independently
  verified **45/45 entries**, with no missing or mismatched file (manifest
  SHA-256 `7c833b0add60a745f326ec416e6bd32337351ac45a70d3072634fa81c6ea565f`).

### Pending checklist and blockers

| Work item | State / evidence | Responsible party | Estimate / next action |
|---|---|---|---|
| TRUST-1 production public registry and independent digest | Missing; no production access performed | Authorized cloud trust administrator, identity to be confirmed by Red/owner | Obtain through approved release-materials channel; estimate one business day after custodian and transfer channel are identified |
| Software Update public key and separately verified fingerprint | Missing; private key is not requested | Existing release-signing custodian, identity to be confirmed by Red/owner | Obtain public PEM + provenance/fingerprint via approved channel; estimate one business day after custodian is identified |
| Current Docker queue/lock holder | NOT VERIFIED; October 23 lock-order note and an absent historical marker do not establish current status | Red/current lock custodian; owner authorizes a test window | Red to report queue, holder, authoritative lock path and preserved logs; no Docker until explicit owner authorization |
| Ubuntu 24.04 / Python 3.12 build and lifecycle acceptance | Pending; current focused test ran on Windows Python 3.14.6 only | Red / release engineering, with Green independent validation | Prepare after release inputs and owner-approved Docker window; estimate 1–2 days after access and lock are available |
| LPR commercial licensing | Distribution blocker; no legal clearance in this review | Owner/legal and release owner | Obtain documented commercial-use clearance before customer distribution; date not estimable until decision owner is assigned |
| Historical `aws.env` credential assessment and secure archival approval | Risk remains flagged; values are not shown/copied; no history-bearing bundle/push created | Credential owner/security and repository owner | Credential owner to assess validity and handle any required rotation under separate approval; owner to approve encrypted archival destination before any history-bearing backup |
| Green acceptance-package independent review | Package copied and hash recorded; review result not yet received | Green | Green to return exact findings against `dcace5778c56988d447bd8a182a8308e7fb21dc5`; estimate 1 business day after review begins |

### Release state

The MediaMTX artifact provenance sub-gate is **PASS**. The focused Windows
installer-builder tests are **PASS with one platform-specific skip**. Linux
installer construction and Linux acceptance remain **BLOCKED** on the missing
approved TRUST-1 registry/digest and update public key/fingerprint, plus a
separately authorized Ubuntu/Docker test window. No installer was built, no
Docker command was run, no credential or private key was accessed, and no
appliance or production system was changed. The next independent action is to
review Green's acceptance package and reconcile its claims against the frozen
source commit while Red supplies the requested custodian and lock-status
information.
