# Windows 1.3.4.1 minimal Tesseract build results — 2026-10-28

Two clean Tesseract 5.5.2 builds completed inside AAC-WinVMS-1341-Test only.
The 40-file runtime-and-notice payload is byte-identical between builds.
Dependency and technical notice checks PASS. Strict raw OCR acceptance FAILS:
8/10 cases over two repeats, with the same plate2 error as the preserved legacy
baseline. No final VMS installer was packaged; no production release is approved.

## Exact outputs and integrity

Guest candidate: C:\AAC-Test\BuildPreparation\Minimal-Tesseract-Build-20261028-01
Guest reference: same directory name with -repro-reference suffix.
The intervening configure-only launcher attempt is retained with -launcher-attempt suffix.

Host backup root:
C:\Hyper-V\AAC-WinVMS-1341-Test\Integration\Artifacts\Minimal-Tesseract-20261028

| Output | SHA-256 |
| --- | --- |
| Candidate/engine/tesseract.exe | ba579ac5e6dbde80a6d4806356b2118fef008a3dfc0f062e0cdba77540e10d88 |
| Candidate/engine/tessdata/eng.traineddata | 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2 |
| Candidate/repro-evidence/second-payload-SHA256.json | ac7d080d0d2dda6228cfc3e0de690b08cdc5a26c6f333bc3af387a6f8acb01ba |
| Candidate/tesseract55.lib (non-shipped static build product) | 269f8fc934c99013e48acb6cad957fbaca41bb4434d27e3ffdeab02671bb7802 |
| Reference/tesseract55.lib | 6bebc22dd02628982c297267125e3f8a5297aa09caea4f47708321424ddeb501 |

[All 40 runtime/model/notice file hashes](C:/Hyper-V/AAC-WinVMS-1341-Test/Integration/Artifacts/Minimal-Tesseract-20261028/Candidate/repro-evidence/second-payload-SHA256.json).
[All candidate EXE/DLL and other build-product hashes](C:/Hyper-V/AAC-WinVMS-1341-Test/Integration/Artifacts/Minimal-Tesseract-20261028/Candidate/all-binary-audit/all-binary-audit.json).
[Machine-readable result summary](C:/Hyper-V/AAC-WinVMS-1341-Test/Integration/Artifacts/Minimal-Tesseract-20261028/BUILD-RESULTS.json).

All 80 backed-up payload files and both static libraries were checked against
build-side hashes. No compiled binaries were executed on the R430 host.
The candidate is unsigned (NotSigned); no signing credentials were requested.

## Reproduction scope and toolchain

Both successful builds used fresh source/build directories at the same absolute
paths, with the first result preserved by renaming its directory before reuse.
Native configure/build argument arrays match exactly. Both verified the original
970 input entries; input-manifest SHA256 is
62873d3ac5b896615b31f1cda4a90d18dbc89939b21adf671024bc9ccbd6cdad.
Source lock SHA256 is 467751166f388592b7e480f1565493b60c1b99dbc6806cb6dc0aee77e8c95e23.
Tesseract source tar SHA256 is 6235ea0dae45ea137f59c09320406f5888383741924d98855bd2ce0d16b54f21.

- VS Build Tools 2022 17.14.43, installation build 17.14.37824.6.
- MSVC v143 cl.exe 19.44.35229.0; linker/dumpbin 14.44.35229.0.
- CMake 3.31.6-msvc6; MSBuild 17.14.60+43b635718.
- x64, Release, SDK target 10.0.22621.0; installed SDK tools 10.0.22621.5040.
- MSBuild parallelism 1; upstream MSVC /MP uses compiler workers within the guest.
- Locked archive/curl/training/tests/graphics/OpenMP-runtime/LTO/native/PCH/ccache
  switches and /Brepro flags retained. No recognition code or numerical flags
  were changed to address a fixture result.

Reproduction PASS applies to the executable, 14 runtime DLLs, English model,
and all assembled notices: 40 files. It is not a claim that temporary CMake
files or all developer artifacts are byte-identical. The 218 static-library
members have identical payloads; all 218 differing archive headers differ only
in timestamp fields. Original .lib files were preserved without normalization.
See Metadata/static-library-reproducibility.json. Cross-machine/path reproduction
has not been claimed.

The first launcher waited for persistent compiler telemetry after native command
exit; only verified children of this VM's own build were stopped. A subsequent
root-only wait exposed PowerShell 5's blank ExitCode behavior. The retained-handle
fix passed an exit-7 probe and the final complete build, with native exit codes
recorded per command. The configure-only attempt and its controller source remain
preserved. These were launcher issues, not Tesseract application defects.

MSVC warning C4849 remains in both compile logs at src/arch/dotproduct.cpp:25:
an OpenMP SIMD reduction clause is ignored. No source or flags were changed to
silence it; no causal link to the inherited plate2 result is established.

## Dependency audit

Every retained EXE/DLL output was checked: 20 per successful build and four from
the configure-only attempt, 44 file instances in total. Checks require AMD64,
parse normal and delay-load imports recursively, verify pinned runtime DLL bytes,
and compare imports with native dumpbin output. No forbidden/unresolved imports
were found. Supplemental literal checks for iconv.dll, charset.dll, archive.dll,
libcurl.dll and libxml2.dll also returned no hits. No libiconv, charset, archive, curl,
libxml2, ICU, OpenSSL, ssh, psl or lzo dependency occurs in the OCR payload.

The shipped runtime candidate contains one executable and 14 DLLs. Windows API
sets and installed VC runtime remain external prerequisites. The CMake compiler-ID
and LTO probe executables use installed Microsoft debug CRT from the verified
VS/SDK installation. They are non-distributed auxiliaries; no debug runtime was
allowed or copied into the OCR engine. The release whitelist remains strict,
including an explicit negative debug-CRT test. All eight policy tests passed in
the guest; raw output is in Metadata/minimal-auditor-tests-build-phase.log.

## Pinned runtime dependencies and notices

| Package | Version | Declared license |
| --- | --- | --- |
| leptonica | 1.87.0 | BSD-2-Clause |
| giflib | 5.2.2 | MIT |
| libjpeg-turbo | 3.2.0 | IJG AND BSD-3-Clause AND Zlib |
| openjpeg | 2.5.4 | BSD-2-Clause |
| libpng | 1.6.59 | zlib-acknowledgement |
| libzlib | 1.3.2 | Zlib |
| libtiff | 4.7.2 | HPND |
| lerc | 4.2.0 | Apache-2.0 |
| libdeflate | 1.26 | MIT |
| liblzma | 5.8.3 | 0BSD |
| zstd | 1.5.7 | BSD-3-Clause |
| libwebp-base | 1.6.0 | BSD-3-Clause |

Technical notice completeness PASS: 12 package mappings and 23 notice/provenance
files, plus NOTICE-MANIFEST.json. Each original notice matches sealed input hashes;
supplements match immutable upstream commits. Full Tesseract LICENSE/AUTHORS,
Leptonica notice, codec notices, IJG README and product acknowledgment, and model
LICENSE/README/provenance are retained under Candidate/distribution-notices.

A missing Lerc 4.2.0 NOTICE was recovered and included, along with the WebP 1.6.0
patent grant. Exact sources and hashes are tracked in installer/windows/tesseract-build/notices:

- https://raw.githubusercontent.com/Esri/lerc/e534980e11992cc5f11864b0dcff1af6a27cee0d/NOTICE
- https://raw.githubusercontent.com/webmproject/libwebp/4fa21912338357f89e4fd51cf2368325b59e9bd9/PATENTS

Required product acknowledgment is present verbatim:
This software is based in part on the work of the Independent JPEG Group.

Model source is tessdata_fast 4.1.0, release commit
65727574dfcd264acbb0c3e07860e4e9e9b22185, under Apache-2.0. Its pinned bytes
match the previously verified upstream Git blob bbef4675053b5b468cdb477053e28b1c698ba08e.
Leptonica's banner prints JPEG turbo 3.1.2, while the actual jpeg8.dll PE product
version is 3.2.0 and matches the sealed 3.2.0 package pin; both observations are
retained in engine-version.stdout.log and binary-versions.json.

No known missing technical notice or libiconv-specific issue remains for this
candidate. This does not authorize production release. Conservative build-plan
licensing/release flags remain false; the separate NOTICE-MANIFEST records the
completed technical notice check. The existing VMS vendor bundle/pin still names
the legacy libiconv-containing package and was deliberately not replaced while
strict OCR acceptance is failing.

## Bounded OCR acceptance

Actual tests used the original five PNG/TXT fixtures twice, --psm 7 and the
unchanged A-Z/0-9 whitelist. Each process had a 30-second timeout, maximum process
time budget 330 seconds. Actual total OCR-case time was 2.249s;
maximum case 0.282s. All ten native OCR processes exited 0;
there were zero timeouts. Harness exit was 1 because of recognition mismatches.
The acceptance tests ran on the first payload, proven byte-identical to the final
candidate; no redundant rerun or reclassification of failures was used.

| Image | Expected | Minimal (both repeats) | Legacy | Strict result |
| --- | --- | --- | --- | --- |
| plate1.png | ABC1234 | ABC1234 | ABC1234 | PASS |
| plate2.png | 7XYZ482 | TXYZ482 | TXYZ482 | FAIL |
| plate3.png | KDN5021 | KDN5021 | KDN5021 | PASS |
| plate4.png | HT88K2P | HT88K2P | HT88K2P | PASS |
| plate5.png | 9MPR317 | 9MPR317 | 9MPR317 | PASS |

Result: 8/10 strict passes, 10/10 predictions identical to legacy. The plate2
7-to-T error is inherited. No expectation, model, fixture, preprocessing or
production LPR behavior was changed. These synthetic diagnostics do not validate
the full licensed glyph-first camera/LPR pipeline.

Legacy raw report SHA256 remains
f1677bc657b9f0b7f8398a7b2d4a983b3d2e7c2466eb3f83a62ea3119363f642;
legacy preprocessing result SHA256 remains
24cee75c9aed9171f017e190a31c78d7b6559f835679c471901e90a954cda555.
All original 970 prepared inputs reverified unchanged after both builds.

## Remaining gates and scope

Strict OCR acceptance remains FAIL. Investigate the inherited mismatch against
the existing full LPR pipeline and an approved representative corpus without
changing these expectations or tuning production solely to these five images.
Do not package the final VMS installer. Replacing the legacy vendor bundle and
updating installer pins is a later integration step after OCR acceptance resolves.
No push, merge, publication, deployment or production signing occurred.
R430 host toolchain, other VMs, Linux validation and unrelated systems were not
modified. No VM restart or memory allocation change was performed.
