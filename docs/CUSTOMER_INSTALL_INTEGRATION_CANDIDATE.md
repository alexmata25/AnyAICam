# Customer installation integration candidate

## Scope and base

Branch: codex/customer-install-integration-20261003.
Base: 9719adc8589a38fbd780400c045e4a255861eb68.
Cherry-picked ae27ae51029166525aae0b41887f8add53de3814 and
90fa29721eb634a01672aa8e02eb1e143c0b7c3a, without conflicts.

No merge, deploy, push, publication, appliance connection or installation was
performed. Software Update, Billing/Stripe, Household, LPR, Ryzen, Samsung,
pricing and production infrastructure are unchanged. Tests use isolated databases,
temporary package storage, locally generated HTTPS certificates and loopback
servers; browser routes reject external destinations.

## Integrated architecture and rendered experience

The subscription page retains the included camera count and prominent Download
AnyAiCam button. It explicitly identifies Ubuntu 24.04 and technician assistance.
Step 2 directs the technician to start setup from the extracted package.
Step 3 describes owner sign-in, location selection and confirmation when the
launcher opens the browser, with setup-code recovery if it does not. The collapsed
Advanced / Technician setup retains build/checksum/manual instructions and explains
the new launcher. Older published packages remain supported; no capability is
inferred from their metadata. Hybrid stays in its existing section.

Newly built Ubuntu packages include setup.sh and customer_setup.py. The launcher
verifies its file manifest, refuses an existing installation/root/unsupported OS,
delegates the existing privileged installer and validator, and starts the existing
device-bound agent claim CLI as the service user. The service retains device
possession secrets, claim proofs and credentials. Only the expiring setup code
goes through the RAM-only, nonce-gated loopback bridge into the owner-authenticated
portal popup. The customer must choose a location and click Confirm claim.
The original claim API and enrollment behavior are unchanged.

The customer still needs a technician to extract/start the Ubuntu archive, enter
the HTTPS portal address and Local/Hybrid plan, and answer the Ubuntu privilege
prompt. This is not a Windows setup wizard. A post-install interruption uses the
existing technician claim CLI rather than rerunning the fresh-install launcher.

## Validation and limits

Focused regression: 212 passing tests, 1 skipped POSIX permission check on
Windows, and 5 passing subtests:

- 120: subscription/install page, download authorization/bytes/metadata/private
  storage, claim API, owner/site authorization, possession, encrypted proof and
  credential recovery, replay/expiration, real agent HTTP enrollment, login CSRF.
- 71 plus 1 skip and 5 subtests: existing agent claim CLI, deterministic installer
  packaging and customer_setup bridge/delegation/failure/manual recovery.
- 10: new combined tests â€” two real Chromium flows and eight claim lookup/confirm
  CSRF/origin refusal cases.
- 11: Windows offline payload validation and local Inno compilation with inert
  fixture adapters. No compiled fixture payload was installed or executed.

The combined Chromium test uses the actual main app over a temporary HTTPS
loopback server, actual customer password sign-in and session cookies, real
security middleware, the actual owner claim page/JavaScript and claim APIs, and
an actual loopback ClaimBridge. It downloads and extracts a representative package
containing real launcher bytes, verifies it and exercises launcher delegation with
install/validate subprocesses mocked. The agent claim start/finish is driven by
real APIs in the test; the separate agent HTTP tests cover actual agent enrollment.
The browser proves prefill/lookup leaves the claim pending, then chooses a site,
confirms and completes the device-bound exchange. A second scenario manually
enters the same kind of setup code and completes the claim. No code or possession
secret appears in captured browser request URLs.

Actual Ubuntu OS installation, real customer network/proxy headers, installer
dependencies and native Windows installation were NOT tested. Browser COOP,
popup restrictions or different browser policies can require manual recovery;
protections were not relaxed to accommodate them.

An additional legacy CSRF/logout source-test group ran 12 tests: 1 passed,
11 failed. Re-running those checks against exact files from the unchanged base
reproduced the same 11 failing test IDs (6 assertions and 5 absent-helper errors).
They expect auth_form_script/csrf_validation/logout helpers not present on this
baseline. No global authentication/logout rewrite was introduced. Current login
middleware tests and the new actual claim-route CSRF/origin checks pass.
Therefore this is a validated scoped candidate, not an entirely green repository.

Python compilation, bash setup.sh syntax, PowerShell build-script parsing and
git diff --check also passed. Existing cookie-deprecation and Python escape
warnings remain.

A local build from the integrated candidate also produced an Ubuntu installer
archive and verified all extracted manifest hashes, setup.sh inclusion, LF-only
shell content and executable tar modes. This smoke build deliberately used
--no-mediamtx and was deleted with its temporary directory; it was not a
customer release and was never installed or published.

## Test commands

Using local Python 3.14 with pytest 9.1.1, Node.js and installed Chromium:

    PYTHONPATH=app python -m pytest -q app/tests/test_customer_downloads.py app/tests/test_customer_subscription_portal.py app/tests/test_appliance_claims.py app/tests/test_claim_device_possession.py app/tests/test_customer_claim_handoff.py app/tests/test_claim_flow_end_to_end.py app/tests/test_login_csrf.py app/tests/test_login_csrf_http_integration.py
    PYTHONPATH=app python -m pytest -q app/tests/test_customer_install_integration.py app/tests/test_customer_downloads.py
    PYTHONPATH=appliance-agent python -m pytest -q appliance-agent/tests/test_setup_wizard_claim.py installer/tests/test_build_release_installer.py installer/tests/test_customer_setup.py
    python -m pytest -q installer/tests/test_windows_candidate.py

The second command rechecks the same 21 download tests, counted once in totals.
The older source checks were tests/test_csrf_client_normalization.py and
tests/test_logout_csrf_flow.py; baseline verification redirected their
MAIN_SOURCE/SECURITY_SOURCE constants to git-show exports of the pinned base.

## Security review

No ownership, authorization, possession, CSRF or token/encryption API logic was
changed by these candidates. The handoff checks exact portal origin, exact child
window/opener and a challenge; the local HTTP service checks exact Host, nonce and
expiry, never listens on the LAN and enables no CORS. The portal never automatically
confirms. Setup code stays out of URLs/clipboard/storage; authenticated claim API
POSTs keep the existing CSRF middleware. Claim status/complete still require the
device secret; proofs and retry credentials remain encrypted at rest and bounded
by existing expiry. Wrong owners/sites, bad secrets, expired/replayed proofs and
unconfigured encryption remain covered by inherited regressions.

Package checksums detect corruption relative to a local manifest; they are not a
signature or protection against an attacker replacing both code and manifest.
The existing trusted-download/checksum provenance remains important.

## Windows work remaining

The offline scaffold is installer/windows-candidate, independently tested and
compiled with inert fixture adapters. It stages only reviewed caller-supplied
Windows payload files with a pinned commit, complete checksum manifest and no
network access. It refuses missing native runtime/connection adapters and Ubuntu
scripts masquerading as EXEs. The Inno wizard gates the unelevated connection
launcher on successful privileged runtime installation.

Missing: real Windows fresh-install/runtime/service/identity adapter, protected
agent state and authenticated local IPC, native desktop connection launcher,
pinned offline dependencies and licenses, rollback/uninstall behavior, clean VM
installation/reboot/security/media tests, then signing. The existing Windows
installer branch was inspected but not imported; it installs a VMS service but
does not supply this secure account-linking architecture.

See installer/windows-candidate/README.md for adapter and build contracts.
Estimate for a first useful testable EXE: 5â€“10 engineering days, assuming the core
VMS and media dependencies run on Windows. Existing service work may shorten this
after separate review; OS portability issues can expand it. Customer distribution
takes additional validation/signing. The download publisher currently rejects
EXEs, and accepting/distributing a Windows artifact remains separate future work.

## Exact files changed from the pinned base

1. app/main.py
2. app/appliance_claims.py
3. app/tests/test_appliance_claims.py
4. app/tests/test_customer_claim_handoff.py
5. app/tests/test_customer_downloads.py
6. app/tests/test_customer_install_integration.py
7. docs/CUSTOMER_INSTALL_UX_CANDIDATE.md
8. docs/CUSTOMER_INSTALLER_HANDOFF_CANDIDATE.md
9. docs/CUSTOMER_INSTALL_INTEGRATION_CANDIDATE.md
10. installer/README.md
11. installer/build_release_installer.py
12. installer/customer_setup.py
13. installer/setup.sh
14. installer/tests/test_customer_setup.py
15. installer/tests/test_windows_candidate.py
16. installer/windows-candidate/README.md
17. installer/windows-candidate/stage_payload.py
18. installer/windows-candidate/build.ps1
19. installer/windows-candidate/AnyAiCam-test.iss

## Recommended wording

Current: "Install AnyAiCam"; "Your AnyAiCam license includes up to 8 cameras";
"Download AnyAiCam"; "For Ubuntu 24.04 computers. This package requires a technician
to install"; "Sign in as the account owner, choose a location, and confirm your
system." Mention setup code only as recovery.

After a native installer passes clean-machine validation: "Download for Windows";
"Open the downloaded installer and follow the setup wizard"; "Sign in to AnyAiCam,
choose a location, and confirm your system." Do not promise automatic ownership
confirmation.
