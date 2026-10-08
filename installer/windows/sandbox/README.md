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

## Interactive linking checks (2026-10-08, by hand)

The automatic run is silent, and a silent setup skips the finish page's
entries. In a fresh Sandbox, with a standard (non-administrator) user where
possible:

1. Run the setup by double-click (`/PortalUrl=https://cloud.anyaicam.test:8443`
   from a command prompt to stay on the test cloud). The last page shows **no
   claim code**, says "There is no code to type" and has *Link this computer
   to my AnyAiCam account* ticked.
2. Click Finish. The browser opens `/claim` **un-elevated** (Task Manager >
   Details > *Elevated* column = No) and the address bar ends at `/claim` or
   the sign-in page (the code is moved out of it).
3. Search `%TEMP%\Setup Log*.txt` (and the setup's `/LOG=` file, if any) for
   the code from `C:\ProgramData\AnyAiCam\label\claim-label.txt`: **no match**.
4. Start menu > AnyAiCam > *Link this computer to AnyAiCam*: a UAC prompt
   appears; Cancel = nothing opens and no code is shown; Yes = the browser
   opens the same page un-elevated, as the signed-in user, and the claim page
   finds the PC (the URL fragment survived explorer.exe).
5. After linking: the Start menu entry says "already linked"; a repair or
   reinstall's last page says "This computer is linked".
