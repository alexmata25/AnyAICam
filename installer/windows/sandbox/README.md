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

## Functional scenarios F-L / F-H (2026-10-21, Blue) -- SYNTHETIC-KEYSET / SIMULATED CAMERA

`make-wsb.ps1 -Functional -KeysetKind synthetic-test -SyntheticSigningKey <key> -ProductMode local|hybrid ...`
prepares a run of `functional-in-sandbox.ps1` instead of `validate-in-sandbox.ps1` (without `-Functional`
nothing changes). F-L installs Local, F-H installs Hybrid; each writes its own report. Every report is labelled
**SYNTHETIC-KEYSET / SIMULATED CAMERA**: a pass is evidence for the product logic on a real Windows install, and
**never** production-keyset acceptance or physical-camera acceptance.

What a run does, all inside the disposable Sandbox with networking disabled:
install; the test cloud with `--functional`; a never-linked PC refuses customer sign-in; an invalid, an expired and
a used link code are refused, a fresh one links; the cloud-signed capacity (synthetic key) is accepted; online
customer sign-in, a wrong password, the session on a protected API, the offline appliance password; a simulated
camera is discovered by a cloud scan job, added through the real provisioning routes, verified by the agent and
synced to the VMS; live view (customer playlist, segment, still; the operator pages' `/static/hls` URL); recording
(MKV); basic motion events; premium analytics off and LPR/PPE unavailable on Windows; Local -> Hybrid -> Local
(F-L) or Hybrid -> Local -> Hybrid (F-H) through the cloud and `restart_vms`; with the cloud stopped: offline
sign-in, wrong-password refusal, live view, recording and playback of a finished segment, then the offline-grace
expiry; back online; the appliance revoked; no secret in any collected file.

How it differs from a real customer PC and a real camera (the exact limits):

| Area | In the functional run | Real acceptance still needs |
|---|---|---|
| Build | the TEST-ONLY synthetic-keyset setup (same source, different public keyset), TEST-signed | the production-keyset, production-signed setup (capacity then comes from the real cloud key) |
| Camera | `onvif-camera-sim.py` (ONVIF SOAP, unicast WS-Discovery) + FFmpeg test pattern published by a separate MediaMTX, H.264 640x360 10 fps, no audio, no credentials | a physical ONVIF camera: real codecs/profiles, credentials, audio, H.265, network loss, PoE, reconnect |
| Address | loopback 127.0.0.5; the agent gets `discovery_networks: ["127.0.0.4/30"]` in agent.json because it never scans loopback by default and manual add-by-IP refuses loopback (both by design) | automatic discovery on the LAN (multicast through the Windows firewall, CAM-10), manual add of a LAN address, a second subnet (G2) |
| Cloud | the in-Sandbox test cloud: the real claim/appliance/customer-camera routes on a fresh SQLite database, a synthetic owner (`/test/owner`), test-only routes for claim expiry, revocation and entitlement status | the real cloud, real accounts, billing-driven entitlements, cloud recording upload and live relay |
| Mode switch | the synthetic entitlement rows are changed directly (`/test/entitlement`, `/test/entitlement-status`) | a real plan change (Stripe TEST/LIVE) driving the same configuration route |
| Offline | the test cloud process is stopped (connection refused); the 72 h grace is simulated by moving `last_verified_at` back | a real internet outage of hours/days, DNS failure, captive portal |
| Live view | HLS through the customer routes; WebRTC is NOT tested (its signalling runs through the cloud) | WebRTC/P2P in a browser on another device, remote live view |
| Browser | HTTP requests (`edge-http.py`); no page is rendered | the pages in Edge/Chrome/Safari, by a person |
| Motion | basic pixel motion on a moving box; premium analytics only checked OFF / unavailable | Smart Motion, people counting, rules on real scenes; a physical driveway for LPR (not on Windows) |
| Duration | about an hour; one 5-minute segment | days of recording, retention and disk-full behaviour, reboot, sleep, Windows Update |
