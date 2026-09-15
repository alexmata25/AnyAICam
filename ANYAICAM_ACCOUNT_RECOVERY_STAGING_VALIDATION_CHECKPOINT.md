# AnyAiCam Account Recovery — Staging Validation Checkpoint

**Checkpoint date:** 2026-09-15
**Purpose:** records the Claude-side validation and staging deployment of the account-recovery/password-reset feature Codex handed off at `49da185`. This document is the authoritative record that the feature is live on `anyaicam-staging` and has been proven end to end through the real public website. It does not authorize a Ryzen release.

## Source

- **Repository:** `alexmata25/AnyAICam`
- **Branch:** `reconcile/golden-foundation-20260911`
- **Source SHA (unchanged from Codex handoff):** `49da18516a021d83fee05c2c7797d086e5e17568`
- No source defect was found; no source fix was required. `app/cloud_security.py` and `app/cloud_features.py` are byte-identical to Codex's `49da185`.

## Tests run

- Pre-existing suite (`test_account_recovery.py` + `test_password_reset_link_host.py`): **11 passed**, reproducing Codex's own result independently.
- New route-level tenant-scope tests added this pass (`app/tests/test_account_recovery_route_tenant_scope.py`, 9 tests, committed alongside this checkpoint): same-tenant unlock success, password never mutated, cross-tenant denial with zero mutation, cross-tenant denial indistinguishable from unknown-customer 404, wrong customer_id/real user_id denial, unauthenticated denial with zero mutation, missing-permission denial, no mutation of unrelated customer fields, audit record written with no password/token in it. **9 passed.**
- Broader authentication/tenant-isolation/security regression set (13 files: camera tenant scoping, camera multi-appliance isolation, cloud customer auth routing, dashboard camera tenant scoping, final tenant-isolation re-audit, live-view customer auth, multi-tenant security remediation, partner-administrator tenant-scope follow-up, playback-thumbnail authorization, portal-login delegated auth, shared Smart-Motion media authorization, Stripe checkout authoritative identity): **187 passed, 5 failed.** All 5 failures were independently reproduced against the pre-account-recovery baseline (`1355f6c`, in an isolated detached worktree) and fail identically there — they are pre-existing, environment-dependent failures (an appliance-activation fixture gap surfaced by this Python 3.14/Windows test environment) unrelated to this feature. Zero regressions attributable to the account-recovery change.

## Staging deployment

- **Pre-deploy DB backup:** `/var/lib/anyaicam-staging/db/staging-pre-account-recovery-deploy-20260915T015833Z.db`, SHA-256 `bcd633707310b7b0440087c03d8e383485ed21f4715e2dbe3492932d1adc387b`, `PRAGMA integrity_check: ok`, 70 tables.
- **Pre-deploy source backup:** `/opt/anyaicam-staging-source-backup-pre-account-recovery-deploy-20260915T015833Z.tar.gz`, SHA-256 `c067a4d8ca1e247b13d0d1424c885d3dc009a6f66d9b3f796b53b291cdeae1a8`.
- **Source transfer:** GitHub's immutable archive for commit `49da185` (`https://github.com/alexmata25/AnyAICam/archive/49da18516a021d83fee05c2c7797d086e5e17568.tar.gz`), SHA-256 `ad3f67d69cda09c56ac6bb016b96034a7722958a9a862ce8ace2924c5dc05337` — verified identical between an independent local download and the instance's own download before extraction. `rsync --delete`'d into `/opt/anyaicam-staging/app/`; `Dockerfile`/`requirements.txt`/`requirements-cpu.txt` copied. `diff -rq` against the verified archive: exact match. `deploy/` and `storefront/` confirmed untouched (mtimes unchanged).
- **Image built:** `deploy-portal:49da185`.
- **Cutover:** `portal-green` started alongside the still-live `portal-blue`, identical mounts (`/var/lib/anyaicam-staging/{db,recordings,hls,data-config}`), identical env-file (`/etc/anyaicam-staging/vms-staging.env`, unchanged), same `deploy_default` network. Health-checked internally through Caddy's own network with the real `Host: portal-staging.anyaicam.com` header (`/health` → `200`, `/forgot-password` rendered, `/partner/customer-accounts` correctly redirected unauthenticated) before any traffic moved. Cutover via Caddy's admin API (`GET /config/` → `dial` `portal:8000` replaced with `portal-green:8000` → `POST /load`) — zero-downtime, no gap in public `/health`.
- **Old container preserved (stopped, renamed, not deleted):** `anyaicam-staging-portal-pre-account-recovery-deploy-20260915T015833Z-rollback` (image `deploy-portal:e99f223`).
- **Post-deploy DB integrity:** `ok`; row counts unchanged from pre-deploy (`customers`=3, `appliances`=3, `cameras`=22, `sites`=3, `partner_users`=4, `partners`=1, `recordings`=5).
- **Not touched:** Ryzen, Samsung, `deploy/`, `storefront/`, AWS security controls, recording-role IAM, `RECORDING_UPLOAD_ENABLED`, rollback assets from prior passes. PR #15 remains open/unmerged.

## Live validation through the real public website

All of the following were driven against `https://portal-staging.anyaicam.com` after cutover, using two clearly-labeled, disposable synthetic tenants (`DISPOSABLE-TEST-partner-a`/`-b`, one `customer_owner` account each, one `technician` operator under partner-a) inserted directly for this purpose and fully deleted afterward — chosen because staging has no second real tenant and its email backend cannot deliver to a real inbox (see limitation below), so a real customer's account was never touched.

1. **Anti-enumeration (real, unknown addresses):** two different unregistered emails through the real `/forgot-password` → `/api/password-reset/request` flow (real CSRF cookie handshake) returned byte-identical generic responses and wrote zero rows.
2. **Real lockout:** 5 real failed logins through `/api/partner-login` against the disposable customer locked the account; the *correct* password was then also rejected with `429` while locked, proving a genuine lockout, not just a bad-password rejection.
3. **Forgot Password → real link:** the real `/forgot-password` flow was run for the disposable account; the resulting message was retrieved from staging's own preview email store (see limitation below) and contained a real, working `/reset-password?token=...` link.
4. **Reset link works:** `GET /reset-password?token=...` rendered `200`; `POST /api/password-reset/complete` with a new password returned `200` / "Password updated."
5. **Old password fails, new password works:** post-reset, the old password returned `403` "incorrect" (not `429` — proving the lockout was actually cleared, not just outlasted); the new password returned `303` (successful login redirect).
6. **Admin/Partner Unlock UI, live:** logged in as the disposable operator through the real website; `GET /partner/customer-accounts` (`200`) and its backing `GET /api/partner/customer-accounts` listed **only** the operator's own tenant's account (correctly `"locked": true` after a second real 5-failure lockout), never the other tenant's.
7. **Same-tenant unlock, live:** `POST /api/partner/customers/{own-customer}/accounts/{own-user}/unlock` as the operator returned `200`, lockout cleared; a subsequent real login with the current password succeeded (`303`).
8. **Cross-tenant denial, live:** the same operator's identical unlock call against the *other* tenant's customer/account returned `404` "Customer not found" (indistinguishable from an unknown id, per this codebase's established no-oracle convention) — verified zero mutation of the other tenant's state.
9. **Cleanup:** all disposable rows (`partners`, `customers`, `partner_users`, `account_lockouts`, `user_sessions`, `password_reset_tokens`) and the one preview email file created for this test were deleted. Post-cleanup row counts confirmed back at the exact pre-test baseline (`customers`=3, `partner_users`=4, `partners`=1). `audit_logs` entries from this validation were deliberately left intact as a record, per this project's own established practice of never rewriting historical evidence.

### Known, reported limitation — email delivery

`ANYAICAM_EMAIL_BACKEND=preview` on staging, with no `AWS_REGION`/SES environment configured. This means password-reset messages are written to a local JSON preview store (`ANYAICAM_EMAIL_PREVIEW_DIR`, mounted at `/app/recordings/email-preview`) rather than delivered to a real inbox — this has been true for every one of staging's 18 prior password-reset messages, not something newly introduced by this deploy. **Real inbox delivery was not, and could not be, validated on staging as currently configured.** Every other part of the flow (link generation, token validity, password change, lockout clearing, subsequent login) was proven for real through the actual reset link the preview backend produced. Configuring a real email provider for staging is separate, explicitly-not-yet-authorized infrastructure work.

## State to resume from

Account Recovery (Codex source `49da185`, unmodified) is deployed to `anyaicam-staging`'s live `portal-green` container and has been proven end-to-end through the real public website: Forgot Password, anti-enumeration, real lockout, real reset link (via the preview store, not a real inbox — see limitation above), password change, lockout clearing, login with the new password, the Admin/Partner Unlock UI, same-tenant unlock, and cross-tenant denial. Ryzen remains `48992a5b91c`, unchanged. Samsung unchanged. PR #15 remains open/unmerged.

Per the Codex handoff's own next step: **AUTHORITATIVE SOURCE → IDENTIFY APPLIANCE-RELEVANT DELTA → VERSIONED RYZEN RELEASE → RYZEN FIVE-CAMERA REAL-HARDWARE VALIDATION** remains separately, explicitly not yet authorized.
