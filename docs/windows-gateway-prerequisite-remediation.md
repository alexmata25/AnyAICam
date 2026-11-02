# Gateway build prerequisite remediation — 2026-11-02

## Recovery evidence refresh (from 056bbd5)

Read-only search covered the transfer directory's existing handoffs/manifests,
candidate filenames under `C:\AnyAiCam-Transfer` and Administrator Downloads,
the previously bounded cache audit, repository build/signing documentation and
recorded original-machine paths. No remote machine was contacted, no binary was
extracted and no other project's files were changed. This is not an all-disks
absence claim. A reference to a package on AlexDellLaptop is a recovery lead,
not evidence of accessible bytes on this host.

| Component | Required version / identifier | Existing location | Verification status | Exact action needed |
|---|---|---|---|---|
| Sealed dependency package | `R430-win134-parity-d6b4244`; SHA256SUMS digest `e9d6d867a764e48c07f6090d196ef0660dfac6c30f1dd91e6e49267e32bd43a0` | Referenced by local `C:\AnyAiCam-Transfer\R430-win1341-release-handoff-579578b\HANDOFF.md` section 6; payload not found in searched paths | Handoff current SHA-256 matches its manifest (`190ae127...5898`); package seal cannot be checked without payload | Owner transfer the unchanged package plus independently confirmed seal, or authorize exact original-machine access; verify every manifest entry before staging |
| Build-helper Python installer | 3.12.10 x64 | `C:\AnyAiCam-Transfer\R430-win1341-supplement-20261028\tools\python-3.12.10-amd64.exe` | Found outside workspace; current SHA-256 `67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb` matches supplement; not installed | Approve installation and normal authenticity validation, then pass installed interpreter as `-PythonExe`; existing 3.11 helper fallback is not the shipped runtime |
| Embedded Python and seven other vendor inputs | Embedded 3.12.10, get-pip pinned capture, WinSW 2.12.0, FFmpeg 8.1.2, MediaMTX 1.21.0, VC redist 14.44.35211, Vosk 0.15, AnyAiCam Tesseract 5.5.2 | Earlier sealed package; separate Tesseract handoff lead below | Exact archives not found in prior bounded cache audit; versions/hashes remain in build.ps1/DEPENDENCIES.md | Recover pinned archives; full Python installer does not replace embedded ZIP; do not regenerate or substitute binaries |
| Wheels | 108 exact locked Windows/cp312 or compatible wheels | Lock/manifests in checkout; payload expected in sealed package | Prior reconciliation 108/108 pins consistent; no verified matching cache payload | Restore all locked files and verify hashes; no resolver/download performed |
| Inno Setup | Baseline 7.1.0 | Referenced in sealed package; expected compiler `%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe` absent | build.ps1 requires major-7 path but does not itself pin patch metadata; 7.1.0 comes from sealed provenance | Recover approved installer/hash, verify provenance/version and obtain installation approval; do not silently use another patch |
| Public entitlement keyset | Canonical Ed25519 public-only set; historical digest `2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c` | Referenced in sealed package; approved payload not located | Historical pin known; current intended environment and actual key IDs not verified | Owner confirm intended environment/digest and recover approved file; enumerate public key IDs only after canonical validation; never infer IDs from test fixtures or supply private keys |
| SDK/signing | SDK tools 10.0.26100.0; current authorized certificate or Azure signing session matching publisher pin | `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\{makecat,signtool}.exe` present | Presence only; no execution/login/store inspection. Historical setup signer expired 2026-10-27 | Owner authorize current signing workflow/session; preserve publisher subject/EKU/root pins; do not change signing keys or assume timestamped baseline's leaf can sign anew |
| Installer baseline/version | Historical setup 1.3.4, product release 1.2.7 | `C:\AnyAiCam-Transfer\R430-win1341-release-handoff-579578b\installer\AnyAiCam-VMS-Setup-1.3.4-e3d7eda.exe` | Handoff records 618,425,712 bytes and SHA `cafc1d1be9f457561094fbf756255d64266a249ae75c994c78a14a3e725df034`; not rehashed or signature-checked this refresh | Approve new monotonically ordered `-Version` and three-part `-ReleaseVersion`; 1.3.4.1 is historical planned work, not an approved beta version or existing candidate |

All editions still use one `AnyAiCam-VMS.iss`, AppId and WinSW layout. Edition
arguments select installation configuration; they do not omit dependency inputs
or create separate architectures. Readiness remains BLOCKED for compilation;
files present outside the checkout (Python installer, handoffs, SDK tools and
baseline setup) must not be described as missing dependencies.

## Immediate recovery procedure (source d95780e)

1. **Request the sealed dependency directory**, not a newly resolved environment:
   `R430-win134-parity-d6b4244`, sealed 2026-10-27. Its original `SHA256SUMS.txt`
   must hash to **`e9d6d867a764e48c07f6090d196ef0660dfac6c30f1dd91e6e49267e32bd43a0`**.
   Blue's verified local `R430-win1341-release-handoff-579578b/HANDOFF.md` §6
   identifies this package as containing vendor files, 108 wheels, Inno **7.1.0**
   and the public keyset. The current 579578b package does **not** duplicate them.
   Folder, `.zip` and `.7z` variants were unavailable at the recorded local
   `C:\AnyAiCam-Transfer\` paths. Do not infer the earlier package's exact asset
   filenames or completeness until its sealed manifest is supplied.
2. **Request Blue's Tesseract artifact** from
   `anyaicam-blue\windows-parity-scope-20261023\handoff-artifacts-d6b4244` if it
   is not in that package: `tesseract-5.5.2-win64-anyaicam.zip`, SHA-256
   `221b4599a9310737d8f3a2d8a93477eb3c495d41b5eca8af6ed7b79e0781fbf7`.
   Check the recovered set against all eight names/hashes in build.ps1 and all
   108 unchanged wheel pins; do not substitute a newer get-pip/redist, generic
   Tesseract installer, or freshly rebuilt wheel. Exact versions/sources follow.
3. **Reuse the Python installer already here**; no download is needed:
   `C:\AnyAiCam-Transfer\R430-win1341-supplement-20261028\tools\python-3.12.10-amd64.exe`.
   Current SHA-256 matches the sealed supplement:
   **`67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb`**,
   26,964,224 bytes. The supplement's manifest/authenticity-record hashes also
   match. Blue recorded a valid Python Software Foundation signature and original
   python.org provenance. Current Authenticode/Sigstore verification was not run.
   Obtain approval before installation; then supply the installed 3.12.10
   interpreter via -PythonExe. The installer EXE itself is not -PythonExe.
4. **Original Dell access is not assumed.** This session is ALEXDELLSERVER;
   the supplement identifies the original as AlexDellLaptop and records Python's
   source under `C:\Users\Alejandro Mata\.codex\worktrees\540d\AnyAiCam-VMS\test-artifacts\rc11-normal-user-20261025-170614\`.
   The original build directory `C:\VMS134Build\rc11-e3d7edab` is unavailable
   here. No laptop connection was attempted. Owner must transfer the sealed
   package or explicitly provide/authorize access to the correct machine/paths.
5. **Before compilation**, recover/verify the pinned inputs into the ignored
   vendor cache; approve Inno 7.1.0 and Python installation, setup/ReleaseVersion, and the public
   keyset/environment/digest. The historical public keyset pin is
   `2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c`;
   independently confirm it before use. Select a current authorized signing
   mode/session. The handoff reports its then-current signing certificate expired
   2026-10-27; that does not invalidate an already timestamped baseline installer,
   but it cannot be assumed usable for a new signature. No credentials are needed
   in chat. A separate resource-window approval is still required to compile.

**Packaging confirmation:** build.ps1 produces one shared VMS setup, with all
eight vendor inputs and the full locked runtime. Edition selection is an
installation-time argument to that setup, **not a build.ps1 switch**:

| Fresh installation | Setup arguments |
|---|---|
| Local Gateway | `/GatewayEdition=local_gateway` (Local bootstrap) |
| Cloud Gateway | `/GatewayEdition=cloud_gateway` (Hybrid bootstrap; uploads remain off) |
| Full VMS | `/GatewayEdition=full_vms /ProductMode=local` or `hybrid` |

All package the same recursive app source, edition helper, AppId, WinSW services
and protected data layout. They are not separate applications or side-by-side
installations. Existing repair/upgrade keeps its saved edition/mode. The accepted
signed 1.3.4 setup is present in the 579578b handoff (618,425,712 bytes); it is
lifecycle evidence, not a replacement for build dependencies and was not extracted.

No installation, download, broad cache rescan, build, service or VM action was
performed for this targeted recovery check. Blue's sealed handoffs were read only.

Source baseline `f7d7533`; machine inspected: **AlexDellServer**. No software
installed, binaries downloaded, installers extracted, build compiled, VM changed,
certificate/private-key store opened or AWS/Stripe service accessed.

## Inno Setup: required lineage and approved source

The repository's build.ps1 uses `%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe`.
It previously specified only major 7, not an exact patch. Blue's sealed handoff
at `C:\AnyAiCam-Transfer\R430-win1341-release-handoff-579578b\HANDOFF.md`, section
6, identifies **Inno Setup 7.1.0** in the earlier sealed
`R430-win134-parity-d6b4244` package. That is the exact documented baseline for
reproduction; there is no evidence here authorizing an automatic patch upgrade.
The earlier package is not available at its recorded transfer path on this host.

Official source for 7.1.0 is the [upstream immutable release](https://github.com/jrsoftware/issrc/releases/tag/is-7_1_0).
The [official download page](https://jrsoftware.org/isdl.php) currently advertises
7.1.1 x64, dated 2026-10-28, which is a **different version**, not silently approved
for this beta. The [upstream verification instructions](https://jrsoftware.org/isdl-verify.php)
require a valid Pyrsys B.V. publisher signature and describe release attestations
and signature-file verification. No installer asset was retrieved or installed.
Recover the sealed 7.1.0 installer/hash record or approve and validate a replacement.
An official URL alone is not a recorded owner-approved binary hash.

## Eight vendor inputs: all required by this shared installer

Exact approved SHA-256 values remain in build.ps1 and DEPENDENCIES.md. No pin,
package version or installer feature set was changed. Copy only from a trusted
retained cache whose bytes match those pins; filenames alone confer no approval.

| Input | Version / role | Approved provenance or recovery path |
|---|---|---|
| `python-3.12.10-embed-amd64.zip` | CPython 3.12.10, shipped private runtime | Recorded python.org `/ftp/python/3.12.10/` archive; earlier sealed vendor cache |
| `get-pip.py` | Captured 2026-09-07 bootstrap | Earlier sealed copy pinned `fb24e693…`; bootstrap.pypa.io is rolling and today's bytes must not replace it without a pin match |
| `WinSW-x64.exe` | 2.12.0, existing VMS/agent service wrappers | Recorded WinSW GitHub release, preferably the sealed pinned copy |
| `ffmpeg-8.1.2-essentials_build.zip` | 8.1.2, local recording/HLS/event clips | Recorded gyan.dev `/ffmpeg/builds/packages/` asset, pinned `db580001…` |
| `mediamtx_v1.21.0_windows_amd64.zip` | 1.21.0, existing live-view pipeline | Recorded bluenviron/mediamtx GitHub v1.21.0 asset, pinned `8a58a9b8…` |
| `vc_redist.x64-14.44.35211.exe` | 14.44.35211, native runtime dependency | Retained Microsoft-signed pinned copy `cc0ff0eb…`; aka.ms is rolling, so latest redist is not interchangeable |
| `vosk-model-small-en-us-0.15.zip` | 0.15, offline Voice Call listening | Recorded alphacephei.com model asset; pinned `30f26242…` plus per-file manifest |
| `tesseract-5.5.2-win64-anyaicam.zip` | 5.5.2, OCR engine/model/closure | AnyAiCam's deterministic bundle, pinned `221b4599…`; recover Blue's sealed bundle, not an arbitrary Tesseract installer |

Blue's handoff says the earlier sealed parity package contains vendor files,
108 wheels, Inno 7.1.0 and public keyset. It separately identifies
`anyaicam-blue\windows-parity-scope-20261023\handoff-artifacts-d6b4244` for the
Tesseract bundle/minimal-build inputs. Those are **documented recovery leads**,
not files verified present on this host. Restoring the bundle is preferable to
rebuilding its 23 pinned conda-forge inputs under this low-resource authorization.

## Wheel audit

Artifacts: `windows-gateway-wheel-audit.csv` (one row per locked wheel) and
`windows-gateway-wheel-audit.json` (scan scope, results and limits).

- 108 lock entries, 108 Windows requirement pins, zero missing requirement, zero
  Windows version mismatch and zero applicable top-level pin mismatch.
- torch 2.5.1+cpu / torchvision 0.20.1+cpu remain the explicitly pinned Windows
  CPU variants. They are not replaceable by GPU builds or similarly named wheels.
- Existing provenance: PyPI, PyTorch's CPU index, and the previously built
  `http_ece` 1.2.1 wheel. Rebuilding a wheel need not produce the locked hash.
- Bounded exact-filename scan covered this checkout's vendor location, local
  uv wheel cache, pip cache location, Downloads, and the 579578b release handoff.
  uv: 112 directories; Downloads: 7; release handoff: 9. No exact wheel or vendor
  archive candidates found; no matched file bytes needed hashing. Pip/vendor locations
  were absent. No archives were extracted or unpacked uv packages repackaged.
- The old `C:\VMS134Build`, `anyaicam-red`, `wt-127-win-build` and earlier parity
  transfer paths were unavailable at their recorded locations. A broader user
  directory listing was access-denied and was not bypassed. No assertion of
  global disk/cache absence is made.

Restore the approved sealed cache and verify every archive hash against the
unchanged wheel lock. No `pip download`, resolver run or package installation
was performed. Do not replace lock entries with currently available versions.

## Interpreter, build and signing verification

| Item | Actual finding / safe action |
|---|---|
| Git | `C:\Program Files\Git\cmd\git.exe`, file metadata 2.56.0.windows.4, already used for local commits. build-tools.ps1 now resolves it without global/process PATH changes; optional `-GitExe` is passed to both commit lookup and source-integrity checks |
| Current Python | `python --version` = 3.15.0; `py --list-paths` lists only `C:\Python315\python.exe`. It is the lightweight test interpreter, not an approved explicit build interpreter |
| Keyset-helper Python | Existing build branch accepts explicit `-PythonExe` only when it reports exactly 3.12.10; otherwise Blue's existing `py -3.11` helper fallback remains. Neither qualifying interpreter was found at the documented locations/launcher |
| Shipped Python | Always the pinned embedded 3.12.10 archive and cp312 Windows wheel set; independent of the helper fallback. Do not substitute the 3.15 runtime |
| Early preflight | Interpreter/public-keyset validation now precedes vendor hashing, native extraction/catalog generation/signing. No build executed to test this; source-order and helper tests cover the change |
| Public keyset | Historical production pin in the existing checklist: `2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c`. Recover approved public-only file and independently confirm this is still the intended environment/pin. No public/private key fetched, printed or replaced |
| SDK tools | makecat/signtool at SDK 10.0.26100.0 paths exist; presence does not establish signature or successful execution |
| Certificate signing | Required process settings are unset; certificate/private-key store not inspected. No conclusion that the owner lacks a certificate |
| Azure signing | DLL/metadata absent at current configured/default paths; no identity/login/session inspected or initiated. Do not infer credentials from another project |
| Source / output | Same editions/AppId, dependency pins, publisher gates, complete native catalog and 750 MB policy. No generated installer or signed beta candidate exists from this work |

The existing build-version and release-version choices remain owner decisions.
No compiler/SDK/signing command, application import, torch workload or build
preflight runtime was executed. Signing documentation's historical cache text
does not override build.ps1's actual Inno 7 behavior or the pinned publisher checks.

## Owner action versus locally resolvable items

**Resolved locally without installation:** Git executable discovery and consistent
source-integrity forwarding; early interpreter/keyset failure ordering; manifest
reconciliation; bounded local-cache inventory. SDK files are already present,
but still require normal build/signature validation during an approved window.

**Owner action required before building:**

1. Provide the earlier sealed 7.1.0 toolchain/vendor/wheel transfer package (and
   its manifest) or approve recovery/download of exact pinned inputs. No usable
   vendor/wheel copies were found in the audited cache locations.
2. Approve software installation of baseline Inno 7.1.0, or explicitly approve
   a different version and record its verification/hash. No installation occurs now.
3. Supply an approved existing helper interpreter or approve installing it:
   explicit Python 3.12.10 is preferred by the recorded per-user build path;
   the existing Python 3.11 keyset-helper fallback remains supported.
4. Supply/confirm the approved public keyset and independent digest, setup version
   and customer-facing ReleaseVersion. Do not supply a private signing key in chat.
5. Select the existing certificate or Azure signing mode and arrange its approved
   prerequisites/session through the owner's normal secure process. No credentials
   should appear in repository files or build reports.
6. Separately approve any downloads, installation, hash-intensive recovery,
   compilation or VM acceptance window. Before a heavy-window request, collect
   fresh R430 RAM, estimated additional RAM/duration and running VMs to preserve.

No changes to Full VMS features, either Gateway edition, retention, licensing,
Blue's acceptance tools or the pinned binary set are required for these repairs.
