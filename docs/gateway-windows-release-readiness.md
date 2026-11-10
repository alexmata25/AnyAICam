# Gateway Windows release readiness: signing, licences, versions, acceptance

Status: PLANNED. Prepared by Claude, 2026-11-05, candidate `bc62066` and
later. Nothing here is released, signed, published or deployed.

## 1. Installer signing requirements

The build already supports `-Sign` (certificate store or token) and
`-AzureSign` (Azure Artifact Signing); see
`installer/windows/SIGNING.md`. To use them the owner must provide:

- **Identity:** a code-signing certificate issued to the legal entity that
  publishes AnyAiCam:
  - OV or EV from a Windows-trusted public CA;
  - EKU Code Signing (1.3.6.1.5.5.7.3.3);
  - or an Azure Artifact Signing account with an identity-validated
    certificate profile.
- **Key custody:** a hardware token, HSM or the managed cloud service.
  Never a PFX in the repo or on a shared disk. Record who may sign.
- **Signing host:** the R430 needs:
  - Windows SDK SignTool (the version pinned in the build);
  - for Azure, the Artifact Signing client tools.
- **Timestamp:** RFC 3161 with SHA-256 (already enforced).
- **Verification gates (already built):**
  - the setup and the cached uninstaller must be `Valid` and match the
    requested thumbprint;
  - after install, verify `unins000.exe` with `Get-AuthenticodeSignature`
    and `signtool verify /pa /all`.
- **Smart App Control:** leave it on for the signed Dell validation.
  Unsigned tests run only in a disposable VM with Smart App Control Off
  or Evaluation.

Owner decisions:
- which certificate type (OV, EV or Azure);
- the subject name;
- who holds the key.

## 2. Open-source licence compliance (legal gate)

Three bundled components carry copyleft obligations. All must be
resolved before customer distribution.

1. **libiconv 1.18** (`iconv.dll`, `charset.dll`), LGPL-2.1-only.
   Imported by `archive.dll` and `libxml2.dll` in the Tesseract bundle.
   - Notice: ship the LGPL-2.1 text and a libiconv notice in `licenses/`
     (the package's licence file is already in `licenses/tesseract/`).
   - Source offer: either ship the corresponding source
     (`libiconv-1.18` tarball plus the conda-forge recipe and patches
     for build `hc1393d2_3`), or include a written offer valid for 3
     years.
   - Replaceability: it is dynamically linked as a separate DLL, so a
     user can replace it. The installer's file-by-file verification
     must not be presented as preventing that for personal use; the
     EULA must not forbid reverse engineering for debugging those
     modifications (LGPL-2.1 section 6).
   - Alternative: rebuild Tesseract with `-DDISABLE_ARCHIVE=ON
     -DDISABLE_CURL=ON` to drop libiconv (needs a compiler toolchain,
     not on this host).
2. **FFmpeg 8.1.2 essentials build (gyan.dev)**, GPLv3.
   - GPLv3 needs the complete corresponding source (or a written offer)
     for the exact build, the licence text, and no added restrictions on
     that binary.
   - It ships as a separate executable, invoked as a program, which keeps
     the rest of AnyAiCam out of GPL scope. Counsel should confirm this.
   - Alternative: an LGPL-only FFmpeg build (no `--enable-gpl` components).
3. **psycopg-binary**, LGPL-3.0 (already flagged in `DEPENDENCIES.md`).
   Same three obligations: notice, source and replaceability.

Recommended package (PLANNED, needs legal sign-off):
- `licenses/THIRD-PARTY-NOTICES.txt` listing every component;
- a `source-offer/` folder on the download site holding the exact source
  archives, with SHA-256 values in the release manifest;
- an installer page or README line pointing to it.

## 3. Version recommendations

Current facts:
- Windows customer releases: 1.3.0 and 1.3.1, then 1.3.2.
  The downgrade guard orders by `AppVersion`, so it must keep rising.
- Blue's Windows testing: 1.3.4.1.
- The test build from 42039e2 used setup `1.3.4.90`, a test-only number.
- `build.ps1` needs `-ReleaseVersion` as `X.Y.Z`. `AppVersion` is the
  setup's own version.

Recommendation (owner decision):
- **Release version 1.4.0** for the first Windows build that includes the
  Gateway editions. This is a new product line and new licensing, so a
  minor version bump is warranted.
- **Setup versions:**
  - test builds: `1.4.0.9x` (e.g. `1.4.0.90`), never published;
  - the first customer setup: `1.4.0.100` or higher, so any test build
    a tester installed is upgraded, never downgraded.
- Keep one edition-neutral installer, because the edition is decided on
  the server. If marketing wants separate download names, use the same
  version with distinct file names.
- Tag the signed release commit in the candidate repo only after Codex
  verification and owner approval.

## 4. Isolated acceptance evidence (needed before any customer claim)

The VM script (`installer/windows/acceptance/New-GatewayAcceptanceVM.ps1`)
creates nothing without these files. The owner or operator supplies
them, hashed:

- **Clean parent disk:**
  - a Windows evaluation VHDX from Microsoft's media;
  - its SHA-256 matching the download page;
  - never used to sign in to any AnyAiCam or personal account.
- **Network isolation:**
  - an internal Hyper-V switch with no external adapter and no NAT;
  - `Get-VMSwitch` output plus `Get-NetNat` showing no rule for it.
  - Do not reuse the AnyAiCam-Agent switch or NAT.
- **Resources at start:** at least 9 GiB free, and at least 4 GiB left
  after start (enforced).

Test list once approved (PLANNED):
- install;
- service health;
- UI;
- edition display;
- claim link opens;
- Local Gateway refuses cloud upload;
- uninstall with ProgramData preserved;
- reinstall and upgrade from 1.4.0.90 to a later test build;
- downgrade refused;
- unsigned trust warnings recorded.

## Summary of owner gates

- Signing identity and key custody.
- Legal sign-off on LGPL and GPL obligations, or the rebuild alternatives.
- Release and setup version numbers (recommended: 1.4.0 / 1.4.0.100).
- VM approval with clean-disk and isolation evidence.
