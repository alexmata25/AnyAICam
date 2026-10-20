# Windows 1.3.2: Local mode with no cloud link runs 0 cameras (documented; proposal for review)

Status: **documentation and proposal only.** No entitlement or licensing enforcement is changed by the Windows 1.3.2
branch (owner instruction 2026-10-19: no licensing change without a separate review).

## Behaviour today (verified in code at `claude/blue-windows-132`)

| Situation on a Windows PC | Cameras that run | Why |
|---|---|---|
| Fresh install, Local or Hybrid, **never linked** to an AnyAiCam account (online or offline) | **0** | `vms_capacity.licensed_camera_numbers` returns `[]` when `load_capacity()` is `None` (`app/vms_capacity.py`, "capacity is None: return []"); `camera_url` enforces it in `main.py`. |
| Upgrade from the published Windows 1.3.0 | **0 until the PC is linked** and the first signed capacity arrives | 1.3.0 kept an unsigned capacity in `C:\opt\anyaicam\data\config\vms_capacity.json`, a folder any signed-in user can modify, so `install-runtime.ps1` deliberately does not migrate it. Setup prints this. |
| Linked once, then offline indefinitely | the last signed capacity, indefinitely | an accepted snapshot is not re-checked for expiry (Red's D3 question; unchanged). |

What still works with 0 licensed cameras: the VMS service, its web page, sign-in and settings. No camera records, streams
or runs analytics. The setup's Local page says "cameras, recording and live view stay on this computer", which is
true for **where** the video is kept, but a Local install still needs **one** online account link.

## Customer-facing consequence (needs wording before release, owner decision)

The Windows download page, the setup's Local option and the Local plan description must say:
"Local mode keeps your video on this computer. Link the computer to your AnyAiCam account once (internet needed for
a few minutes) to activate your cameras; after that it keeps working offline."
Until that wording is approved, the 1.3.2 setup must not be described as supporting fully offline (air-gapped) use.

## Proposal for legitimate offline Local licences (for Codex and owner review; NOT implemented)

Recommended: **option B** of `docs/local-offline-licensing-options-2026-10-14.md`, an **offline licence file**,
reusing the signed-capacity path that Windows already has, so no second trust path exists.

1. **Issue (cloud, owner-approved later):** the account owner signs in on any other device and downloads a capacity
   certificate for a specific PC. It is the same signed snapshot format the cloud already sends
   (`anyaicam.vms_capacity`, signed by the entitlement keyset whose public half the 1.3.2 setup pins,
   SHA-256 `2389a655…af7c`), bound to that PC's appliance identity (shown on the VMS's local page and in
   `%ProgramData%\AnyAiCam\config\appliance_identity.json`), with an explicit `expires_at` and a camera cap.
2. **Import (Windows):** an administrator copies the file into an Administrators-only drop folder
   (`%ProgramData%\AnyAiCam\update\import\`), or uses an elevated Start-menu entry "Import AnyAiCam licence file".
   The VMS hands it to the existing `capacity_accept_windows` helper, the same code that accepts a cloud snapshot:
   keyset signature, identity binding, issued/expiry window and never-older replay rules all apply unchanged
   (`appliance-agent/system/capacity_accept.py`).
3. **Revocation:** none is possible offline, so certificates must carry an expiry (for example 12 months) and are
   re-issued on renewal; a PC that comes online later receives the cloud's current snapshot, which replaces the file
   if newer.
4. **What it does not do:** no unsigned starter capacity (option D) and no per-customer installer (option C). Both
   would weaken enforcement or break the single, checksummed release artifact.

Tests to write once approved: a valid imported file licenses exactly N cameras; tampered, foreign-key, wrong-identity,
expired, future-dated and replayed files are refused with their exact codes (the same matrix as the Sandbox acceptance
`acceptance-127.ps1` synthetic runs); a standard user cannot write the import folder; a later cloud snapshot supersedes
the file.

Related: Red's permanent-Local design (`anyaicam-red\licensing-permanent-local-20261018\DESIGN.md`, D-1..D-6) covers what
a linked PC keeps after a subscription ends; this proposal covers a PC that is never linked. Both need Codex review
and owner decisions before any enforcement code changes.
