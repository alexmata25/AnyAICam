# Minimal OCR offline preparation

The new `installer/windows/tesseract-build/build-tesseract-offline.ps1` is for
the approved Windows guest only. It never installs tools or downloads inputs.
The original Sandbox runner and transferred handoffs remain unchanged.

The default operation verifies the sealed input manifest, requires a verified
VS 2022 guest toolchain, copies source into a new writable directory, preserves
the input notices and acquired supplement, and records exact configure/build
arguments. It does not compile. Existing output and output inside the input tree
are rejected. Compilation requires explicit `-Build` with a fresh output path.

Preparation command inside the guest:

```powershell
& C:\AAC-Test\Source\installer\windows\tesseract-build\build-tesseract-offline.ps1 `
  -Inputs C:\AAC-Test\BuildPreparation\TesseractInputs `
  -Output C:\AAC-Test\BuildPreparation\Minimal-Tesseract-20261028
```

The future component build uses the same script and inputs with `-Build` and
a different, unused output directory. It explicitly selects the VS 17 generator,
v143, x64, SDK 22621, the locked feature switches, and build parallelism 1. Logs
and failed outputs are retained. An installer-requested reboot blocks this runner
until the toolchain/restart checkpoint is resolved.

## Dependency audit and current evidence

`audit-minimal-engine.py` reuses the existing normal/delay-load PE import parser,
requires AMD64 binaries, verifies candidate DLLs against the sealed input
manifest, rejects forbidden dependency families and unresolved imports, and
stages only the resulting DLL closure. Windows and VC runtime imports are
reported as external prerequisites, not as bundled or cleared libraries.
The runner also saves dumpbin output for every staged executable and DLL.

Eight policy tests cover actual synthetic PE normal/delay tables: recursive and
case-insensitive closure, delay import inclusion, transitive forbidden imports,
unresolved imports, rejection of debug CRT from release dependencies, x86
providers, malformed binaries, and preservation of prior
audit evidence. Read-only validation of sealed binaries
also passes the Leptonica x64 closure and rejects the legacy Tesseract closure
at `archive.dll`. None of these checks substitutes for auditing the unbuilt
minimal Tesseract executable.

The staged Leptonica DLL imports 13 non-system DLLs. Their package/notice map:

| DLL | Pinned package | Notice |
| --- | --- | --- |
| leptonica-1.87.0.dll (root) | leptonica 1.87.0 | leptonica-license.txt |
| deflate.dll | libdeflate 1.26 | COPYING |
| gif-7.dll | giflib 5.2.2 | COPYING; preserve accompanying getopt notice |
| jpeg8.dll | libjpeg-turbo 3.2.0 | LICENSE.md, README.ijg, IJG product acknowledgment |
| lerc.dll | lerc 4.2.0 | LICENSE and exact-release NOTICE |
| liblzma.dll | liblzma 5.8.3 | COPYING and COPYING.0BSD |
| libpng16.dll | libpng 1.6.59 | LICENSE, PNG Reference Library License v2 |
| libsharpyuv.dll, libwebp.dll, libwebpmux.dll | libwebp-base 1.6.0 | COPYING and PATENTS |
| openjp2.dll | openjpeg 2.5.4 | LICENSE |
| tiff.dll | libtiff 4.7.2 | LICENSE.md |
| zlib.dll | libzlib 1.3.2 | LICENSE |
| zstd.dll | zstd 1.5.7 | LICENSE |

Disabling Tesseract's own TIFF integration does not remove Leptonica's transitive
TIFF dependency. This map must be reconciled with the actual final executable
closure. Tesseract source LICENSE/AUTHORS and tessdata_fast model LICENSE/README
are also retained in the acquired supplement.

`prepare-ocr-notices.py` verifies the notice/package map against the actual
audited DLL set, verifies sealed notice hashes and immutable supplement commits,
and assembles full notices, the IJG acknowledgment and model provenance.
`audit-build-products.py` additionally checks CMake compiler-ID and LTO probe executables.
Those non-distributed auxiliaries may use the installed nonredistributable
debug CRT; debug CRT is never permitted or staged for the release engine.
Native launcher waits retain the process handle and wait for the command itself,
avoiding persistent telemetry-child waits and blank PowerShell 5 exit codes.

## Gates after compilation

No release archive or installer is created by this runner. Dependency success
is explicitly marked as OCR and notice review still pending. Run the unchanged
five-image diagnostic against the new engine, capture stdout separately from
stderr, enforce timeouts and exit codes, and report every recognition mismatch.
Preserve the legacy 4/5 raw and 2/5 preprocessing results. Do not change production
LPR code or expected text to make these fixtures pass. Full licensed glyph-first
LPR validation remains a separate acceptance requirement.

Resolve final binary provenance, normal/delay dependency closure, runtime launch,
model identity, applicable notices and OCR acceptance before considering final
VMS installer packaging. No licensing clearance is claimed by preparation.
