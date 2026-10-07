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
