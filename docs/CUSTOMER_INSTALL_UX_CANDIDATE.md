# Customer installer UX candidate — 2026-10-03

Scope: customer-facing presentation in `GET /subscription-portal` only. No backend, installer, pricing, billing, invitation, Software Update, LPR or device changes. No deployment or merge.

## Isolation

Branch: `codex/customer-install-ux-20261003`.
Base: `launch/overnight-20261002`, commit `9719adc8589a38fbd780400c045e4a255861eb68`.
The original detached August checkout does not contain this UI. The separate worktree uses the existing current installer/license baseline without integrating subsequent Software Update or billing/household candidates. The candidate diff is the review scope; inherited baseline changes are not part of this candidate.

## Implementation inspected

- `app/main.py`: `_customer_subscription_portal_page()` renders the real customer section at `/subscription-portal`; `_installer_steps_html()` supplies the technical setup instructions. No separate template file exists for this panel.
- `app/customer_downloads.py`: only the existing Ubuntu `.tar.gz` package naming pattern is accepted by `publish_vms_installer()`. The download endpoint still returns the stored bytes or an S3 signed redirect. This path does not serve a Windows executable.
- Build/version/checksum/size metadata comes from `downloads/vms-installer/latest.json` in private object storage. `publish_vms_installer()` computes SHA-256 from the package bytes and records the version and exact VMS commit supplied by the operator. The panel reads `latest_vms_installer()`; the candidate changes none of these sources.
- `installer/build_release_installer.py`: builds the release-specific archive. Installer internals are unchanged.
- `appliance-agent/anyaicam_agent/setup_wizard.py`: the existing `--claim` flow prints a code, waits for portal confirmation and completes activation. It does not turn customer sign-in into automatic account linking.
- `app/appliance_claims.py`: the signed-in account owner enters the code, selects a site and confirms at `/customer/claim-appliance`. Device possession, permissions, entitlements and retry-safe completion already exist and remain unchanged.

## Before / after rendered description

Before: a panel headed **VMS software license**, a technical installer/version row, hardware specifications, build ID and full SHA-256 checksum, a secondary Download installer link, and a visible list of checksum, tar, sudo install/validate and claim commands.

After, with a licensed owner and published package:

> **Install AnyAiCam**
>
> License active (one-time purchase) · Active
>
> Your AnyAiCam license includes up to 8 cameras.
>
> 1. **Step 1 — Download AnyAiCam**
>    **Download AnyAiCam** (primary button)
>    For Ubuntu 24.04 computers. This package requires a technician to install.
> 2. **Step 2 — Install AnyAiCam**
>    Ask your technician to install the downloaded package on your computer. Installation instructions are in Advanced / Technician setup below.
> 3. **Step 3 — Connect your system**
>    Your technician will give you a setup code. Sign in to your AnyAiCam account, connect your system, and enter the code to choose its location.
>
> Get setup help
>
> ▸ Advanced / Technician setup (collapsed)

Opening the native keyboard-accessible disclosure reveals all existing version, build, checksum, hardware/size, manual installation, validation and claim instructions. It starts closed without JavaScript. The original download and claim URLs remain intact. Existing no-license purchase controls and unreleased-download message remain intact. Viewers see their allowance but no owner-only download or commands. The existing Hybrid section stays in its original location.

## Recommended wording and future limitation

Use **Install AnyAiCam**, **Download AnyAiCam**, **Your AnyAiCam license includes up to N cameras**, and **Advanced / Technician setup** now. Keep the explicit Ubuntu/technician notice and setup-code wording until the delivered package and linking flow support a simpler experience. Do not label this download **Download for Windows**, promise a setup wizard, or promise automatic account linking.

Smallest future linking change: wrap the existing claim flow in the customer installer. Start/resume its device-bound claim session, open the customer portal sign-in with a short-lived handoff, and associate that handoff with the authenticated account owner. Reuse existing claim-confirm permission/entitlement checks, site selection (or an unambiguous default) and device-proof completion; keep expiry, possession validation and retry safety. This removes code copying and CLI entry without replacing the activation backend. The existing portal would need a secure handoff/prefill-and-confirm path; authentication alone is not sufficient evidence of device possession. A genuine customer-friendly launcher/package must also be published through the download path; this UI-only candidate does not implement either change.

Only after those capabilities are delivered, use: **Open the downloaded installer and follow the setup wizard** and **Sign in to AnyAiCam when prompted. Your system will link to your account automatically.**

## Verification

Local fixture-based commands used Python 3.14 with `PYTHONPATH` set to the isolated worktree's `app` directory. No live Stripe, storage, appliance or camera requests were used.

- Baseline: `python -m pytest -q app/tests/test_customer_downloads.py` — 17 passed.
- Candidate: `python -m pytest -q app/tests/test_customer_downloads.py` — 21 passed. Includes download bytes/authorization plus four new UI regressions: normal vs collapsed technical content, viewer restrictions, appliance-included capacity, and escaped release metadata.
- `python -m py_compile app/main.py` — passed. Existing invalid-escape SyntaxWarnings remain outside this change.
- `git diff --check` — passed.
- `python -m pytest -q app/tests/test_customer_subscription_portal.py` — 16 passed; verifies current-plan rendering, Hybrid availability, role isolation and staff/legacy behavior.

Full application suite and real installer execution were not run for this narrowly scoped presentation candidate. A rendered description is supplied above; no live customer page was accessed.
