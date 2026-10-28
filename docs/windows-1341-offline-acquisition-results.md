# Offline acquisition results — 2026-10-28

Collection computer: ALEXDELLSERVER (R430 host), internet access to official Microsoft download service verified. Acquisition/staging only; no development tools installed on host and no guest transfer or installation performed.

New acquisition root: C:\Hyper-V\AAC-WinVMS-1341-Test\Acquisition\VS2022-20261028
Original source bundles, handoffs and legacy OCR evidence were not modified.

## Space and layout

Measured before collection: host C: free 5,589,330,477,056 bytes; guest C: free 146,279,215,104 bytes. Completed layout: 929 files, 3,077,772,645 bytes (3.08 GB / 2.87 GiB). Guest installation/cache/build space is not measured yet; the previous 60 GB free-space allowance is comfortably satisfied before staging.

Official https://aka.ms/vs/17/release/vs_buildtools.exe resolved to the Microsoft Visual Studio download CDN. Bootstrapper Authenticode: Valid, Microsoft Corporation. ProductVersion is the descriptive string Visual Studio 2022, not a numeric major version; numeric build identity was verified from catalog and bootstrapper log.
Bootstrapper SHA256: 4AAD1082E180F558598C346EB105B465B4908148CB9F7F0511AA1105620E0C30.
Catalog: VisualStudio/17.14.43+37824.6, product line 2022, VisualStudio.17.Release, BuildTools.
Explicit selected components: Workload.VCTools; Component.VC.Tools.x86.x64; Component.VC.CMake.Project; Component.Windows11SDK.22621; Component.Windows11SDK.26100 (all prefixed Microsoft.VisualStudio.).
Compiler payload: v143 14.44.35229 (HostX64 TargetX64/TargetX86). CMake package 17.14.36017.18; archive module family 3.31; actual CMake executable patch version must be recorded inside guest after installation. SDK package versions: 10.0.22621.7 and 10.0.26100.15. Both SDK payloads are present; no duplicate standalone SDK downloads.

Acquisition completed with exit 0 in bootstrapper evidence. Microsoft --verify completed with exit 0: No problem was found. Logs retained under Evidence. Layout-SHA256.csv covers all 929 files.
Manifest SHA256: D39088321822A40B087272AD859276501308C495B1ED653C86DC0E24E7DE5EF7.

## OCR supplement

OCR-Licensing-Supplement contains Tesseract 5.5.2 LICENSE/README/AUTHORS, libjpeg-turbo 3.2.0 LICENSE.md/README.ijg, tessdata_fast 4.1.0 LICENSE/README, and Leptonica 1.87.0 notice copied read-only from sealed inputs. Six license/README downloads match content independently fetched by immutable release commit URL. Tesseract's root release inventory has LICENSE and AUTHORS, with no root NOTICE. Inventory and pinned provenance JSON retained.
Tesseract commit: 6e1d56a847e697de07b38619356550e5cf4e8633.
libjpeg-turbo commit: c85e6b905bf237038faa936dab160ebfc5da0344.
tessdata_fast commit: 65727574dfcd264acbb0c3e07860e4e9e9b22185.
The existing English model matches its pinned SHA256 and upstream release Git blob bbef4675053b5b468cdb477053e28b1c698ba08e. No duplicate model archive downloaded.
Required IJG product acknowledgment is recorded in supplement README. OCR-Supplement-SHA256.csv covers supplement files. This is acquisition evidence, not final licensing clearance.

## Next safe action and remaining gates

Acquisition has no remaining network/access blocker. Recheck sealed manifest before staging into C:\AAC-Test\Prerequisites\VS2022-Layout, then verify again inside the Windows guest. Install only inside guest using offline/noWeb mode with the same explicit component set. Do not use VS 2026 host cache. Keep VM startup memory 4 GB.
Before minimal OCR build: revise original network-installing runner for preinstalled VS 17, bounded build concurrency, unique output, checked exit codes, strict OCR/dependency failures and complete notices. Record actual compiler/CMake/SDK versions inside guest. Final normal/delay-load PE closure, libiconv absence, notice mapping and OCR acceptance remain OPEN. No final VMS installer packaging or production release clearance.
