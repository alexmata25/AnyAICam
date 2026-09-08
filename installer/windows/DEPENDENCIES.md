# Windows installer dependencies

All customer installation inputs are embedded in the installer. Downloads occur only while preparing the ignored `vendor` build cache.

| Component | Version | Source | SHA-256 | License | Installed location | Role |
|---|---:|---|---|---|---|---|
| CPython embeddable x64 | 3.12.10 | `python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip` | `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3` | PSF-2.0 | `runtime/python` | Core |
| get-pip | build input captured 2026-09-07 | `bootstrap.pypa.io/get-pip.py` | `fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6` | MIT | Temporary bootstrap input | Core build input |
| Python wheels | versions in `requirements-windows.txt` | Python Package Index | Manifest: `wheels.lock.sha256` (`00a7b09d310188fadc8f07addb15b704e70af953a1d3182e530312f9c4f6fce3`) | Per-package licenses | `runtime/python/Lib/site-packages` | Core |
| WinSW x64 | 2.12.0 | GitHub WinSW release | `05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da` | MIT | `service/AnyAiCamVMS.exe` | Core |
| FFmpeg essentials x64 | 8.1.2 | `gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip` | `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec` | GPLv3 | `runtime/tools/ffmpeg` | Core recording/HLS/media |
| Tesseract OCR engine | Not bundled | N/A | N/A | Apache-2.0 upstream | N/A | Optional license-plate recognition only |

`pytesseract` remains installed because the LPR module imports it, but the external OCR engine is called only when LPR analysis runs. Its absence does not block core VMS startup, camera recording, HLS, or playback.
