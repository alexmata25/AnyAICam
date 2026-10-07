# Windows installer dependencies

All customer installation inputs are embedded in the installer. Downloads occur only while preparing the ignored `vendor` build cache.

| Component | Version | Source | SHA-256 | License | Installed location | Role |
|---|---:|---|---|---|---|---|
| CPython embeddable x64 | 3.12.10 | `python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip` | `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3` | PSF-2.0 | `runtime/python` | Core |
| get-pip | build input captured 2026-09-07 | `bootstrap.pypa.io/get-pip.py` | `fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6` | MIT | Temporary bootstrap input | Core build input |
| Python wheels | versions in `requirements-windows.txt` (108 packages, 2026-10-07; top-level pins = the Linux image's `requirements*.txt`) | Python Package Index; torch/torchvision CPU builds from `download.pytorch.org/whl/cpu`; `http_ece` built from its sdist with `pip wheel` | Manifest: `wheels.lock.sha256` (`c30ecd320b7f9ac1c350d444f9ed4d6072e27f63eff3075cfc93c370acde7a3e`) | Per-package licenses | `runtime/python/Lib/site-packages` | Core |
| WinSW x64 | 2.12.0 | GitHub WinSW release | `05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da` | MIT | `service/AnyAiCamVMS.exe` | Core |
| FFmpeg essentials x64 | 8.1.2 | `gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip` | `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec` | GPLv3 | `runtime/tools/ffmpeg` | Core recording/HLS/media |
| MediaMTX x64 | 1.21.0 (same version as the Linux appliance) | `github.com/bluenviron/mediamtx/releases/download/v1.21.0/mediamtx_v1.21.0_windows_amd64.zip` (matches the release's `checksums.sha256`) | `8a58a9b8c25ee99a96c23dc0a17f39ace3072c01d2e148329073c64ddf83493d` | MIT | `runtime/tools/mediamtx` | Core WebRTC live view |
| Tesseract OCR engine | Not bundled | N/A | N/A | Apache-2.0 upstream | N/A | Optional license-plate recognition only |

`pytesseract` remains installed because the LPR module imports it, but the external OCR engine is called only when LPR analysis runs. Its absence does not block core VMS startup, camera recording, HLS, or playback.
