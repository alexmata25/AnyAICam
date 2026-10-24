# Linux 1.2.7.1 release-input and preservation plan

Date: 2026-10-24
Application/test source: `dcace5778c56988d447bd8a182a8308e7fb21dc5`
Review branch: `codex/linux-1271-integrated`
Frozen baseline: `b8c7a7edba7083ee703bb7a4d71bc8f27ea7dc5c`

This is an offline preparation record. No installer, image, registry, key, or
MediaMTX artifact was built or downloaded. Docker HOLD is still in force.

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

**Important evidence gap:** the tracked Linux release notes preserve only an
abbreviated binary hash (`9fac297a…01ad`) and archive hash (`e02e34c3…`), not
the exact full 64-hex SHA-256 values. No Linux MediaMTX binary is present in
this tracked checkout. Therefore the historical pin is identifiable, but its
exact expected hash is not independently recoverable from this source tree.
Do not pass a guessed or Windows checksum to the builder.

Required before build: obtain the exact v1.21.0 Linux amd64 archive and its
official release checksum/attestation from the upstream release, verify the
asset using the published checksum and GitHub attestation procedure, extract
`mediamtx`, compute its SHA-256, and have release engineering compare it with
the independently preserved historical binary hash or an approved release
record. Preserve the archive hash and extracted-binary hash in the build
evidence. The builder accepts the extracted binary plus its exact SHA-256 and
refuses mismatch.

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

No production credentials were requested or accessed. No Docker command,
installer build, download, GitHub push, appliance access, or production action
was performed for this preparation note.
