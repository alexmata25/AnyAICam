# Windows 1.3.4.1 offline toolchain and OCR gates — 2026-10-28

Checkpoint reviewed: 9adcc5c3b52be3c940df0ee6336aa4c05f3f158d. Final installer packaging is blocked. No production LPR behavior or recognition expectations changed.

## Existing resources and missing acquisition

Guest has Python 3.12.10, 108 pinned runtime wheels, Git 2.56.0.windows.4, Inno Setup 7.1.0, and VC runtime 14.44.35211. Original verified transfer packages remain immutable.

Guest lacks MSVC, CMake and Windows SDK. Host has VS 2026 Build Tools 18.11 with VC 14.51 cache; it is not the kit's VS 2022/v143 toolchain. Host SDK cache has setup executables but not a complete offline payload. No suitable offline layout was found in checked host Downloads, C:\ISO, C:\Packages, test Incoming packages, or guest tool/dependency directories. This does not assert absence from every disk.

Acquire a sealed COMPLETE Visual Studio 2022 Build Tools (17.x) offline layout, not just vs_buildtools.exe. Include bootstrapper, catalog/channel manifests, response file, and all MSI/CAB/VSIX dependencies for these explicit component IDs:

| Required component | Version/architecture and purpose |
| --- | --- |
| Microsoft.VisualStudio.Workload.VCTools | VS 2022 17.x C++ workload |
| Microsoft.VisualStudio.Component.VC.Tools.x86.x64 | v143, x64 host/target tools; cl, link, dumpbin, headers and libraries |
| Microsoft.VisualStudio.Component.VC.CMake.Project | Bundled CMake; Visual Studio 17 2022 generator requires CMake >=3.21; target x64 |
| Microsoft.VisualStudio.Component.Windows11SDK.22621 | SDK 10.0.22621.0 requested by original OCR build script |
| Microsoft.VisualStudio.Component.Windows11SDK.26100 | SDK with 10.0.26100.0 x64 makecat required by installer build default; retain signing/catalog tools |

The lock does NOT pin an MSVC or CMake patch release. Do not invent exact patch versions: select a supported VS 2022 release layout, record its full catalog/component versions and hashes, verify it offline, and record actual cl/CMake/SDK versions before building. No separate CMake archive is needed if the complete layout includes the component. A standalone archive would require a separately verified version, provenance and checksum.

Proposed host transfer folder: C:\Hyper-V\AAC-WinVMS-1341-Test\Incoming\VS2022-Layout
Guest staging: C:\AAC-Test\Prerequisites\VS2022-Layout
Use a short collection path (for example C:\VS2022Layout). Include SHA256SUMS for every payload and acquisition manifest with official URLs, product/channel/version, selected component IDs, Microsoft signature evidence and layout --verify result. Verify again after transfer; install ONLY inside the isolated Windows guest using offline/noWeb mode. Never copy an installed host compiler directory as a substitute layout.

Official references:
- https://learn.microsoft.com/en-us/visualstudio/install/create-a-network-installation-of-visual-studio?view=vs-2022
- https://learn.microsoft.com/en-us/visualstudio/install/workload-component-id-vs-build-tools?view=vs-2022
- https://cmake.org/cmake/help/latest/generator/Visual%20Studio%2017%202022.html

## OCR inputs and notice review

Sealed tesseract-minimal-build-inputs.zip contains Tesseract 5.5.2 source, tessdata_fast 4.1.0 English model, 12 pinned win-64 dependency packages staged as prefix, package notices, five diagnostic fixtures, and legacy comparison inputs. Its 970 manifest-covered inputs previously verified. Source SHA256: 6235ea0dae45ea137f59c09320406f5888383741924d98855bd2ce0d16b54f21. English model SHA256: 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2.

Disable libarchive/libcurl, training tools, OpenMP and graphics per lock. This removes the intended libiconv dependency path, but configuration flags alone do NOT prove the final executable is free of it. Minimal executable has not been built; final dependency closure is therefore unverified and licensing gate remains OPEN. Legacy engine is not an acceptable substitute: it imports the forbidden dependency chain.

Preserve full notices for Tesseract (Apache-2.0) and every library actually shipped: leptonica 1.87.0, giflib 5.2.2, libjpeg-turbo 3.2.0, openjpeg 2.5.4, libpng 1.6.59, libzlib 1.3.2, libtiff 4.7.2, lerc 4.2.0, libdeflate 1.26, liblzma 5.8.3, zstd 1.5.7 and libwebp-base 1.6.0. Final list must follow actual PE closure, not blindly include all prefix DLLs.

Actual staged notice review:
- libjpeg-turbo LICENSE.md explicitly requires binary product documentation to say: This software is based in part on the work of the Independent JPEG Group. Include this acknowledgment and applicable BSD notice. Referenced README.ijg is absent from staged notices; recover matching 3.2.0 upstream notice for complete provenance review before distribution.
- No separate tessdata model license is staged. Recover the exact tessdata_fast 4.1.0 model license/provenance and retain applicable Apache notices.
- giflib getopt_LICENSE.txt contains permissive Miller/NetBSD notices, not an identified libiconv license.
- liblzma COPYING distinguishes 0BSD liblzma from optional LGPL getopt in command-line tools and GPL scripts. Ship only the required DLL closure, not unrelated tools/scripts; retain COPYING and COPYING.0BSD.
- libpng's actual notice is PNG Reference Library License v2; do not reduce notice selection to the lock's abbreviated label.
- These are engineering notice findings, not completed legal clearance. Resolve remaining notice/provenance and final artifact mapping before distribution.

## Exact next build steps

1. Obtain and verify the full VS 2022 layout and missing matching notice material above; preserve original handoffs. No packages downloaded during this review.
2. Install tools inside guest only. Record selected VS instance/version, cl, CMake, x64 SDK and makecat paths. Keep working 4 GB startup memory and monitor memory; use bounded build parallelism.
3. Revise the build runner in a separate reviewed commit before use: remove network/bootstrap installation; select preinstalled VS 17 explicitly; reject reused output directories; check exit codes; use bounded parallelism; fail on missing/forbidden imports and failed OCR expectations. Existing runner downloads tools, copies every prefix DLL and does not make OCR/dependency failures reliable success gates. Do not execute it unchanged.
4. Build minimal Tesseract in a NEW guest output directory using sealed inputs and locked CMake options. Preserve complete logs and record tool versions, input hashes, flags and engine SHA256.
5. Parse both normal and delay-load PE imports recursively for the executable and each shipped DLL. Require x64; resolve each non-system import to an exact pinned provider; reject libiconv/charset, archive, curl, libxml2, ICU, OpenSSL, ssh, psl and lzo. Confirm with dumpbin and isolated launch. Do not treat Windows system/VC runtime exclusions as bundled library provenance.
6. Package only required closure, model and complete notices in a NEW candidate OCR archive with per-file SHA256 and provenance manifest. Keep minimal engine/bundle pins unset until artifact verification succeeds.
7. Run strict five-image diagnostic and compare with preserved legacy results without changing expectations. Legacy raw result is 4/5 (7XYZ482 recognized TXYZ482); preprocessing diagnostic is 2/5. Investigate minimal results and full licensed glyph-first LPR path separately; five synthetic images do not justify production algorithm or expectation changes.
8. Only after toolchain, dependency, notice and OCR acceptance gates resolve, consider the VMS installer build and isolated acceptance tests. No final installer packaging authorized while gates remain open.
