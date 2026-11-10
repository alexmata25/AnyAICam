# Windows installer dependencies

All customer installation inputs are embedded in the installer. Downloads occur only while preparing the ignored `vendor` build cache.

The shared Full VMS / Local Gateway / Cloud Gateway installer requires all eight
pinned vendor inputs and the complete 108-wheel lock. Optional runtime analytics
do not make their packaged dependencies optional build inputs. Gateway editions
must not silently strip Vosk, Tesseract or the Full VMS dependency set.
Blue's sealed 1.3.4 handoff records Inno Setup **7.1.0**; build.ps1 checks the
compiler's numeric PE version metadata before staging or compiler execution.
This check does not establish authenticity; approved provenance/hashes and
normal publisher verification are still required. Prefer recovery
of the sealed toolchain; any replacement version/install needs owner approval.
See `../../docs/windows-gateway-prerequisite-remediation.md` for provenance,
local-cache findings, interpreter roles and unresolved approval gates.

| Component | Version | Source | SHA-256 | License | Installed location | Role |
|---|---:|---|---|---|---|---|
| CPython embeddable x64 | 3.12.10 | `python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip` | `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3` | PSF-2.0 | `runtime/python` | Core |
| get-pip | build input captured 2026-09-07 | `bootstrap.pypa.io/get-pip.py` | `fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6` | MIT | Temporary bootstrap input | Core build input |
| Python wheels | versions in `requirements-windows.txt` (106 packages after the psycopg exclusion 46713ee, 2026-10-29; top-level pins = the Linux image's `requirements*.txt`) | Python Package Index; torch/torchvision CPU builds from `download.pytorch.org/whl/cpu`; `http_ece` built from its sdist with `pip wheel` | Manifest: `wheels.lock.sha256` (`e9f7038d488b962821423de7ac155de805cc7f0ab4e5f253e4052aacdc828c6e`) | Per-package licenses | `runtime/python/Lib/site-packages` | Core |
| WinSW x64 | 2.12.0 | GitHub WinSW release | `05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da` | MIT | `service/AnyAiCamVMS.exe` | Core |
| FFmpeg essentials x64 | 8.1.2 | `gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip` | `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec` | GPLv3 | `runtime/tools/ffmpeg`; licence text in `licenses/ffmpeg/LICENSE` | Core recording/HLS/media |
| MediaMTX x64 | 1.21.0 (same version as the Linux appliance) | `github.com/bluenviron/mediamtx/releases/download/v1.21.0/mediamtx_v1.21.0_windows_amd64.zip` (matches the release's `checksums.sha256`) | `8a58a9b8c25ee99a96c23dc0a17f39ace3072c01d2e148329073c64ddf83493d` | MIT | `runtime/tools/mediamtx` | Core WebRTC live view |
| Microsoft Visual C++ 2015-2022 Redistributable x64 | 14.44.35211 (captured 2026-10-07; Authenticode: Microsoft Corporation, checked by `build.ps1`) | `aka.ms/vs/17/release/vc_redist.x64.exe`, stored as `vc_redist.x64-14.44.35211.exe` | `cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b` | Microsoft Visual C++ Redistributable license | Windows system (`/install /quiet /norestart`; kept on uninstall, shared component) | Core: torch/cv2 DLLs need it; a clean Windows lacks it |
| Vosk small US English speech model | vosk-model-small-en-us-0.15 (the same pin as the Linux Dockerfiles) | `alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip` | `30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498`; every extracted file in `vosk-model-small-en-us-0.15.sha256` | Apache-2.0 (Copyright 2020 Alpha Cephei Inc); notice `notices/vosk-model-small-en-us-0.15-NOTICE.txt`, installed in `licenses/` | `%ProgramData%\AnyAiCam\models\stt\vosk-model-small-en-us-0.15` (SYSTEM/Administrators only; verified, kept or replaced on every install by `install-stt-model.ps1`) | Voice Call listening (offline speech-to-text) |
| Tesseract OCR engine bundle | Tesseract 5.5.2 + its 27-DLL import closure (leptonica 1.87.0, libarchive, libcurl, ICU, libtiff, ...) and the English model (byte-identical to tessdata_fast 4.1.0 `eng.traineddata`, as on Linux) | 23 pinned conda-forge win-64 packages (`tesseract-bundle.lock.json`, each checked against conda-forge's published SHA-256), assembled deterministically by `prepare-tesseract-bundle.py` into `tesseract-5.5.2-win64-anyaicam.zip`; the DLL list is re-derived from the PE import tables on every run | `221b4599a9310737d8f3a2d8a93477eb3c495d41b5eca8af6ed7b79e0781fbf7`; every file in the zip's `BUNDLE-MANIFEST.json` | Apache-2.0 (Tesseract) and permissive licences for every library, EXCEPT libiconv 1.18 (`iconv.dll`, `charset.dll`): LGPL-2.1-only, imported by archive.dll/libxml2.dll. **Legal gate before customer distribution** (notice, source offer, replaceability; same class as psycopg-binary). Each package's own licence files ship in `licenses/tesseract/` | `runtime/tools/tesseract` (replaced as a whole and verified file by file by `install-runtime.ps1`; on the VMS's PATH, `TESSDATA_PREFIX` set by `service-launcher.ps1`) | Licence plate reading (OCR) |

Setup order: Visual C++ runtime (exit 0, 1638 already installed, or 3010 restart needed; anything else stops setup), then the Python runtime, then `runtime-preflight.py` with the bundled Python (imports fastapi, uvicorn, numpy, cv2, torch, torchvision, ultralytics and runs one torch operation), and only then the service. A failed preflight stops setup before any service is installed.

`pytesseract` remains installed because the LPR module imports it, but the external OCR engine is called only when LPR analysis runs. Its absence does not block core VMS startup, camera recording, HLS, or playback.

The build-time keyset validator can use the verified per-user CPython 3.12.10 explicitly with `build.ps1 -PythonExe <absolute-path-to-python.exe>`. This avoids relying on a system Python Launcher registration. The build script verifies that the selected executable reports exactly Python 3.12.10 before it validates the public entitlement keyset and trust registry.

## Excluded on Windows (1.3.4.1)

| Package | Licence | Why excluded | Effect |
|---|---|---|---|
| `psycopg` / `psycopg-binary` 3.2.3 (pinned on Linux as `psycopg[binary]==3.2.3`) | LGPL-3.0 | The Windows edge is SQLite-only: `cloud_config.database_backend` defaults to `sqlite`, the installer never sets `ANYAICAM_DATABASE_BACKEND`/`ANYAICAM_DATABASE_URL`, and `psycopg` is imported only inside `database_backend.py`'s PostgreSQL branch. No other wheel requires it. | None at runtime; selecting PostgreSQL on Windows fails closed with `Install psycopg[binary] to use PostgreSQL.` Removes the LGPL psycopg item from the Windows distribution. |

Not excluded: `libiconv` 1.18 (LGPL-2.1) inside the Tesseract bundle -- `tesseract.exe` imports `archive.dll`, which imports `iconv.dll` + `charset.dll`; removing them would stop LPR. It can only go with the minimal Tesseract rebuild (`-DDISABLE_ARCHIVE=ON -DDISABLE_CURL=ON`).

## Inside a Python wheel

`opencv_python-4.10.0.84` carries `cv2/opencv_videoio_ffmpeg4100_64.dll`, SHA-256 `3de83b84588b3ee8bacdbea85a8f92d4855a32a1108183963315a7db06ad5744`: FFmpeg 4.4.4 (LGPL-2.1-or-later) with libvpx 1.14.0 (BSD-3-Clause) and libaom 3.9.0 (BSD-2-Clause), built from `opencv/opencv_3rdparty` commit `394dca6ceb3085c979415e6385996b6570e94153`. It reads HLS frames for analytics. Notice: `notices/opencv-ffmpeg-NOTICE.txt`, installed in `licenses/`.
