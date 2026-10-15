# Cloud compat for VMS 1.2.6 customer sign-in: deploy and rollback runbook

Status: **prepared, not deployed.** Deploying needs explicit owner approval.

| | |
|---|---|
| Branch | `claude/cloud-126-customer-login-compat` |
| Candidate | `b55fc49` (parent `92b0e32`, which `https://app.anyaicam.com/version` reports as live on 2026-10-14) |
| Change | 4 source files, +18/−2, no schema change, plus 2 test files |

## Why

A VMS 1.2.6 appliance (`local_customer_access.py`) forwards a customer's sign-in to `POST /api/appliance/{cloud_id}/authenticate-operator` with `portal: "customer"`. Cloud `92b0e32` has no `customer` portal bucket. A correct owner password therefore gets `not_authorized_for_selected_portal`, which the appliance shows as "Only the account owner can sign in on the appliance itself…".

This was reproduced on the Ryzen (`AIC-C814766E`) after the RC2 install. No customer can sign in on, enroll an offline password on, or then sign in offline to a 1.2.6 appliance until this is deployed.

## What changes (cloud runtime only)

| File | Change | Source |
|---|---|---|
| `app/appliance_identity.py` | `PORTAL_BUCKETS["customer"] = {"customer_owner"}`. Owner only; the operator buckets are unchanged. | `e47e271` |
| `app/appliance_cloud.py` | `identity-manifest` and `authenticate-operator` answer **404** "This appliance is not activated." for an unknown or de-activated appliance, instead of a 500. Otherwise the appliance treats the 500 as an outage and allows offline sign-in. | `5dd3a20` |
| `app/cloud_security.py` | A password **reset** bumps `partner_users.authorization_version`. | `e47e271` |
| `app/partner_portal.py` | A password **change** (`/api/partner/activate-account`) bumps it too. Nothing else in 92b0e32's change flow changes. | `5dd3a20` |

### Effect of the version bump

It only affects that user's **appliance** sessions and offline appliance password. Both are revoked when the appliance next applies the signed identity manifest. The cloud never compares `authorization_version` for its own sessions. Invitations, household access, registration revocation and grant changes already bump it the same way.

## Compatibility review

- **Signing keys:** no key code changed. `appliance_identity.py` in `b55fc49` differs from `92b0e32` only by the 3-line bucket, so assertion and manifest formats and the `identity_signing_keys` table are unchanged. Live key `anyaicam-identity-49cfda63a27f` is the only key in every 1.2.5/1.2.6 installer keyset (`2389a655…`).
- **Appliance identities:** `appliances`, `appliance_credentials` and the activation flow are untouched.
- **Existing sessions:** `user_sessions` is untouched, and the blue/green swap keeps the same DB mount. No session is invalidated by the deploy itself.
- **Database:** no migration. 92b0e32 and b55fc49 read and write the same schema, so rollback needs no DB step.
- **Other 1.2.6 appliance calls:** every cloud endpoint the 66a0012 VMS and agent call (41 paths) exists in 92b0e32. The Ryzen on 1.2.6 already syncs, heartbeats and accepted signed capacity 8 against it. Customer sign-in is the only incompatibility.
- **Older appliances (1.2.4/1.2.5):** they never send `portal: "customer"`. The 404 change only alters an error that used to be a 500.

## Test evidence (isolated containers, `--network none`)

- **New tests:** `test_cloud_customer_portal_compat.py` and `test_password_change_bumps_authorization_version.py`. 4 of them fail on 92b0e32 (customer assertion, 404 mapping, reset bump, change bump) and all pass on b55fc49. The refusal tests pass on both.
- **Auth regression on b55fc49:** identity integration, delegated portal login, admin lockout/password change, reset lifecycle, account recovery, reset hardening, bootstrap admin grant, cloud administrator bridge, onboarding identity grants, grant management/privilege, logout, admin-initiated reset. Result: **196 passed, 1 skipped.**
- **Cross-version over HTTP:** a cloud container (b55fc49 routes, seeded DB) and an edge container (66a0012 `local_customer_access`):
  - owner: OK (online, `customer_owner`);
  - viewer, technician on the customer portal, wrong password: all refused;
  - manifest verified and reconciled.
  - The same harness against 92b0e32 reproduces the live refusal.
- **Full app suite, base vs candidate:** see the branch report.

## Preconditions (stop if any fails)

1. Owner approval for this deploy, through the SSO permission set `AnyAiCamCloudDeploy` (profile `anyaicam-deploy`), never root. All commands run over SSM on the single cloud host `i-0a082abd812929bb4`, which serves `app.anyaicam.com` and `portal-staging`.
2. **The live container really is 92b0e32.** Check its image label/build id and diff `/app` against `git archive 92b0e32 app` (read-only). If it differs, stop and rebase the fix onto what is live.
3. Host disk has room for one more image (about 3.5 GB) plus a DB copy. `anyaicam-storage-maint` reports usage.
4. `GET https://app.anyaicam.com/api/appliance/signing-keys` returns exactly `anyaicam-identity-49cfda63a27f`. Record the full response.

## Deploy

1. **DB backup:** use SQLite's backup API to copy `/var/lib/anyaicam-staging/db/staging.db` to `staging-pre-cust-login-b55fc49-<UTC>.db`. Then run `PRAGMA integrity_check` on the copy (must be `ok`) and record its sha256. Record `user_sessions` and `appliances` counts.
2. **Ship the code:** create a git bundle of `b55fc49` on the Dell and send it over SSM in chunks. Clone it to `/opt/anyaicam-staging-build/repo-b55fc49`, then check `git rev-parse HEAD` = `b55fc49…`.
3. **Build:** `deploy-portal:b55fc49` with the same build arguments as the live image.
4. **Cutover:**

   ```
   python3 deploy/staging_bluegreen_cutover.py --live portal-92b0e32 \
       --candidate portal-b55fc49 --image deploy-portal:b55fc49 \
       --build-id <full b55fc49 sha>
   ```

   It uses the same env file (`/etc/anyaicam-staging/vms-staging.env`) and the same DB volume as the live container. It must print `CUTOVER_COMPLETE`. `CUTOVER_ABORTED` or `CUTOVER_ROLLED_BACK` leaves or restores 92b0e32 automatically.
5. **Post-deploy checks:**
   - `/version` on both `app.anyaicam.com` and `portal-staging` reports b55fc49;
   - `signing-keys` is byte-identical to the precondition record;
   - an administrator signs in to the Admin Portal;
   - `user_sessions` and `appliances` counts are unchanged;
   - the Ryzen agent heartbeat continues.
6. **Acceptance:** one owner sign-in on the Ryzen at `http://100.77.253.28:8000/customer-login.html`. It must land on `/customer-live`. Then continue the Playwright acceptance (appliance password, playback, and so on).

## Rollback

- **Automatic:** the cutover script restores `portal-92b0e32` if the public route fails after the switch.
- **Manual:**

  ```
  python3 deploy/staging_bluegreen_cutover.py --live portal-b55fc49 \
      --candidate portal-92b0e32-restore --image <92b0e32 image> --build-id 92b0e32…
  ```

  Alternatively, restart the kept stopped container `portal-92b0e32` with the aliases. No DB restore is needed because there is no schema change. Restore the DB backup only if data corruption is observed, which is unrelated to this change.
- **After rollback:** customer sign-in on 1.2.6 appliances fails again with the original error. Nothing else regresses.

## Risks

- **Brief interruption:** a few seconds during the cutover. The script keeps both containers serving during the swap.
- **Live drift:** if live is not exactly 92b0e32, the patch must be re-based (precondition 2).
- **Behaviour change after reset/change:** after a password reset or change, the user's appliance sessions end at the next manifest sync and their offline appliance password must be set again. This is intended and matches the 1.2.6 design.
- **Admin sessions:** they are not affected by the bump on the cloud.
