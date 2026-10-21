# Test-account cleanup (Requirement #11): owner procedure

Tool: `app/account_cleanup.py`. It runs offline against the application database through the app's own DB
layer (`database_backend`), so it works on SQLite and PostgreSQL. It never calls Stripe, AWS, S3 or an appliance.

**A production run needs the owner's written authorization.** Red, Green, Codex and the tool itself cannot give it.

## Classes

| Class | Meaning | What `apply` does |
|---|---|---|
| **P** protected | Owner admin `amata@anyaicam.com`; Founder `alexmata25@gmail.com` / `4efaf5153f…` (never archive); sandbox admin grant `ca357bbbd5388114`; `ANYAICAM_PLATFORM_OWNER_EMAIL`, `ANYAICAM_ADMIN_EMAIL`, `ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS`; any login user with a staff role or a live global grant. Also any login user with no customer account. | Never touched. `plan` refuses the whole list. |
| **C** appliance-linked | Any appliance, camera, claim, recording, detection event, event media, WireGuard peer, provisioning/scan job, facial event or live-view session. | Refused unless `--allow-appliance-linked --appliance-linked-reason "…"`. Even then it is only tombstoned (as B), and no appliance, camera or recording row is changed. |
| **B** tombstone | Any other history: entitlements, orders, Stripe bindings, payments, notes, quotes, and so on. | Every row is kept. `customers.status='closed_test'`. Each login is disabled: `account_status='revoked'`, `approved=0`, an unusable password, and sessions, grants, tokens and devices revoked. The email is released: the customer, its login users, its registration requests and its direct signups are rewritten to `closed-test+…@tombstone.invalid`. Pending invitations are cancelled and lockouts for the email are cleared. |
| **A** hard delete | Only login, identity and preference rows exist. No billing, Stripe, order or appliance history. | These rows are deleted in FK-safe order, inside one transaction. |

The class always comes from the live data. The approved list must state the class it expects. If the live class is
different, the list is refused.

Why the tombstone rewrites the email: a Stripe event with no authoritative customer id is matched by email. Since
1.2.7, `customer_lookup.find_usable_customer_by_email` ignores `closed_test` accounts. The rewrite also covers older
code and every other email lookup (signup, registration, guest hardware orders). A later purchase made with that email
becomes a pending link. It attaches only to a new, verified account.

## Procedure

1. **Read-only inventory.** The owner or Codex runs this inside the portal container, or against a copy of the
   database:

   ```sh
   python account_cleanup.py inventory --output inventory.json
   ```

   It lists every customer and login user with its class, reasons, dependency counts, billing rows and
   `email_sha256`. Emails are masked.

2. **Approved list.** The owner picks the accounts and writes `approved.json`:

   ```json
   {"format": 1, "approved_by": "owner name", "approved_at": "2026-10-20",
    "accounts": [{"customer_id": "…", "expected_class": "B", "email_sha256": "<from the inventory>",
                  "test_billing_rows": ["customer_entitlements:<id>"]}]}
   ```

   About `test_billing_rows`:
   - List an active or paid billing row there only after checking in the Stripe dashboard (Test Mode) that it is a
     test transaction.
   - Rows with `cs_test_…` sessions, or with a test-mode customer binding, are already marked as test.
   - Any live evidence is always refused, attestation or not: a `cs_live_…` session or a `livemode=1` binding.
   - `inventory --approved approved.json` shows the approved accounts' emails unmasked, so the owner can check them.

3. **Plan (dry run, read-only).**

   ```sh
   python account_cleanup.py plan --approved approved.json --output plan.json
   ```

   The whole list is refused if any of these is true:
   - an account is P;
   - an account is C and the flag and reason are missing;
   - the class or the email hash differs from the approved list;
   - an account is unknown or already closed;
   - a billing row has live Stripe evidence;
   - an active or paid billing row is neither marked as test nor attested.

   The plan lists every row change for each account: table, match, row count and ids. It also gives a
   `confirm_token` and a `plan_sha256`. It contains no plaintext email.

4. **Codex review** of `plan.json`, including the exact steps and the counts.

5. **Backup.**
   - **SQLite (the live cloud today):** `apply --backup-dir DIR` makes an online backup itself while it holds the
     write lock, then checks it: `integrity_check` must be `ok`, and every account in the copy must match the live
     state. Only then does it write. Also copy the JSON stores under the recordings folder.
   - **PostgreSQL:** after the plan, take `pg_dump -Fc -f cleanup.dump "$ANYAICAM_DATABASE_URL"` and verify it with
     `pg_restore --list cleanup.dump`. Then pass `--backup-verified cleanup.dump`. Without that file the tool refuses.
     It also refuses a dump that is older than the plan.

6. **Apply**, within 30 minutes of the plan (`--max-age-minutes` changes the limit):

   ```sh
   python account_cleanup.py apply --plan plan.json --confirm APPLY-<token> --actor "<who>" --backup-dir /backups
   ```

   Before writing, `apply`:
   - checks the plan hash, the token, the plan's age and that it targets this database;
   - re-derives every change from the live data and refuses if anything differs from the reviewed plan.

   Then it backs up and makes all the changes in **one transaction**. Before committing, it checks:
   - the exact row-count change of every table;
   - SQLite only: no new `foreign_key_check` violation, and `quick_check` returns `ok`;
   - every account outside the plan is unchanged;
   - deleted accounts are gone;
   - tombstoned accounts no longer hold their emails.

   If any check fails, everything rolls back.

   It writes one `audit_logs` row per account plus one summary row (actor role `maintenance:test_account_cleanup`,
   plan hash, email hash, backup hash) and one `test_account_closures` row per account. Running the same plan again
   reports `already_applied` and changes nothing.

7. **After apply.** Run `inventory` again: closed accounts show `closed: true`. Rollback means restoring the backup.

Nothing writes without `apply` and its confirm token. `inventory` and `plan` open the database read-only.

## Not covered (owner decisions)

- **D1.** Tombstoned accounts still appear in `/admin-customers`, labelled `closed_test`. Hiding them needs a small
  list filter, which needs the owner's approval and a cloud deploy.
- The JSON identity stores under the recordings folder (`users.json`, `partner_customers.json`, …) are not changed.
- No Stripe, S3, IAM or appliance credential is changed. Stripe Test Mode objects stay.
- `apply` requires the `20261020_test_account_closures` migration, so 1.2.7 must be deployed first.
