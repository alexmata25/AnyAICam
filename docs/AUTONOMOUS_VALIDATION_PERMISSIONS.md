# AnyAiCam autonomous validation -- permission model

Established 2026-09-15, alongside the `e2e/` Playwright browser-test
foundation. This is a policy document, not a checkpoint snapshot --
update it when the *rules* change, not when a session merely makes
progress within them (see `docs/PROJECT_CHECKPOINT.md` for that).

## Why this exists

A 2026-09-15 read-only capability audit (recorded in
`docs/PROJECT_CHECKPOINT.md`) found that this environment already has
far more reach than a human copy/paste loop implies: `aws ssm send-
command` executes arbitrary shell commands **as root** on both the
staging and the production EC2 instances, with no separate approval
step per command. That capability makes an explicit, written scope
boundary a precondition for any autonomous loop -- not an optional
nicety -- because the technical ability to act and the authorization to
act are no longer the same gate.

## The three environments and what's autonomous in each

### Staging — autonomous inspect / fix / build / deploy / retest

`https://portal-staging.anyaicam.com` (`portal-green` /
`deploy-portal:<commit>` on EC2 instance `i-0a082abd812929bb4`), and the
`e2e/` browser-test suite's default target.

Once the setup in this document's own "Still required" section is
complete, an autonomous loop **may**, without asking first each time:

- Read source, logs, container state, and the running database (staging
  only) to diagnose a failure.
- Write and run unit tests (`app/tests/`) and e2e tests (`e2e/`).
- Edit source, commit to `reconcile/golden-foundation-20260911`.
- Build a new staging image and redeploy `portal-green`/`portal-blue`
  (the existing blue/green pattern `PROJECT_CHECKPOINT.md` already
  documents), restart the container, and re-run the e2e suite against it.
- Repeat that cycle unattended.

It may **not**, ever, without separate explicit authorization: touch
Cameras 2–5's recording-upload scope, weaken IAM or the broad recording
Deny, touch Ryzen or Samsung, touch AWS resources outside the one
staging EC2 instance + its own S3/IAM already in scope, or create a
second real (non-test) tenant on staging.

### Ryzen — autonomous testing/deployment only after separately enabled

The physical edge appliance (`ryzen-tailscale`, currently a
non-privileged SSH key with no `sudo`). **Not in scope for any
autonomous loop today.** Before it ever is, this document needs a new,
explicitly-authorized section describing exactly what's allowed there
(at minimum: which containers may be recreated, whether camera
discovery/recording may ever be touched automatically, and how a
privileged action -- still never handled directly per this project's
standing rule -- fits into an otherwise-unattended loop). Until that
section exists, any Ryzen change stays a human-initiated, one-at-a-time
action, exactly as today.

### Production — inspection allowed when specifically authorized; modification always needs a human

`https://app.anyaicam.com` (real production domain, confirmed live via
SSM during the 2026-09-15 audit -- **not** `portal.anyaicam.com`, the
stale placeholder in `deploy/.env.production.example`), EC2 instance
`i-0f0fb6a78871b20d4` ("Anyaicam2026", `ANYAICAM_ENV=production`,
container `anyaicam-vms`). Real customer traffic, ~6.7 days uptime at
audit time.

- **Inspection** (reading logs, health endpoints, running a read-only
  `e2e/` pass against it) is allowed **only when a human has separately
  authorized that specific pass** -- not as a standing default, and not
  implied by staging authorization. `e2e/conftest.py`'s own safety rail
  enforces the technical half of this: it hard-fails at startup unless
  `ANYAICAM_E2E_ALLOW_PRODUCTION=true` is explicitly set, which is never
  the default and never set by an autonomous process itself.
- **Any modification or deployment** -- code change, container
  recreate, config change, anything that writes -- always requires
  explicit human approval, every time, no standing exception. This
  mirrors the project's own established phased-authorization discipline
  (`docs/AI_HANDOFF.md`: "every phase of work... expected to get its
  own explicit authorization") applied specifically to production.

## What "autonomous" means here, precisely

An autonomous loop operating under this document may run its own
inspect → code → test → build → deploy → restart → re-test → diagnose →
fix cycle **within the staging boundary above** without a human in the
loop for each individual step. It still stops and asks before:
broadening scope (new AWS resources, a second real tenant, anything
outside staging), any Ryzen action, any production inspection without
that pass's own specific go-ahead, and any production modification at
all.

## Still required before the first autonomous end-to-end loop can run

1. A dedicated staging e2e test-tenant (separate from the real pilot
   customer) with its own low-privilege login, referenced by
   `e2e/.env` locally -- see `e2e/README.md`.
2. One human-driven authenticated pass through `e2e/tests/` to replace
   the remaining `pytest.skip(...)` TODOs with real, DOM-verified
   assertions.
3. AWS credential hygiene: the CLI in this environment currently
   authenticates as the AWS account's own root user
   (`arn:aws:iam::880690594006:root`), found during the 2026-09-15
   audit. Move to a scoped IAM role/user before any loop regularly and
   unattended exercises AWS on staging's behalf.
4. A concrete decision on *where* the autonomous loop itself runs (this
   session, a scheduled job, CI) and how it's told to stop/report,
   rather than running indefinitely with no checkpoint.

Until all four are in place, the pieces this document describes are
real and tested individually (SSM access, `e2e/`'s own framework, the
staging deploy pattern already used by hand for months) but not yet
wired into one unattended loop.
