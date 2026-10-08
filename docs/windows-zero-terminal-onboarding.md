# Windows zero-terminal onboarding (2026-10-08)

Branch `feature/windows-zero-terminal-20261008`, from `aedecb6` (Windows cloud
linking, 53/53 in Windows Sandbox). Local only: not merged with the Linux
zero-terminal branch, not pushed, built, signed or published.

Approved decisions (owner, 2026-10-08): the unsigned Windows setup is not
promoted as a production-ready release; the Linux-style loopback setup page
stays disabled on Windows; all existing Windows and Linux work is preserved.

## Customer experience

1. Run the setup (administrator approval, as before). The last page shows
   **no claim code**: *Link this computer to my AnyAiCam account* is ticked
   and the text says there is no code to type.
2. Finish → the browser of the person who ran the setup (never an elevated
   browser) opens `https://app.anyaicam.com/claim#label=<code>&link=windows`.
   `/claim` moves the fragment into the tab and out of the address bar, then
   sign-in / account creation, then the claim page.
3. The owner chooses a site and confirms (unchanged: owner sign-in, own-site
   check, explicit confirmation). The AnyAiCamAgent service completes the link.
4. Later or again: *Start menu → AnyAiCam → Link this computer to AnyAiCam*.
   It asks for administrator approval (UAC), then opens the same page in the
   signed-in user's normal browser. Already linked → "already linked".
5. A repair / upgrade / reinstall of a linked PC says "This computer is linked"
   and offers no link.

**With today's cloud (`4ffbf35`)** step 3 still shows the code prefilled in
the claim page's input box and needs a *Look up* click: nothing is typed, but
the code is visible. Hiding it is the shared cloud change below.

## Changes

| File | Change |
|---|---|
| `installer/windows/source-integrity.ps1` (new) | Build integrity: every repository folder the .iss packages (read from its `Source: "..\..\<folder>\*"` lines -- `app`, `appliance-agent/anyaicam_agent`) and `installer/windows` must be exactly the commit: no change, staged change, deletion, untracked file, or git-ignored file in a packaged folder (the .iss packages whatever is on disk), except what the .iss excludes (`tests\*`, `__pycache__\*`). `installer/windows` is packaged file by file, so only its changes and untracked files count; `vendor/` there may hold only the pinned inputs and locked wheels. |
| `installer/windows/build.ps1` | Uses it (was `git status --porcelain -- app installer/windows`, which missed the agent and every ignored file). |
| `installer/windows/AnyAiCam-VMS.iss` | `[Run]`: both finish-page entries `runasoriginaluser`; the link is a `Parameters` value of a hidden PowerShell `Start-Process` with `dontlogparameters` (Inno logs every [Run] `Filename` -- the old entry wrote the code to `%TEMP%\Setup Log*.txt` on every interactive install; silent Sandbox runs skip postinstall entries, so the Sandbox scan could not see it). Link validated to `https://` + `/claim#label=` + `[A-Za-z0-9.:/#=-]` before it reaches the command line. Finish page: no code; linked PCs offer no link. `[Icons]`: *Link this computer to AnyAiCam* replaces *Show AnyAiCam claim code* (removed on upgrade by `[InstallDelete]`). |
| `installer/windows/open-claim-link.ps1` (new) | The Start menu entry: elevates first (UAC), only then reads the Administrators-only label file, and opens the link through `explorer.exe` so the browser runs un-elevated as the signed-in user. Writes and prints nothing. |
| `installer/windows/agent-launcher.ps1` | `ANYAICAM_LINK_PORT=0`: the Linux agent's loopback link page stays off on Windows after any future merge (agent.env cannot override it). |
| `installer/windows/sandbox/*` | New automatic checks (Start menu entries, link helper, agent has no listening socket, confirm through the setup's link) and a by-hand interactive section. |

`cloud-link.ps1`, the agent, the cloud (`app/`) and the claim protocol are
unchanged.

## Who can link (local threat model)

| Surface | Before (`aedecb6`) | Now |
|---|---|---|
| Finish page | showed the code to whoever sat at the setup | no code; link opens only for the person who ran the (admin-approved) setup |
| Setup log (`%TEMP%`, user-readable) | the claim link with the code | parameters withheld (`dontlogparameters`) |
| Start menu | "Show claim code" (Notepad, elevated) | "Link this computer" (elevated; code never displayed) |
| Local network/port | none | none -- the Linux loopback page is off (`ANYAICAM_LINK_PORT=0`) |
| Label file | SYSTEM/Administrators only | unchanged |

A standard user cannot obtain the code or start a link without an
administrator's UAC approval. On the Linux branch, by contrast, anyone signed
in to the appliance's desktop can link it -- acceptable for a single-purpose
appliance, not for a shared Windows PC (owner decision 2026-10-08).

Residual, documented:
* The Windows code is the permanent label code (valid until the PC is
  linked), not Linux's fresh per-click code. Someone who obtained it earlier
  (an administrator, a backup of ProgramData) could claim an unlinked PC.
  A fresh-code design needs the agent's `link_code()` (Linux branch) plus an
  Administrators-only local channel; revisit after the merge.
* The link is briefly on a process command line (the original user's hidden
  PowerShell; explorer.exe for the Start menu entry); readable by that user
  and administrators, and recorded only if the PC audits process command
  lines (Security log, Administrators-only).
* Over-the-shoulder UAC (a standard user types an admin's password): the Start
  menu entry then opens the browser as that administrator.
* Browser extensions in the owner's browser can read the tab, as on Linux.

## Shared cloud change -- for Green Claude (not made here)

The claim page lives in `app/appliance_claims.py`, which the Linux branch
(`feature/linux-zero-terminal-onboarding-20261008`, `035eaa1`) rewrote. To
avoid a conflicting second edit, the Windows branch changes **no** cloud code.
Requested on top of `035eaa1`:

1. **`/claim`**: when the fragment has `link=windows` next to `label=`, also
   set `sessionStorage['anyaicam.claimLabelLink']='windows'` (the existing
   label regex already accepts `label=XXXX-XXXX-XXXX&link=windows`; a cloud
   without this change keeps today's behaviour, so the setup works with both).
2. **Claim page, installer link mode** (`claimLabel` + `claimLabelLink`):
   keep the 12-character code in a JS variable only (never in the input),
   hide the code input and *Look up*, look the PC up at once, show
   "Only confirm if you just chose *Link this computer to my AnyAiCam
   account* on your own computer.", and on failure "This link has expired or
   was already used. On the computer: Start menu → AnyAiCam → Link this
   computer to AnyAiCam." In practice: let `linkCode` hold a 12-character label
   code too and have `claimBody()` send it as `label_code`.
3. Owner sign-in, site choice, explicit Confirm, the enrollment-readiness wait
   (`waitForAppliance`) and `?return=setup` → Discover cameras: unchanged
   (they already work for a label lookup).
4. Copy: the intro is label/hardware-oriented; add the Windows case.
5. Tests: `/claim#label=…&link=windows` → code hidden, auto-lookup, explicit
   confirm, readiness wait; plain `#label=` (printed QR) unchanged unless the
   owner decides printed labels should also auto-look-up.
6. Deploy with the Linux cloud part (blue/green, owner approval). Until then
   production behaves as described above (code visible in the input).

## Before the Windows and Linux branches are combined

1. `appliance-agent/tests/conftest.py` (add/add, the only textual conflict):
   keep both autouse fixtures.
2. Add a code-level Windows guard in `service.start_link_server()` (return
   before binding when `windows.IS_WINDOWS`), with a test, in addition to
   `ANYAICAM_LINK_PORT=0`.
3. Full agent, app and installer suites; Windows Sandbox run (the automatic
   check "the agent opens no listening port" covers 2).

## Tests

* `installer/tests/test_windows_zero_terminal.py` (27): build integrity in a
  throwaway git repository with the real .iss (edited / untracked / ignored /
  staged / deleted packaged files fail; excluded and pinned files pass;
  folders come from the .iss; unknown Excludes stop the build; extra vendor
  files fail); finish-page entries run as the original user, link never a
  logged `Filename`, `dontlogparameters`; no code on the finish page or Start
  menu; linked PC offers no link; link character check; the real .iss compiles
  (`ISCC /O-`, nothing written); `open-claim-link.ps1` with the real
  `cloud-link.ps1` output, refusal of malformed links, link state, elevation
  before reading, un-elevated browser, no output; `ANYAICAM_LINK_PORT=0`.
* `installer/tests/test_windows_installer.py`: claim code never shown or logged.
