# Customer installer/linking candidate — 2026-10-03

## Isolation and scope

Branch: `codex/customer-installer-handoff-20261003`, based on
`9719adc8589a38fbd780400c045e4a255861eb68` (`launch/overnight-20261002`).
Separate worktree: `.codex/worktrees/customer-installer-handoff/AnyAiCam-VMS`.

The candidate adds a fresh-install wrapper and browser presentation handoff. It
changes no Software Update, billing/Stripe, pricing, LPR, invitation, appliance
credentials, claim API, database schema or production infrastructure. No real
appliance, Ryzen, Samsung or production service was contacted. No merge or
deployment was performed. Existing installer internals and the agent claim state
machine are untouched. Only the packaging allowlist gains the two launcher files.

## Architecture and inspected implementation

1. The existing customer download route (`app/customer_downloads.py`) still
   serves the licensed owner's approved Ubuntu `.tar.gz`. Publishing/storage and
   metadata behavior are unchanged; currently published archives are not modified.
2. New `installer/setup.sh` invokes the standard-library Python
   `customer_setup.py` as the normal desktop user, so the browser uses that user's
   existing session. Root launches and non-Ubuntu-24.04 hosts are refused. Existing
   installation markers are refused to keep this path out of repair/update work.
3. The wrapper asks for the HTTPS portal origin and Local/Hybrid plan (or accepts
   explicit arguments for technicians), verifies `artifact-files.json` before
   privilege escalation, then invokes the unchanged `install.sh` and `validate.sh`
   through `sudo`, using argument arrays rather than a shell-built command. Failure
   of either prevents claim startup.
4. It starts the unchanged installed `anyaicam-setup --claim --portal-url=...`
   CLI as the `anyaicam` user. This already creates/resumes a device-bound claim,
   validates device possession, polls for confirmation/proof, redeems once and
   enrolls with retry-safe recovery. The wrapper only recognizes its printed
   eight-character setup code; it never reads the protected claim-state file,
   device secret, proof, or credential.
5. A random-port HTTP listener binds to `127.0.0.1` only. Its initial document
   contains no setup code. Code retrieval requires a 256-bit local nonce in a
   request header, exact Host and absent/same-loopback Origin. There is no CORS
   permission, HTTP access logging, file persistence or clipboard use. Responses
   use no-store, no-referrer and a restrictive CSP. Reads expire within 15 minutes
   (the existing server-side claim TTL remains authoritative) and setup termination
   shuts the listener down and clears its code/nonce.
6. The browser launch URL contains only a local bridge nonce in its fragment,
   never a claim code, claim session ID, proof or device secret. A fragment is not
   sent in HTTP. The page immediately removes it with `history.replaceState`.
7. Clicking Connect AnyAiCam opens the fixed `/customer/claim-appliance` path on
   the validated HTTPS portal origin. The unauthenticated page now goes to the
   existing customer login with that fixed return path; existing owner authentication
   and special login destinations still apply. No claim secret travels through login.
8. The authenticated page and loopback opener exchange a fresh challenge. Both
   sides check the exact window source; the local side checks the exact HTTPS
   portal origin and the portal side accepts only an IPv4-loopback opener origin.
   The code travels through `postMessage` with exact target origins. Receipt is
   acknowledged; retries do not trigger duplicate lookup. Manual field input is
   never overwritten.
9. Prefill invokes only the existing lookup button. The account owner must still
   review the device, choose a site and click Confirm claim. All existing role,
   self-link permission, customer/site ownership, CSRF, rate-limit, expiry,
   encrypted-proof and device-secret checks remain on the same APIs. The proof
   still goes exclusively to the possessing appliance, never the browser bridge.

## Customer experience and manual steps

After downloading and extracting a new package, the customer starts `setup.sh`
in a terminal, enters the sign-in website and purchased plan, and approves Ubuntu's
normal password prompt. The wrapper executes installation, validation and the
claim command without further command copying. The browser then handles owner
sign-in, device review, location selection and explicit confirmation. No manual
claim-code copying is required when the bridge works.

This does not promise a double-click GUI installer: extraction and launching the
script still require manual interaction and can need technician help. Browsers
that block the sign-in popup or isolate its opener can use the collapsed manual
code fallback. Setup interrupted after installation uses the existing technician
claim-resume path; this fresh-install wrapper will refuse a reinstall. No existing
published download gains this behavior until an approved artifact includes the
launcher, and the portal is separately running the handler. Neither was published
or deployed as part of this candidate.

## Security review

- No new activation or ownership authority is introduced. Possessing the setup
  code still cannot redeem a credential without the device's protected secret/proof.
- The portal handoff provides input only; it never signs the owner in, chooses an
  account/site silently, or confirms ownership. The existing confirmation checks
  remain the authorization boundary.
- Cross-origin/cross-window messages, malformed codes/challenges, prefill without
  handshake, repeated prefill and replacement of manual input are rejected.
- No claim secret is put in a remote URL, referrer, browser storage, clipboard or
  application/access log. The local nonce is a temporary loopback capability,
  distinct from any claim credential, and is removed from the visible URL on load.
- The bridge is not LAN-accessible. The nonce and exact Host check prevent ordinary
  local-port guessing and DNS rebinding from yielding a code. Malicious software
  already controlling the same desktop user can see the setup code (also true of
  the existing console flow); this does not give it the agent's private device secret.
- Package manifests detect missing/changed files, not a forged package with a
  rewritten manifest. The existing trusted release/download provenance remains
  required; no new signature claim is made. The original installer repeats its
  own payload verification.
- Authentication/CSRF middleware, APIs, proof encryption, expiry windows and
  durable agent resume state were not altered.

## Tests and results

All HTTP/activation tests use isolated fixtures. No sudo installer was executed.

- Baseline claim/possession suites: 49 passed.
- Baseline agent-claim/packaging suites: 49 passed, 1 skipped, 5 subtests passed.
- Candidate: `PYTHONPATH=app python -m pytest -q app/tests/test_appliance_claims.py
  app/tests/test_claim_device_possession.py app/tests/test_customer_claim_handoff.py
  app/tests/test_claim_flow_end_to_end.py` — 70 passed. Tests cover executable
  JavaScript origin/source/challenge gates, explicit confirmation, authentication,
  site isolation, device possession and existing activation/recovery regressions.
- Candidate: `PYTHONPATH=appliance-agent python -m pytest -q
  appliance-agent/tests/test_setup_wizard_claim.py installer/tests/test_build_release_installer.py
  installer/tests/test_customer_setup.py` — 71 passed, 1 skipped, 5 subtests passed.
  The skip is the existing POSIX extraction-permission test on Windows. New tests
  cover loopback nonce/Host/Origin gates, expiry/no-store, no URL claim code,
  integrity, unsafe/incomplete/malformed manifests, privilege boundary, refusal of
  existing installs, execution ordering/failures, packaging mode and interruption cleanup.
- Real Playwright browser fixture: actual loopback page + candidate portal handler,
  fixture HTTPS sign-in redirect and return — passed. Code prefilled automatically,
  lookup executed once, no automatic confirmation, local fragment scrubbed, no
  claim code in any request URL, no JavaScript errors. All non-fixture requests
  were blocked. This used dummy data, not a real customer login.
- Changed Python modules compiled successfully; `bash -n installer/setup.sh` and
  `git diff --check` passed.

Existing cookie/deprecation warnings remain. Full application suite and actual
Ubuntu privileged installation/desktop-launch behavior were not run on this
Windows development host. An approved fresh Ubuntu test environment is still
needed before release; none of the excluded devices should be used for that test.

## Compatibility with the customer UX candidate

Both candidates share base `9719adc8589a38fbd780400c045e4a255861eb68`.
The first candidate `ae27ae51029166525aae0b41887f8add53de3814` changes only
`app/main.py`, `app/tests/test_customer_downloads.py` and its own handoff doc.
This candidate does not modify those paths, so they can be combined without
textual overlap. Neither candidate is integrated here.

The first candidate's Ubuntu/technician wording remains truthful. After a new
artifact is built and validated, a follow-up presentation change can explain
Extract the download, start setup, then sign in and confirm your system. The
automatic-account-linking promise is still inappropriate because owner confirmation
and location selection remain intentional security steps.
