# AnyAiCam Account Recovery — Claude Handoff

- **Authoritative worktree:** `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-VMS-reconciliation`
- **Branch:** `reconcile/golden-foundation-20260911`
- **Starting SHA:** `06082cce3a1eebf3b1dd3fe42bf8f857db740e05`
- **Final Codex SHA:** pending this handoff commit.
- **Staging deployment:** not performed.

## Implemented source work

- Lockout policy remains five failed attempts and fifteen minutes.
- An expired lockout now starts a new failure window; its retained count cannot immediately re-lock the account.
- Successful login continues to clear failed-login state.
- Successful password reset atomically claims the single-use token, updates the password, invalidates other active reset tokens, clears the account lockout, and revokes existing sessions.
- Customer recovery requests remain generic for enumeration resistance and are bounded to three requests per email per fifteen minutes plus thirty per source IP per fifteen minutes.
- Customer reset links use the customer reset page for customer accounts on edge deployments.
- Tenant-scoped Partner/Admin API: `POST /api/partner/customers/{customer_id}/accounts/{user_id}/unlock` clears only the selected customer user lockout and audits the action. It authorizes the customer in the same DB transaction before deletion and never reads/changes password, plans, cameras, sites, appliances, subscriptions, or permissions.
- Operator page: `/partner/customer-accounts` lists only authorized customer accounts and provides a confirmation-based Unlock account action.

## Files changed

- `app/cloud_security.py`
- `app/cloud_features.py`
- `app/tests/test_account_recovery.py`
- `app/tests/test_password_reset_link_host.py`

## Tests

Ran: `pytest -q tests/test_account_recovery.py tests/test_password_reset_link_host.py` — **11 passed**.

The new test proves: five failures lock; expired lock then one failure is count one and not locked; successful reset clears lockout, rejects old password, accepts new password, revokes a session, and rejects token reuse.

Not run: route-level same-tenant/cross-tenant/unauthenticated unlock tests; broader tenant-isolation/auth regression suites; staging deployment and website validation.

## Known limits and required Claude work

- Email delivery is not validated. Staging’s configured backend must be checked without exposing configuration secrets. Do not fake delivery.
- The browser/backend stale-lockout discrepancy remains unreproduced: live staging had no lockout row or corresponding failure/blocked audit event while the browser showed the message. Public portal response was dynamic, not cached. Recheck after deployment with route/status/audit correlation.
- Add actual route-level tests for same-tenant success, cross-tenant denial, unauthenticated denial, password preservation, and no unrelated customer mutation before staging deploy.
- Review the minimal operator page styling/navigation integration if product polish is needed; do not alter unrelated portal pages.

## Exact staging deployment procedure

1. Confirm active portal health, source/image, Caddy upstream, SSM, TCP 22 absence, recording-role denial, and effective recording upload false.
2. Preserve a timestamped copy of `/var/lib/anyaicam-staging/db/staging.db` and the current `/opt/anyaicam-staging/app` source.
3. Archive the committed application source with `git -c core.autocrlf=false archive`; transfer and SHA-256 verify it; update only the staging app source. Keep `/etc/anyaicam-staging/vms-staging.env` unchanged.
4. Build a versioned `deploy-portal:<commit>` image. Start a parallel portal container with the same mounts, env file, command, and `deploy_default` network, but a distinct name/alias. Verify `/health` through the required trusted Host header before traffic cutover.
5. Change only Caddy’s active upstream through its existing admin configuration path. Retain the old container as rollback until validation completes.
6. Verify HTTPS, customer login page, reset request generic response, CSRF, health, appliance heartbeat, Live Relay/signing assumptions, recording-role denials, and no public TCP 22.

## Staging validation checklist

- Use normal website only; do not inspect passwords, tokens, email credentials, or database hashes.
- Verify unknown and known recovery requests return the same generic response.
- Validate actual configured email delivery only when safely available.
- Reset the designated customer through the received link, then prove old password fails and new password succeeds through the normal login page.
- Verify reset clears an existing temporary lockout.
- Verify operator unlock with a same-tenant authorized account and verify tenant denial with an authorized fixture only if separately available/approved.
- Validate audit records contain no passwords or tokens.

## Preservation

- Ryzen remains `48992a5`; do not deploy this cloud-only change to Ryzen by default.
- Samsung unchanged.
- PR #15 remains open/unmerged.
- Do not touch AWS remediation, recording roles, Live Relay, CloudFront, or rollback assets.

After Account Recovery passes staging, next task:

**AUTHORITATIVE SOURCE → IDENTIFY APPLIANCE-RELEVANT DELTA → VERSIONED RYZEN RELEASE → RYZEN FIVE-CAMERA REAL-HARDWARE VALIDATION**