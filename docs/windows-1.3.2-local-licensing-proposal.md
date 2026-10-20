# Windows 1.3.2: Local mode with no cloud link runs 0 cameras (documented; proposal for review)

Status: **documentation and proposal only.** No entitlement or licensing enforcement is changed by the Windows 1.3.2
branch (owner instruction 2026-10-19: no licensing change without a separate review).

## Investigation 2026-10-20 (Codex FAIL on ef93b503, priority 3): does the approved architecture already allow a secure offline first activation?

**No.** Verified in code at `claude/blue-windows-132`. Nothing was changed in licensing.

1. **Every signed capacity is bound to the activation identity.**
   - `vms_capacity._accepted_signed` and `capacity_accept_windows` accept an `anyaicam.vms_capacity` snapshot only when its `appliance_id`, `cloud_id` and `customer_id` equal `appliance_entitlements._local_identity()` (`BOUND_IDENTITY_FIELDS`).
   - That identity comes from `appliance_activation.load_persisted_identity()`. It requires `appliance_id`, `cloud_id`, **`credential`**, `customer_id`, `site_id`, `partner_id`, `activated_at` and `activation_version` (`_REQUIRED_FIELDS`), and anything incomplete fails closed.
2. **Only the online claim creates that identity.** The cloud issues the appliance `credential` and binds the PC to a customer and site when an administrator confirms the one-time link. A never-linked PC holds only the setup's local `appliance_id` (`cloud-link.ps1`). So no capacity, however validly signed, can be accepted before the first link. This is deliberate: the binding is what stops one licence being copied to many PCs.
3. **After the first link, Local is genuinely offline-capable.** The accepted capacity is re-verified with `check_expiry=False`, so the licensed cameras keep running without internet indefinitely (tests `test_vms_capacity_acceptance.py::…remains_offline_after_expiry`, `test_windows_capacity_accept.py::…keeps_last_accepted_capacity`).
   - Premium analytics need a signed grant refreshed at least every 168 h (ae23d024).
   - Talk Down and Voice Call need the 24 h snapshot.
4. **No approved alternative exists.** `docs/local-offline-licensing-options-2026-10-14.md` is still "decision needed (owner)", and Red's permanent-Local design (D-1..D-6) covers a *linked* PC after a lapse, not first activation.

**Supported requirement, now stated to customers by the 1.3.2 setup** (`AnyAiCam-VMS.iss` mode page and finish page, and the install-runtime log; tests `installer/tests/test_windows_local_activation_wording_132.py`):
- both modes need the PC linked to an AnyAiCam account **once, online**, before cameras start;
- after that, Local cameras, recording and live view keep working without internet.

The tests forbid any offline-activation, "fully offline", "air-gapped" or "without an account" promise. They also fail if the capacity code stops behaving as the wording says (`check_expiry=False`; no capacity → no cameras).

**Website and download-page wording (Green / owner, not changed by Blue):** "Local mode keeps your video on this computer. Link the computer to your AnyAiCam account once (an internet connection is needed for a few minutes) to activate your cameras; after that, cameras, recording and live view keep working without internet." Do not describe 1.3.2 as supporting air-gapped installation.

### Offline first activation: design for OWNER APPROVAL (product-policy and security change; NOT implemented)

The only secure shape that reuses the approved trust model is an **offline claim**: the cloud performs the same claim it performs online, and the result is carried to the PC by file instead of over the network. It still needs the cloud once (from any device), so it is "offline for the PC", not "no account".

1. **Request (PC, offline).**
   - An administrator runs "Create an AnyAiCam activation request" (Start menu, elevated).
   - It writes `activation-request.json`: the setup's `appliance_id`, a fresh one-time public key generated on the PC (private half kept in `%ProgramData%\AnyAiCam\agent`, SYSTEM/Administrators only), the setup version and a nonce.
   - No secret leaves the PC.
2. **Claim (cloud, any online device).**
   - The account owner signs in and uploads the request, chooses the site, and the cloud runs the **existing claim checks** (licence capacity, Option B rules, one appliance per licence seat).
   - It returns `activation-response.json`: the appliance credential encrypted to the request's public key, plus the normal signed capacity snapshot bound to the new identity.
   - The request is single-use: the nonce and the appliance_id are consumed.
3. **Import (PC, offline).**
   - The agent verifies the snapshot with the pinned entitlement keyset (`2389a655…`).
   - It decrypts the credential with the private key that never left the PC.
   - It writes the activation identity, then hands the snapshot to the existing `capacity_accept_windows`, so there is no second acceptance path.
   - A response for another request, a reused response or one with a foreign signature is refused.
4. **What changes and needs a decision:**
   - (a) a new cloud claim endpoint and owner-facing page (Red/Codex: cloud, Option B contract);
   - (b) the agent import step (Blue: Windows, and Red for Linux parity);
   - (c) the policy itself: a seat is consumed by a PC the cloud has never contacted, and revocation reaches it only when it next comes online;
   - (d) whether premium analytics may also be granted offline (today they need a 168 h refresh).
5. **Explicitly rejected:**
   - an unsigned starter capacity;
   - per-customer installers;
   - a bypass flag;
   - any locally created identity without a cloud-issued credential.

   Each would let one licence run unlimited PCs.

**Decision needed from the owner:** keep the one-time online link as the supported rule for 1.3.2 (recommended; it is what is now implemented and worded), and decide whether to schedule the offline claim above for a later release.

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

## Premium analytics offline (interaction with the shared premium-default-OFF work, 2026-10-19)

Red's in-progress shared implementation (`claude/red-127-analytics-default-off`, not yet committed) runs a premium
analytic on an edge only with a valid, **unexpired** per-camera grant in the signed feature-entitlement snapshot. That
snapshot's TTL is 24 h by default (at most 168 h). So on a Windows PC:

- **never linked** (Local, offline): no snapshot, so no premium analytics. Basic motion, recording, live view and
  playback are unaffected. This matches the owner rule.
- **linked, then offline longer than the TTL:** premium analytics stop. Cameras keep recording at the last signed
  capacity, which is not re-checked for expiry. Codex/owner: is that the intended "authorized offline use" for premium
  analytics? An offline licence file (option B above) could carry a per-camera analytics grant with a longer, explicit
  expiry, verified by the same keyset.
- **On Windows the snapshot is in a protected folder** (`%ProgramData%\AnyAiCam\config\feature_entitlements.json`,
  SYSTEM/Administrators only, Blue `e1e79e5b`). Before, it was `C:\opt\...`, where a standard user could delete it
  (premium analytics off) or put back an older, still-valid snapshot.

Related: Red's permanent-Local design (`anyaicam-red\licensing-permanent-local-20261018\DESIGN.md`, D-1..D-6) covers what
a linked PC keeps after a subscription ends; this proposal covers a PC that is never linked. Both need Codex review
and owner decisions before any enforcement code changes.
