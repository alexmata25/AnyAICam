# Gateway build prerequisite remediation — 2026-11-02

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
