# Clean-machine validation in Windows Sandbox

Windows Sandbox is a disposable Windows VM built into Windows 11 Pro; closing
it throws everything away, so installing the AnyAiCam service, firewall rules
and ACLs there never touches the host.

1. One time, as administrator (needs a reboot):
   `Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All`
2. Put the setup in `setup\` next to `AnyAiCam-Validation.wsb` and create an
   empty `results\` folder (make-wsb.ps1 does both and writes the .wsb with
   absolute paths).
3. Double-click `AnyAiCam-Validation.wsb`. The validation runs by itself at
   sign-in (about 10 minutes) and writes `results\report.txt`,
   `results\report.json` and the setup logs; `results\DONE` appears at the end.
4. Close the Sandbox window.

Cloud linking (2026-10-07) is checked against `test-cloud.py`, a disposable
cloud started inside the Sandbox (the installed app's real claim and appliance
routes, fresh database, HTTPS with a throwaway CA). The setup is run with
`/PortalUrl=https://cloud.anyaicam.test:8443`, and `app.anyaicam.com` is pointed
at the Sandbox itself, so the production cloud is never contacted.

Not covered automatically (needs a person / a camera): adding a real camera,
live view in a browser on another device, a recording and its playback, and a
reboot of the Sandbox VM.

## Upgrade from Windows 1.3.0 (2026-10-08)

`make-wsb.ps1 -Setup <new setup> -Folder <new folder> -Previous <AnyAiCam-VMS-Setup-1.3.0-aedecb6.exe>`
installs the earlier setup first and validates the new one as an upgrade over
it: 1.3.0's permanent claim code is removed and cannot link anything, and the
PC links with one-time links.

## Interactive linking checks (2026-10-08, by hand)

The automatic run is silent, and a silent setup asks for no link and skips
the finish page's entries. In a fresh Sandbox, with a standard
(non-administrator) user where possible, and the test cloud running:

1. Run the setup by double-click (`/PortalUrl=https://cloud.anyaicam.test:8443`
   from a command prompt). Near the end: "Preparing the link to your AnyAiCam
   account...". The last page shows **no code**, says "There is no code to
   type" and has *Link this computer to my AnyAiCam account* ticked.
2. Click Finish. The browser opens `/claim` **un-elevated** (Task Manager >
   Details > *Elevated* = No); the address bar no longer shows the code.
3. Search `%TEMP%\Setup Log*.txt` for `#code=` and for the 8-character code:
   **no match**.
4. Start menu > AnyAiCam > *Link this computer to AnyAiCam*: UAC appears;
   Cancel = nothing opens; Yes = the browser opens a new link un-elevated as
   the signed-in user; the earlier link no longer works.
5. After linking: the Start menu entry says "already linked"; a repair's last
   page says "This computer is linked". With the network cable out, the last
   page says to link later from the Start menu.
