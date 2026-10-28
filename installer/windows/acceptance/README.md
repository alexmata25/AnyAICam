# Windows VMS 1.3.4.1 installer acceptance tools

Test tools only. The setup does not package this folder.

**Result files.** Every tool writes a JSON result (`test`, `mode`, `checks[]`, `result` = PASS / FAIL / BLOCKED) and exits:
- **0** = PASS;
- **1** = FAIL;
- **2** = BLOCKED (the evidence could not be produced; never counted as a pass).

| ID | Tool | Proves | Where it must run for acceptance |
|---|---|---|---|
| A1 | `Test-TesseractLocalSystem.ps1` | The bundled Tesseract launches and finds `eng` **as LocalSystem**, with the service's exact PATH and TESSDATA_PREFIX. Installed files match the bundle manifest; licences present; not blocked by App Control; optional OCR with LPR's flags. | **R430 VM**, elevated, installed 1.3.4.1 |
| A3 | `Test-SapiLocalSystem.ps1` | The product's `synthesize_speech_sapi` produces audible 22,050 Hz speech **as LocalSystem**, levelled to −28 dBFS ± 3; the product selects `windows-sapi`. | **R430 VM**, elevated (Server may have no voice: then FAIL with the reason) |
| A4 | `Test-VmsJobObject.ps1` | `-Mode Simulate`: the real `vms-job-limits.ps1` limits as Windows reports them; child and grandchild inherit the job; breakaway refused. `-Mode Service`: the WinSW service tree is in a job and the launcher logged the configured limits. | Simulate: any PC. Service: **R430 VM**, elevated |
| A5 | `Test-RuntimeCatalogCoverage.ps1` | **Every** native file under the install folder is in the signed AnyAiCam runtime catalog (Windows catalog hash vs catalog members) or validly signed. The catalog itself is valid and pinned. pip-generated launchers are reported separately. | Any PC with the install (read-only); build stage with `-AllowUnsignedCatalog` |
| A6 | `Get-AnyAiCamInstallState.ps1` + `Compare-AnyAiCamInstallState.ps1` + `LIFECYCLE-ACCEPTANCE-CHECKLIST.md` | Upgrade from the accepted signed 1.3.4, repair and uninstall keep customer data and settings, and leave services, catalog and firewall in the right state. | **R430 VM**, elevated |
| A7 | `..\installer-size-policy.ps1` (called by `..\build.ps1`) | A setup over 750,000,000 bytes fails the build before signing or verification. | Every build |

**LocalSystem mode.** Uses a one-shot scheduled task with principal SYSTEM (`\AnyAiCamAcceptance\…`), removed after the run. It needs an elevated PowerShell. `-RunAs CurrentUser` runs the same probe as the current user: useful locally, but it is **not** LocalSystem evidence.

## Proven on the Dell (2026-10-28, non-elevated, read-only towards the installed VMS)
- **A3 CurrentUser:** real SAPI + product code, PASS (David / Zira voices, 5.8 s, −25.8 dBFS levelled).
- **A4 Simulate:** PASS (Windows reports cpu=50;memoryMB=4096; child + grandchild in the job; breakaway error 5).
- **A5 on the installed signed 1.3.4** (`C:\Program Files\AnyAiCam`):
  - 349 native files: 315 in the catalog, 1 signed (uninstaller), **0 uncovered**;
  - **33 pip-generated `runtime\python\Scripts\*.exe` launchers outside the catalog → FAIL by default** (a finding; see the handoff);
  - catalog Valid and pinned.
- **A6 snapshot of the installed 1.3.4:** works. Data "access denied" without elevation, so a compare correctly reports BLOCKED.
- **A1:** logic tested with a mock engine (CurrentUser). The real engine as LocalSystem is R430 work.
- **Automated tests:** `installer/tests/test_windows_acceptance_tools_1341.py`.
