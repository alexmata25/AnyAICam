# Windows zero-terminal onboarding (2026-10-08)

Two local branches, neither pushed nor merged into a release branch:

| Branch | What |
|---|---|
| `feature/windows-zero-terminal-20261008` (`b53bc12`, from `aedecb6`) | Windows-only fixes: full build integrity, no elevated browser, no visible / logged claim code, Administrators-only relink. Still uses 1.3.0's permanent label code. |
| `feature/windows-linux-integration-20261008` (this document) | the above + Green Claude's Linux zero-terminal branch (`035eaa1`, merged unchanged) + the Windows completion: **one-time links instead of the permanent code**, the Windows guard in the agent, and the claim page's Windows link mode. The Windows 1.3.0 successor is built from here. |

Owner decisions (2026-10-08): keep the Linux-style loopback setup page
disabled on Windows; continue Windows 1.3.0 (the public 1.3.0-aedecb6 download
stays as it is); preserve all existing Windows and Linux work.

## Customer experience

1. Run the setup (administrator approval, as before). At the end it asks the
   AnyAiCam Cloud Agent for a one-time link ("Preparing the link to your
   AnyAiCam account..."). The last page shows **no code**; *Link this
   computer to my AnyAiCam account* is ticked.
2. Finish → the browser of the person who ran the setup (never elevated)
   opens `https://app.anyaicam.com/claim#code=XXXXXXXX&link=windows`.
3. Sign in or create an account. The claim page finds the PC by itself
   (code hidden), says "Your Windows computer (ID ending …)", the owner
   chooses a site and **confirms** (explicit click, own sites only).
4. The agent completes the link; the page waits until the PC has enrolled
   and checked in, then offers **Discover cameras**.
5. Later or again: *Start menu → AnyAiCam → Link this computer to AnyAiCam*
   (UAC: an administrator), which opens a new one-time link the same way. A
   linked PC says so; a PC that is offline says so.

## The permanent claim code is retired

Windows 1.3.0 created a 12-character code at install that stayed valid until
the PC was linked (shown on the finish page and by a Start menu entry; kept
in `label\claim-label.txt`). Anyone who learned it could claim an unlinked PC
for their own account.

Now:
* **No code at rest.** The setup creates none; an upgrade from 1.3.0 deletes
  `label\claim-label.txt` and `config\label_claim.json` (`cloud-link.ps1`).
* **No standing claim.** The agent runs `HeadlessClaim(on_demand=True)` on
  Windows: it opens a claim only when an administrator asks for a link, and
  **without a label verifier**, so the cloud can match nothing but the code
  in that link.
* **One-time codes.** Every request resumes the same claim with the agent's
  device secret and gets a fresh 8-character code; the previous code stops
  working at once (cloud `claim/begin`). A claim lasts 15 minutes and is not
  reopened by itself after it expires.
* A label claim left open by 1.3.0 is resumed without its label on the next
  request and expires within 15 minutes; the old code can no longer be
  found once it has.

## Who can link (local threat model)

| Surface | 1.3.0 (`aedecb6`) | Successor |
|---|---|---|
| Finish page | showed the permanent code | no code; a one-time link opens for the person who ran the (admin-approved) setup |
| Setup log (`%TEMP%`, user-readable) | the claim link with the code ([Run] Filename) | nothing: the link is a `dontlogparameters` parameter; the setup's request uses `Exec` (no output logged) |
| Start menu | "Show claim code" (elevated Notepad) | "Link this computer" (UAC; the code is never displayed) |
| Local web page / port | none | none: `ANYAICAM_LINK_PORT=0` **and** `start_link_server()` returns before binding on Windows whatever the variable says |
| Link requests | — | `C:\ProgramData\AnyAiCam\agent\link-requests`, SYSTEM/Administrators-only (inherited); a standard user can neither ask nor read an answer |
| Code at rest | `label\claim-label.txt` (permanent) | none; an answer file holds a one-time code until the requester reads and deletes it (deleted by the agent after 2 minutes otherwise) |

How the link reaches a non-elevated browser:
* **Setup:** `open-claim-link.ps1 -Prepare` (elevated, from the setup) writes
  `<status>\n<link>` into the setup's own `{tmp}` with `CreateNew`; the setup
  reads and deletes it, validates the link (`https://…/claim#code=[0-9A-Z]{8}&link=windows`
  and a character allow-list) and the finish-page entry opens it with
  `runasoriginaluser`.
* **Start menu:** the script, as the signed-in user, starts a copy of itself
  with UAC and waits; the elevated copy requests the link and writes it into
  that user's `%TEMP%\anyaicam-link-<random>.txt` with `CreateNew`; the first
  copy reads, deletes, validates and opens it as itself. If an administrator
  approved with *their* account (over-the-shoulder UAC) the hand-back file is
  in their profile and the customer is told to sign in as the administrator.

Residual, documented:
* The one-time link is briefly on a process command line (the original
  user's hidden PowerShell); visible to that user and administrators, and
  recorded only where process command-line auditing is enabled.
* Browser extensions in the owner's browser can read the tab, as on Linux.
* The person who runs the setup gets the link: the setup itself already
  needs an administrator's approval.

## Changes on the integration branch (beyond `b53bc12` and `035eaa1`)

| File | Change |
|---|---|
| `appliance-agent/tests/conftest.py` | the only merge conflict (add/add): both autouse fixtures kept |
| `appliance-agent/anyaicam_agent/headless_claim.py` | `on_demand` mode (no label, no standing claim); Linux unchanged |
| `appliance-agent/anyaicam_agent/windows_link.py` (new) | `LinkRequests`: the Administrators-only request folder (nonce names, plain files only, 2-minute limits, atomic answers, waits up to 20 s for the claim right after the service starts, never logs a code) |
| `appliance-agent/anyaicam_agent/service.py` | Windows: on-demand factory, `start_link_requests()`, and `start_link_server()` returns `None` before binding |
| `app/appliance_claims.py` | `/claim` keeps `link=windows`; the claim page's link mode uses Windows words (computer, Start menu) -- **for Green Claude's review** |
| `installer/windows/cloud-link.ps1` | no claim code; retires 1.3.0's on upgrade |
| `installer/windows/open-claim-link.ps1` | requests one-time links (Start menu / elevated copy / `-Prepare`) |
| `installer/windows/AnyAiCam-VMS.iss` | `RequestAccountLink` at the end of interactive installs, `IsAccountLink`, finish page (linked / link ready / link later), no `label` folder |
| `installer/windows/sandbox/*` | one-time link checks, upgrade mode (`make-wsb.ps1 -Previous <1.3.0 setup>`) |

## Cloud dependency -- for Green Claude

The successor's links need the zero-terminal claim page (`#code=` link mode,
enrollment wait) from `035eaa1`, plus the small Windows wording change above,
deployed to the AnyAiCam cloud (blue/green, owner approval). Against today's
cloud (`4ffbf35`) a `#code=` link lands on the manual claim page with an
empty code box, so **the successor must not ship before that deploy.** The
installed 1.3.0-aedecb6 keeps working with today's cloud (label code).

Please review in `app/appliance_claims.py` on this branch:
1. `/claim`: `link=windows` next to `code=` → `sessionStorage['anyaicam.claimLinkSource']='windows'`.
2. Claim page: `fromWindows` only changes words ("Finding your computer…",
   "Your Windows computer (ID ending …)", the confirm warning, and the
   expired-link advice pointing to the Start menu entry). Same hidden code,
   auto-lookup, explicit confirm and readiness wait as Linux.
3. The intro still mentions "the last page of the Windows installer" (true
   for installed 1.3.0-aedecb6 PCs); keep or reword.

## Tests

* `appliance-agent/tests/test_windows_link_requests.py` (22): on-demand
  eligibility, no standing claim, no label sent, fresh code per request,
  completion, expiry not reopened, 1.3.0 claim resumed; link requests (link
  only in the fragment, linked, startup wait, unavailable, https only,
  malformed / old / non-file requests ignored, unread answers deleted, code
  never logged, thread); service wiring (no LinkServer on Windows even with
  `ANYAICAM_LINK_PORT=8790`, link requests on Windows only, Linux label claim).
* `app/tests/test_windows_zero_terminal_e2e.py` (5): real cloud routes under
  uvicorn + real agent: link with no code and no label, new request kills the
  old code, expired claim not reopened, 1.3.0 label claim resumed and linked,
  another account's site refused.
* `installer/tests/test_windows_zero_terminal.py`: build integrity (from
  `b53bc12`), finish page / Start menu / log rules, setup link request,
  `open-claim-link.ps1` against a stand-in agent (fresh link, malformed links
  refused, linked, timeout, one-time hand-back files), Inno compile.
* `installer/tests/test_windows_cloud_link.py`: no code created; upgrade
  retires 1.3.0's code.
