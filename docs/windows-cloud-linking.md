# AnyAiCam for Windows — cloud linking (2026-10-07)

Builds on the native Windows installer (`docs/windows-native-installer.md`,
`18dff14`). A Windows install was a local-network VMS only; it can now be
linked to the customer's AnyAiCam account like the Linux appliance, using the
same appliance agent and the same label-claim flow (`docs/headless-label-claim.md`).

## Customer flow

1. Run the setup. Its last page shows this computer's **claim code**
   (`XXXX-XXXX-XXXX`) and, ticked by default, *Link this computer to my
   AnyAiCam account*, which opens `https://app.anyaicam.com/claim#label=<code>`.
   The code sits in the URL fragment, so it never reaches a server; `/claim` moves
   it into the tab and out of the address bar.
2. The customer signs in (or creates an account), picks a site and confirms.
3. The **AnyAiCam Cloud Agent** service redeems the confirmation, saves its
   credential, writes the VMS's cloud identity and `ANYAICAM_CLOUD_URL`, and
   restarts the VMS. Heartbeats, camera status and the portal's commands follow.

Shown again later: *Start menu → AnyAiCam → Show AnyAiCam claim code* (opens the
Administrators-only file elevated). The code survives repair, upgrade and
uninstall/reinstall, so a code already shown stays valid.

## What the installer adds

| Piece | Where | Notes |
|---|---|---|
| Agent package | `{app}\agent\anyaicam_agent` (+ `agent-main.py`) | the same `appliance-agent` code as Linux, on the bundled Python 3.12 |
| `AnyAiCamAgent` service | `{app}\service\AnyAiCamAgent.exe` (WinSW) + `agent-launcher.ps1` | LocalSystem, Automatic, restart on failure; **no** dependency on `AnyAiCamVMS` (restarting the VMS on request would stop it) |
| `cloud-link.ps1` | run by `install-runtime.ps1` every install | `config\appliance_identity.json` (UUIDv4, once), `label\claim-label.txt` (code, once), `config\label_claim.json` (verifier only), `config\agent.env` (portal + production mode), `config\vms_release.json` |
| Agent preflight | `install-runtime.ps1` | the bundled Python must import the agent before any service exists |

Folders (all under the SYSTEM/Administrators-only `C:\ProgramData\AnyAiCam`):
`config` is shared with the VMS (`vms.env`), the agent's state is `agent\`, its
log `logs\agent.log`. Setup option `/PortalUrl=https://…` (https, not local) picks
another cloud; otherwise the saved one, else `https://app.anyaicam.com`.

## Agent on Windows (`appliance-agent/anyaicam_agent/windows.py`)

Linux behaviour is unchanged; call sites branch only when `IS_WINDOWS`.

* **Remote commands (owner decision 2026-10-07: restarts only).**
  `restart_vms` → `AnyAiCamVMS.exe restart`; `restart_service`/`restart_agent`
  → `AnyAiCamAgent.exe restart!` (WinSW self-restart). **Refused:**
  `reboot_appliance` (it is the customer's own PC) and `install_update`
  (Windows updates come as a new signed setup). The WinSW paths come from the
  bundled interpreter's location, never from the environment.
* Never on Windows: the Linux release check/apply path, the root privileged
  watcher, the LAN-address file (the VMS runs natively, so MediaMTX sees the
  PC's addresses itself), WireGuard (off everywhere).
* Metrics from the Win32 API; camera discovery scans each private IPv4 /24 of
  the PC and reads `arp -a`.
* After a claim the running agent picks up its credential itself, so
  `_finish_enrollment` does not restart it (that would race the VMS update).

## Tests

* `appliance-agent/tests/test_windows_platform.py` (16): allowlist and refusals,
  command dispatch, no updater/LAN paths, finish-enrollment on Windows, ARP /
  network / service parsing, heartbeat metrics. `tests/conftest.py` keeps every
  other agent test on the Linux behaviour whatever host runs it.
* `installer/tests/test_windows_cloud_link.py` (6): runs the real
  `cloud-link.ps1`; code format and randomness, verifier = the cloud's formula,
  the agent's own eligibility check passes, reinstall keeps everything, portal
  validation, an unusable identity is never replaced.
* `installer/tests/test_windows_installer.py`: service order, launcher paths,
  preflight placement, claim code never logged.
* Windows Sandbox (`installer/windows/sandbox`): `test-cloud.py` serves the
  installed app's **real** claim and appliance routes on a fresh database over
  HTTPS (throwaway CA) inside the Sandbox; `app.anyaicam.com` is pointed at the
  VM so production can never be reached. The validation claims the PC end to
  end, checks heartbeat, the refused and allowed commands, link kept across
  reinstall/repair/uninstall, and that no code or credential is in any log.

## Not in this slice

* The label-claim cloud change (`2ceb084`) must be deployed to the AnyAiCam
  cloud before a Windows PC can be claimed against production.
* `vms-windows.html` / download page copy for the claim step.
* Signing and publication (unchanged release gates).
