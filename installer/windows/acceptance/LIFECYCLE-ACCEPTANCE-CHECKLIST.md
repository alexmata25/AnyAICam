# Windows VMS 1.3.4.1: upgrade / repair / uninstall acceptance checklist (A6)

**Where:** the R430 test VM `AAC-WinVMS-1341-Test` only (Server 2022, Private switch). Never the Dell's live install.

**Baseline:** the accepted signed Windows 1.3.4 installer `AnyAiCam-VMS-Setup-1.3.4-e3d7eda.exe`.
- SHA-256 `CAFC1D1BE9F457561094FBF756255D64266A249AE75C994C78A14A3E725DF034`, 618,425,712 bytes.
- Dell path: `C:\VMS134Build\rc11-e3d7edab\installer\windows\output\`.

**Candidate:** the 1.3.4.1 setup Codex builds from the integrated tree (record its SHA-256 here: `________`).

**Run all commands elevated** from `installer\windows\acceptance\` of the same source tree. Every snapshot is read-only. `vms.env` values are recorded only for non-secret switches.

## 0. Preconditions
- [ ] VM checkpoint taken before step 1 (name: `pre-1341-lifecycle`), so each step can be repeated from a clean state.
- [ ] Both setups' SHA-256 verified (`Get-FileHash`). Both setups' Authenticode `Valid` with the pinned AnyAiCam publisher (`publisher-pin.ps1`).
- [ ] The camera simulator (`installer\windows\sandbox\onvif-camera-sim.py`) is running if recordings are to be compared.

## 1. Install the baseline 1.3.4 and create customer data
- [ ] `AnyAiCam-VMS-Setup-1.3.4-e3d7eda.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ProductMode=Local /LOG=C:\Acceptance\A6\01-install-134.log`
- [ ] Sign in, add the simulated camera, and let at least one event recording be written.
- [ ] Put one non-default switch in `vms.env`, for example `ANYAICAM_LPR_ENABLED=false`. That proves an explicit choice survives (the upgrade must keep `false`).
- [ ] `.\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\01-before-upgrade.json`

## 2. Upgrade 1.3.4 → 1.3.4.1
- [ ] `AnyAiCam-VMS-Setup-1.3.4.1-<sha>.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=C:\Acceptance\A6\02-upgrade.log`
- [ ] `.\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\02-after-upgrade.json`
- [ ] `.\Compare-AnyAiCamInstallState.ps1 -Before C:\Acceptance\A6\01-before-upgrade.json -After C:\Acceptance\A6\02-after-upgrade.json -Step Upgrade -ExpectedVersion 1.3.4.1`. Expect **PASS**:
  - version changed and build id changed;
  - Tesseract installed;
  - the LPR switch kept as explicitly set;
  - identity files, recordings, database, `vms.env` keys and settings preserved;
  - services Auto / Running / LocalSystem;
  - catalog and firewall present.
- [ ] Run the component tests on the upgraded install:
  - `.\Test-TesseractLocalSystem.ps1 -OutputDir C:\Acceptance\A6\02`
  - `.\Test-SapiLocalSystem.ps1 -OutputDir C:\Acceptance\A6\02`
  - `.\Test-VmsJobObject.ps1 -Mode Service -OutputDir C:\Acceptance\A6\02`
  - `.\Test-RuntimeCatalogCoverage.ps1 -OutputDir C:\Acceptance\A6\02`
- [ ] Event-only recording still holds after the upgrade: no new recording between events (Codex's event-only harness).

## 3. Repair (re-run the same 1.3.4.1 setup)
- [ ] `.\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\03-before-repair.json`
- [ ] Optional damage, to prove repair restores files: delete `C:\Program Files\AnyAiCam\service\vms-job-limits.ps1` (the VM only).
- [ ] Re-run the same setup: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=C:\Acceptance\A6\03-repair.log`
- [ ] `.\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\03-after-repair.json`
- [ ] `.\Compare-AnyAiCamInstallState.ps1 -Before …03-before-repair.json -After …03-after-repair.json -Step Repair`. Expect **PASS**:
  - same version;
  - no program file lost;
  - scripts and Tesseract manifest identical;
  - data and settings unchanged;
  - services running.

## 4. Downgrade refusal (already covered by `install-version-guard.ps1` tests; confirm once)
- [ ] Running the 1.3.4 setup over 1.3.4.1 is refused and nothing changes. Snapshot before and after; the compare with `-Step Repair` must PASS (no change).

## 5. Uninstall
- [ ] `.\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\05-before-uninstall.json`
- [ ] `"C:\Program Files\AnyAiCam\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=C:\Acceptance\A6\05-uninstall.log`
- [ ] `.\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\05-after-uninstall.json`
- [ ] `.\Compare-AnyAiCamInstallState.ps1 -Before …05-before-uninstall.json -After …05-after-uninstall.json -Step Uninstall`. Expect **PASS**:
  - unregistered;
  - program folder, both services, runtime catalog and firewall rules removed;
  - ProgramData kept (identity, database, recordings, `vms.env`).
- [ ] No AnyAiCam process left running (`Get-Process | ? Path -like 'C:\Program Files\AnyAiCam\*'` is empty).

## 6. Reinstall over kept data
- [ ] Install 1.3.4.1 again. The compare against `05-before-uninstall.json` with `-Step Repair` must show identity, settings and recordings intact, and the service running with the same cloud link (no new claim).

## Evidence to hand back
- `C:\Acceptance\A6\*.json` and `*.log`.
- The SHA-256 of both setups.
- The VM checkpoint name.
- Any FAIL check names with their `detail`.
