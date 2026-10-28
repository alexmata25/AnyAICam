# Windows 1.3.4.1 offline toolchain installation results

Installation and verification completed inside `AAC-WinVMS-1341-Test`
(`WIN-UVT6JIPSLLK`). No restart was requested or performed. No development tools
were installed on the R430 host; its VS 2022 instance query remains empty.

## Transfer integrity and storage

The source layout manifest SHA256 matched the accepted checkpoint:
`D39088321822A40B087272AD859276501308C495B1ED653C86DC0E24E7DE5EF7`.
All 929 layout entries and all 15 OCR supplement entries passed source validation.
Guest free storage before transfer was 146,249,662,464 bytes.

PowerShell Direct was available. Its initial per-file copy was slow, so that
owned transfer was stopped and its partial guest directory was preserved as
`C:\AAC-Test\Prerequisites\VS2022-20261028-partial-PowerShellDirect`.
A new TAR was created outside the original acquisition directory and copied
through Hyper-V Guest Services for this VM only. The service was restored to
its original disabled state. No networking/sharing/switch changes were made.

The archive matched on both sides:
`CA03E9D0BFDFDB5C99D430540CB04CD3664855BE41035BDE55B51E766422C715`.
All 929 layout files and 15 supplement files passed guest manifest validation
before setup. The complete guest package is under
`C:\AAC-Test\Prerequisites\VS2022-20261028`.
Original acquisition files, source bundles and handoffs were not modified.

## Installation and certificate prerequisite

Install location: `C:\AAC-Test\BuildTools2022`.
Setup used `--noWeb --quiet --wait --norestart` and the approved VCTools,
VC.Tools.x86.x64, VC.CMake.Project, Windows11SDK.22621 and Windows11SDK.26100
components. No substitute VS 2026 cache or duplicate SDK acquisition was used.

The first attempt returned 5003 because the offline guest lacked the Microsoft
Windows Code Signing PCA 2024 intermediate. Both required Microsoft roots were
already trusted. Following Microsoft's documented missing-certificate procedure,
the certificate was obtained from the official PKI URL and matched byte-for-byte
against the intermediate already present on the host. Only the guest's
LocalMachine CA (intermediate) store was updated; no root trust or signature
verification was bypassed.

- Documentation: https://learn.microsoft.com/en-us/troubleshoot/developer/visualstudio/installation/install-failure-2017-later-versions
- Source: https://www.microsoft.com/pkiops/certs/Microsoft%20Windows%20Code%20Signing%20PCA%202024.crt
- Thumbprint: `D30F05F637E605239C0070D1EA9860D434AC2A94`
- SHA256: `FE229EFC927F6D77B738896752A21803A59736BAA17BF5BE9A50C72E219CBCD2`

The retry completed with exit 0. VS reports a complete, launchable installation
and `isRebootRequired=false`. CBS and Windows Update pending-restart flags were
also false. The first verifier mishandled the JSON array returned by vswhere in
PowerShell 5; its lookup was corrected and verification resumed without another
installation. The failed lookup evidence is preserved with an explicit historical
filename, alongside the first setup attempt and all final successful results.

## Exact versions and functional checks

| Component | Installed version/result |
| --- | --- |
| VS Build Tools 2022 | 17.14.43; installation build 17.14.37824.6 |
| v143 compiler, x64 and x86 | cl.exe 19.44.35229.0 |
| Linker and dumpbin, x64 and x86 | 14.44.35229.0 |
| VC tools directory | 14.44.35207; serviced binary versions above are recorded separately |
| CMake | 3.31.6-msvc6 |
| SDK 22621 | Target directory 10.0.22621.0; makecat/rc 10.0.22621.5040 |
| SDK 26100 | Target directory 10.0.26100.0; makecat/rc 10.0.26100.7705 |
| SDK signing tools | signtool present for both; reported FileVersion 4.00; hashes retained |

Actual successful checks:

- x64 C compile/link/execute using SDK 26100, with PE machine 0x8664.
- x86 C compile/link/execute using SDK 22621, with PE machine 0x014C.
- CMake VS 2022 generator + v143 + SDK 22621: C++17 configure, build and execution
  passed, using parallelism 1 and executable exit code 0.
- CMake capabilities and compiler/linker/dumpbin/SDK tool paths, versions and
  SHA256 values recorded. Required SDK makecat, signtool and rc files found.
- Seven dependency-policy/recoverability tests passed on host and guest Python
  3.12.10. Sealed Leptonica dependency closure passes; legacy engine correctly
  rejects at forbidden archive.dll. The final minimal engine remains unbuilt.

Final measured guest free storage: 128,759,984,128 bytes. Available guest memory:
2,642,712 KiB at the final check. Startup and assigned RAM remained 4 GiB.
No setup or vs_buildtools processes remained at the final check.

## OCR preparation and remaining gates

Preparation-only execution completed at
`C:\AAC-Test\BuildPreparation\Minimal-Tesseract-20261028`.
All 970 sealed inputs verified. A writable source copy, original notices,
acquired licensing supplement, exact build arguments and PREPARED-ONLY marker
were preserved. `Compiled=false`, `LicensingCleared=false` and
`ReleaseArchiveCreated=false` are recorded in build-plan.json.

The new guest-only runner does not download or install tools. It rejects output
reuse and output inside the sealed input tree, selects VS 17/v143/x64/SDK 22621,
and limits builds to one job. Its explicit -Build mode uses a fresh output,
checks native exit codes, runs recursive normal/delay-load PE dependency checks,
stages only pinned DLL closure, saves dumpbin results and checks engine launch
and version. It does not create a release archive or claim OCR/notice acceptance.

Next safe component command, using an unused output directory:

```powershell
& C:\AAC-Test\Source\installer\windows\tesseract-build\build-tesseract-offline.ps1 `
  -Inputs C:\AAC-Test\BuildPreparation\TesseractInputs `
  -Output C:\AAC-Test\BuildPreparation\Minimal-Tesseract-Build-20261028-01 `
  -Build
```

No toolchain prerequisite remains missing. The minimal executable still must be
built and audited for forbidden/transitive DLLs, exact provenance, applicable
notices, model identity and recognition acceptance. Legacy five-image evidence
and expected results remain unchanged. Production LPR code was not modified.
Final VMS installer packaging, release signing, publication and deployment have
not begun and remain gated.

Host evidence: `C:\Hyper-V\AAC-WinVMS-1341-Test\Integration\Evidence\Toolchain-20261028`.
Guest evidence: `C:\AAC-Test\Evidence\Toolchain-20261028` and sibling Attempt1 folder.
Operational scripts, transfer TAR and certificate supplement are preserved under
the Integration directory; source preparation and audit code are committed in
the isolated integration branch with a recoverable Git bundle.
