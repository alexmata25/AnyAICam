# AnyAiCam VMS — native Windows installer (2026-10-07)

Brings the September Windows installer (`installer/windows`, tag `v0.1.3`) up
to the current VMS. Customer-delivery scope: reliable installation, bundled
dependencies, auto-start service, firewall, VMS access from the local
network, cameras, live view, recording and playback.

## What changed

| Area | 0.1.3 | Now |
|---|---|---|
| VMS paths | launcher set `ANYAICAM_STATIC_FOLDER`/`RECORDINGS_FOLDER`, but the current VMS ignored them and hard-coded `/app/...` (→ `C:\app\...` on Windows) | `app/runtime_paths.py`: 25 call sites in 16 modules read the data/static roots from those variables; unset = byte-identical Linux defaults |
| Network | VMS bound to `127.0.0.1` only (phones could not reach it) | binds `0.0.0.0` (overridable `ANYAICAM_BIND_HOST`, `ANYAICAM_HTTP_PORT` in `vms.env`), same as the Linux appliance |
| Firewall | none (Windows blocks inbound) | `firewall.ps1`: TCP 8000 (python.exe) and UDP 8189 (mediamtx.exe), **Private/Domain profiles only**, from **LocalSubnet + Tailscale 100.64.0.0/10** only; removed on uninstall |
| Live view | no MediaMTX | MediaMTX 1.21.0 (same as Linux), checksum-pinned; the VMS starts it itself |
| Data folder ACL | ProgramData default (all users can read) | `icacls`: SYSTEM + Administrators only (holds app secrets, DB, recordings) |
| Upgrades | stale modules could linger | `[InstallDelete] {app}\app` before copying; data in ProgramData untouched; same data layout as 0.1.3 |
| Version | literal 0.1.3 / ec5272f | `build.ps1 -Version x.y.z` + the committed source commit; `ANYAICAM_VERSION` written to `vms.env` |
| Dependencies | 0.1.3 package set | `requirements-windows.txt` = exact freeze (108) of the runtime that ran the current VMS; top-level pins = Linux image pins; `setuptools` added (torch needs it on 3.12 — found by the offline install test) |
| Build inputs | existence checked | every vendor file SHA-256-checked; build refuses an uncommitted tree |

## Verified on the Dell (no system changes)

* Python 3.12.10 embeddable + get-pip match their pinned SHA-256s; WinSW,
  FFmpeg, MediaMTX match theirs (MediaMTX = the release's own checksum file).
* **Fully offline install**: a fresh embedded runtime installed every package
  from `vendor/wheels` with `--no-index`; all key modules import; `pip check`
  clean.
* **The current VMS runs natively**: started with the launcher's environment
  on `127.0.0.1:18080` from scratch folders — `/health` 200, `/version` 200,
  `/login` 200, static assets 200, `/ready` 503 (expected: unclaimed, no
  cameras); every file written under the configured data root.
* Tests: `installer/tests/test_windows_installer.py` (16),
  `app/tests/test_runtime_paths.py` (5), `installer/windows/test-installer-source.ps1`.

## Needs a clean Windows machine (Windows Sandbox) — not done on the Dell

Installing a service, firewall rules and ACLs would change the owner's
workstation (which also runs the old 0.1.3 service on port 8000). In Sandbox:

1. `powershell -File installer\windows\build.ps1 -Version 1.2.4` (unsigned test build).
2. Run the setup; confirm service `AnyAiCamVMS` Running + Automatic; reboot
   the Sandbox VM → it starts again.
3. From the host, open `http://<sandbox-ip>:8000`; confirm the two firewall
   rules exist with Private/Domain + LocalSubnet scope.
4. Add an RTSP camera (or a test stream), check live view (MediaMTX starts,
   UDP 8189), recording, playback.
5. Upgrade over itself; uninstall → rules and service removed, ProgramData kept.
6. Data-folder ACL: a standard user cannot read `C:\ProgramData\AnyAiCam\config\vms.env`.

## Not in this branch

* The appliance agent / cloud claim on Windows (the agent uses systemd and a
  root helper; needs a Windows service adapter — next slice).
* Authenticode signing (owner's Azure Artifact Signing sign-in; `build.ps1 -AzureSign`).
* Publishing an `.exe` through the customer download catalog (Ubuntu `.tar.gz` only today).
