# Samsung appliance — checkpoint

**Read `docs/PROJECT_CHECKPOINT.md` first.** This file assumes that context.

## Do not restart Samsung setup from scratch

Samsung is **waiting at the cloud identity/activation stage** — it is not a
clean/unconfigured device, and it must not be treated as one. Every future
session must read this file, verify current state, and resume from the next
action below — never re-run the installer's clean-install path, never
re-image, never re-run camera discovery from zero, unless Samsung's actual
current state (checked live) turns out to genuinely be `clean` per the
runbook's own `detect_install_state` check.

This session (2026-09-11, reconciliation) did **not** touch Samsung in any
way — no SSH, no commands, nothing. Nothing here has changed since the last
session that actually worked on Samsung.

## What is documented and proven so far

- `docs/samsung-installation-runbook.md` is the step-by-step install/repair
  procedure for Samsung specifically. As of the runbook's own header, every
  command in it had been validated twice against the same installer package
  on disposable EC2 instances (`docs/phase1-edge-validation-report.md`,
  `docs/phase1-privileged-watcher-e2e-validation-report.md`) — **not**
  against Samsung itself at the time it was written.
- The claim/activation flow it depends on (`first_enroll`,
  `coordinated_reenroll`, `_finish_enrollment` in
  `appliance-agent/anyaicam_agent/setup_wizard.py`) is the
  `staging/cloud-integration-repair` implementation, carried into the golden
  branch (`reconcile/golden-foundation-20260911`) in this session's
  reconciliation work.
- A prior real incident on Samsung is on record: a suspend/hibernate bug
  took down LAN, SSH, and Tailscale simultaneously by putting the whole
  machine to sleep. `disable_system_suspend()` was the fix for that,
  referenced in the runbook's §0 step 2. Confirm this is still in effect
  before relying on remote access to Samsung.
- Samsung's `talk_isapi_diagnostic` setting must stay enabled throughout any
  work — this has been an explicit standing constraint across sessions, not
  something to toggle for a test.

## Current real-world state (per direct instruction this session)

Alejandro has stated directly that Samsung is currently sitting at the cloud
identity/activation stage of setup — i.e., past initial install/discovery,
not yet claimed/activated against the cloud (`anyaicam-staging` or
production, whichever is intended for Samsung). **This checkpoint has not
independently re-verified that against live Samsung console/SSH access this
session** (Samsung was explicitly off-limits this session) — the next
session that actually resumes Samsung work must confirm this first via the
runbook's own §0/§2 detection steps before proceeding, per this project's
standing rule of verifying a checkpoint against live state rather than
trusting it blindly.

## Exact next action

1. **Verify, don't assume.** Before doing anything else: confirm
   Tailscale + LAN SSH connectivity (runbook §0 steps 2-3), confirm the
   currently-running image (`docker inspect anyaicam-vms --format
   '{{.Image}}'`), and run `detect_install_state` (runbook §2) to confirm
   Samsung is actually still at `existing`/partial-activation and not
   something else.
2. **Back up before touching anything** — runbook §0 step 1
   (`partner_portal.db` backup, copy of `docker-compose.yml` +
   `vms.env` off-box, note the current image) — even though Samsung is
   mid-setup, it may already have real customer/camera state worth
   preserving.
3. Resume from wherever the claim/activation step actually is — do not
   re-run install from §1 unless step 1 above proves a clean/partial state
   that genuinely requires it.
4. Once the golden build (`docs/PROJECT_CHECKPOINT.md` → Authoritative
   branch) has an actual versioned artifact, Samsung should be brought up
   on **that** artifact through the normal installer/update mechanism —
   never hand-patched to resemble Ryzen or staging. If Samsung hits a bug
   that Ryzen already hit and fixed, check whether the golden release
   actually contains that fix before re-fixing anything on Samsung directly.
5. Update this file immediately after any session that changes Samsung's
   real state, before ending that session.
