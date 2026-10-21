# Owner-only AWS Cost Dashboard

## Scope and access boundary

The dashboard is part of the existing AnyAiCam Admin Portal. It does not add a public-site page, customer navigation item, or customer API. Both `/admin/aws-costs` and `/api/admin/aws-costs/summary` require:

1. `ANYAICAM_PLATFORM_OWNER_EMAIL` to be configured for the deployment;
2. a signed Partner Portal administrator session whose email exactly matches that setting; and
3. a live, non-revoked `administrator` / `global` identity grant on every request.

An ordinary legacy local admin, a partner-scoped admin, and any customer are denied. A missing setting, invalid session, database error, or missing/revoked grant fails closed. Reads of the page and API are written to the existing application audit log. The web process never returns or accepts AWS credentials.

## Data design

The additive migration `20261015_aws_cost_dashboard_snapshots` adds:

- `aws_cost_monthly_snapshots`: AWS Cost Explorer values by month, service, and record type, with the API's `Estimated` bit and collection time retained. A completed month is labeled finalized only when Cost Explorer says it is not estimated.
- `aws_camera_usage_snapshots`: daily per-camera sums by storage class from an S3 Inventory report, plus object/version count, oldest/newest S3 `LastModifiedDate`, and a point-in-time recording-mode/retention snapshot. Current camera mode and retention are resolved from the active per-camera plan, camera cloud/local recording settings, customer cloud-retention override, and the legacy plan fallback used by the existing event-media policy. It stores no object keys, credentials, media, or customer names.
- `aws_lifecycle_rule_snapshots`: read-only lifecycle-rule snapshots including the filter needed to evaluate a camera prefix.

The dashboard reads these snapshots and the existing customers/sites/appliances/cameras tables in a fixed set of queries. Missing measurements remain `null`/“Not measured”; they are not displayed as zero. It makes no AWS calls on page load and does not run `ListObjects`.

## Cost semantics

- Account charges come only from persisted Cost Explorer `UnblendedCost` results grouped by service and record type. The dashboard separates estimated and finalized periods and displays service/tax categories as returned.
- Camera storage is an estimate at the current Inventory snapshot size multiplied by the configured/list S3 class rate. It is not a billed camera-level GB-month. Only S3 Standard `us-east-1` has a built-in display estimate of `$0.023` per decimal GB-month; unpriced classes produce an unknown estimate rather than `$0`.
- Camera PUT/GET and transfer charges remain unallocated unless a future approved metering source supplies prefix-level measurements. S3 request costs cannot be reliably assigned to customers from account-wide Cost Explorer service totals alone.
- Shared infrastructure is shown as not allocated. The customer “operating cost” is therefore storage-only when measured, not a complete profitability figure or AWS invoice.
- Cost projections assume steady-state daily data, a 30-day billing month, selected retention, and user-entered average GB/camera/day. They exclude taxes, cross-service transfer, shared infrastructure, and unknown request counts. Request estimates are included only if the user supplies request volumes and rates.

The storage price is a configurable estimate, not a contract rate. Verify the current [AWS S3 pricing page](https://aws.amazon.com/s3/pricing/) before using it for customer pricing.

## Daily collection flow

1. After a separately approved S3 Inventory configuration exists, an operator/scheduler supplies that report's exact `manifest.json` key. The collector reads only the manifest, its checksum, and the named CSV shards using `GetObject`; it does not list the bucket or fetch recording objects. It checks the manifest and shard MD5 values, streams compressed CSV through a restrictive temporary directory, aggregates camera-prefix bytes/counts in memory, and deletes temporary files on exit.
2. Camera prefixes are generated through the existing `recording_s3_prefix(customer_id, site_id, appliance_id, camera_id)` contract. The manifest source bucket must match `ANYAICAM_RECORDING_S3_BUCKET`, its destination ARN must match the explicitly requested Inventory bucket, and each CSV row's Bucket field must match that source bucket. The manifest companion MD5 and every listed shard MD5 are required and verified. Shards must stay under the report's `data/` directory. The parser URL-decodes S3 Inventory keys before exact-prefix matching. Unknown prefixes are ignored.
3. The collector may call Cost Explorer once per scheduled run and save monthly service/record-type snapshots. It may also read the bucket lifecycle configuration and save a separate dated snapshot. All three data sources are read-only.
4. The dashboard serves cached database snapshots. Collection runs once daily outside a request path; the page never triggers inventory or Cost Explorer calls.

### Automatic daily schedule (1.2.7 RC9)

`app/aws_cost_collection_scheduler.py` is started from the application lifespan in `app/main.py` and calls the collector functions in-process (no subprocess). It runs only when **all** of these are true:

- `ANYAICAM_RUNTIME_ROLE=cloud` — edge and combined appliances never start it (they must not hold AWS billing permissions);
- `ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED=true`;
- `ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED=true`.

Timing: one run `ANYAICAM_COST_SNAPSHOT_STARTUP_DELAY_SECONDS` (default 300) after startup, then daily at `ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC` (0–23, default 6). Each UTC day is claimed in the `aws_cost_collection_runs` table (primary key = run date, migration `20261020_aws_cost_collection_runs`), so several workers or containers on the same database collect at most once per day; a claim left `running` for more than 3 hours (crashed worker) can be reclaimed. A failed day is not retried automatically; the owner can retry it manually (below).

Each run performs, independently:

| Step | Runs when | Reads |
|---|---|---|
| `cost_explorer` | always | `ce:GetCostAndUsage`, previous month start to today, grouped by service + record type and service + usage type |
| `lifecycle` | `ANYAICAM_RECORDING_S3_BUCKET` is set | `s3:GetLifecycleConfiguration` on that bucket |
| `inventory` | `ANYAICAM_COST_INVENTORY_MANIFEST_S3` is set | `s3:GetObject` on the exact manifest, its checksum and listed shards |

`ANYAICAM_COST_INVENTORY_MANIFEST_S3` is an exact `s3://bucket/key` or a template containing `{date}` (for example `s3://<inventory-bucket>/<prefix>/<recordings-bucket>/<config-id>/{date}T01-00Z/manifest.json`); `{date}` is tried as the run date and then the previous day. Copy the fixed time part (`T01-00Z`) from the first real delivery. Because the role intentionally has no `s3:ListBucket`, S3 answers `AccessDenied` (not `NoSuchKey`) when that day's manifest does not exist.

Outcome per run is `succeeded`, `partial` (some steps failed) or `failed`, stored with an error category (`permission`, `credentials`, `not_found`, `aws_error`, `validation`, `internal`), the AWS error code and an owner-readable instruction — never the raw AWS message, ARNs or credentials. A permission failure is logged as `aws_cost.collection_permission_denied` and stored as a status; no cost row is written for a failed request, so a denied call can never appear as `$0`. The worker catches every error and never stops the application.

The dashboard shows `Last collection: <time> OK` or `Last collection: <time> FAILED: AccessDeniedException — ask an AWS administrator to grant ce:GetCostAndUsage …`, the next scheduled run, and — when no snapshot rows exist — an explicit empty state explaining whether collection is disabled (and which flag/role), not yet run, in progress, or failed. No figures are displayed until real snapshots exist.

The collector defaults off. A scheduled invocation requires explicit `ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED=true`; API reads additionally require `ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED=true`. Use the cloud workload's approved IAM role/default credential provider. Do not put access keys in code, browser storage, or command arguments. No AWS resource is created or changed by this code.

Examples (after owner approval of the read-only role and Inventory destination):

```powershell
# Environment variables are deployment settings, not credentials.
$env:ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED='true'
$env:ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED='true'
$env:ANYAICAM_RECORDING_S3_BUCKET='<recordings-bucket-name>'
python app/aws_cost_snapshot_collector.py --refresh-cost-explorer --refresh-lifecycle --bucket $env:ANYAICAM_RECORDING_S3_BUCKET
python app/aws_cost_snapshot_collector.py --inventory-manifest-s3 s3://<inventory-bucket>/<inventory-prefix>/<manifest-key>
```

The command should run inside the cloud app runtime so `ANYAICAM_DATABASE_BACKEND` points at its configured database. These examples do not authorize enabling the collector in production; deployment and AWS permission changes remain approval gates.

## Minimum read-only AWS permissions for collection

No policy is applied by this work. A least-privilege draft is in [`aws-cost-collector-iam-policy.DRAFT-NOT-APPLIED.json`](aws-cost-collector-iam-policy.DRAFT-NOT-APPLIED.json); replace the three `REPLACE_WITH_…` placeholders before review. The workload role needs only:

- `ce:GetCostAndUsage` on `Resource: "*"` (Cost Explorer does not support a narrower resource ARN for this API);
- `s3:GetObject` on the exact S3 Inventory destination prefix containing its manifest/checksum/report CSV files;
- `s3:GetLifecycleConfiguration` on the recordings bucket ARN.

If the Inventory destination encrypts report objects with SSE-KMS, S3 reads may also require `kms:Decrypt` on that specific customer-managed key. Add it only after verifying the report encryption, and constrain it to the Inventory destination's encryption context; do not grant decrypt access to the recordings bucket's media key. If report encryption is SSE-S3, no KMS permission is needed.

No S3 listing is necessary when the scheduled job is given the exact manifest path. Do not grant `s3:ListBucket` on recording prefixes, `GetObject` on recording media, any write/delete/tag action, Cost Explorer forecast, or IAM permissions. If a future scheduler must discover the newest manifest, request a separate review for a narrowly prefix-conditioned `s3:ListBucket` on the Inventory destination bucket only.

## Owner procedure to get real AWS data

Every step that changes AWS or the live cloud requires owner/administrator approval. Nothing below is done by the application or by this change.

1. **Apply IAM (owner/AWS administrator approval).**
   - Identify the role attached to the cloud EC2 instance (EC2 console → instance → Security → IAM role). If none exists, create a dedicated read-only role and instance profile for it.
   - Fill the placeholders in `docs/aws-cost-collector-iam-policy.DRAFT-NOT-APPLIED.json`, review it (three statements, no `s3:ListBucket`, no write/delete/IAM/KMS actions) and attach it as an inline or customer-managed policy to that role.
   - Cost Explorer must be enabled for the account (Billing → Cost Explorer → *Launch Cost Explorer*); it can take up to 24 hours before the API returns data. If the account is a member of an AWS Organization, Cost Explorer access may also need to be allowed from the management account.
   - The app runs in Docker on EC2: set the instance metadata option *HTTP PUT response hop limit* to `2` (IMDSv2) so the container can obtain the role credentials. Never put access keys in the environment file.
2. **Set up S3 Inventory (optional, for camera-level storage; separate approval because it has a small recurring cost).** On the recordings bucket create a daily CSV Inventory configuration (fields: Size, Last modified date, Storage class; include all versions if versioning is on) delivering to a separate destination bucket/prefix. After the first delivery (up to 48 hours) note the exact `…/<config-id>/<YYYY-MM-DD>THH-MMZ/manifest.json` path and turn it into the `{date}` template. If the destination uses SSE-KMS, review the optional `kms:Decrypt` statement described above.
3. **Set the cloud environment (deployment change, owner approval).** In the cloud container's environment file:
   ```
   ANYAICAM_RUNTIME_ROLE=cloud
   ANYAICAM_PLATFORM_OWNER_EMAIL=<owner email that signs in to the Admin Portal>
   ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED=true
   ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED=true
   ANYAICAM_RECORDING_S3_BUCKET=<recordings bucket>
   ANYAICAM_COST_INVENTORY_MANIFEST_S3=s3://<inventory-bucket>/<prefix>/<recordings-bucket>/<config-id>/{date}T01-00Z/manifest.json   # only after step 2
   ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC=6
   AWS_REGION=us-east-1
   ```
   Restart the container so the settings and the `20261020_aws_cost_collection_runs` migration apply. The startup log shows `aws_cost.collection_scheduler_started` (or `aws_cost.collection_scheduler_disabled` with the reason).
4. **Verify one manual run** inside the cloud container (it records its status like a scheduled run and may retry a failed/partial run of the same UTC day):
   ```
   docker exec <cloud-container> python /app/aws_cost_snapshot_collector.py --run-daily-collection
   ```
   Expect JSON with `"status": "succeeded"` and per-step counts. `AccessDeniedException` on `cost_explorer` → step 1 is not effective (wrong role, policy not attached, or hop limit). `NoCredentialsError` → the container cannot reach the instance role. Then open `/admin/aws-costs` as the owner and confirm `Last collection: … OK` and populated monthly totals.
5. **Let the schedule run.** The next day after `ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC`, confirm the dashboard shows the new run time, or query `SELECT run_date,status,error_code FROM aws_cost_collection_runs ORDER BY run_date DESC LIMIT 7`. Logs: `aws_cost.collection_finished`, `aws_cost.collection_permission_denied`, `aws_cost.collection_step_failed`.

To disable: set either flag to `false` (or unset) and restart; the stored snapshots remain read-only history.

### Verifying the numbers against the AWS bill

- In the AWS console open **Cost Explorer**, set the same month, granularity *Monthly*, metric **Unblended cost**, group by **Service**, and compare each service to the dashboard's month/service table. Totals should match to the cent for the same capture time; the current month is labelled *estimated* and keeps changing until AWS finalizes it.
- For a closed month, compare the dashboard total (finalized) with **Billing → Bills** for that month. Expected differences: credits, refunds, savings plans/reserved-instance amortization and support charges appear under their own record types, and tax is shown as the *Tax* service; the dashboard does not net them out.
- Spot-check S3: the dashboard's S3 usage-type rows (e.g. `TimedStorage-ByteHrs`, `Requests-Tier1`) should match Cost Explorer grouped by **Usage type** filtered to *S3*.
- Camera storage figures are estimates (Inventory bytes × list price), not billed amounts; compare their sum with the bucket's size in S3 Storage Lens or CloudWatch `BucketSizeBytes` for the same day, not with the bill.

## Retention interpretation

The dashboard compares the effective Hybrid/cloud-event retention setting with the latest compatible lifecycle-rule snapshot. It treats complex filters as unverified. Current mode and retention come from the same customer billing/policy sources used by VMS event-media authorization; the inventory snapshot retains the mode/retention observed at collection time, but the page reports the current configuration and separately identifies the inventory snapshot date. It displays the oldest S3 upload timestamp and flags uploads older than the configured lifecycle interval plus a 48-hour tolerance. S3 lifecycle expiration is asynchronous and `LastModifiedDate` is upload time, not necessarily camera capture time; Inventory and a lifecycle rule do not prove a particular object was deleted. `actual_deletion_verified` intentionally remains false.

## Current limitations and deployment gates

- Previous audit figures are not seeded as live records. Development tests use synthetic rows. Production begins with “Not measured” until separately approved collection runs.
- Existing audit found no S3 Inventory configuration, so camera-level daily trends will remain unavailable until an owner approves an Inventory configuration/destination and its small recurring costs.
- Existing account-wide Cost Explorer totals do not yield per-camera request or egress attribution. S3 server access logging/CloudTrail data events were not enabled by this task and must not be enabled without review.
- Storage rates, S3 class pricing, discounts, tax treatment, savings plans, credits/refunds, and other consolidated billing adjustments need verification against finalized Cost Explorer/bill data before being used for price or margin commitments.
- Production deployment requires an independently reviewed owner-email setting, a read-only workload role, tested database migration, approved Inventory report source, and staging verification. This change does not modify production AWS, Stripe, the VMS release, or appliance state.

## 1.2.7 integration, deployment, and rollback

The dashboard source was developed separately from the VMS history. Integrate it by selectively porting these six files into a new worktree based on the approved 1.2.7 source: `app/aws_cost_dashboard.py`, `app/aws_cost_snapshot_collector.py`, `app/db_migrations.py` (the single dashboard migration), `app/main.py` (route registration and owner-only portal link), `app/tests/test_aws_cost_dashboard.py`, and this document. Do not cherry-pick the development commit across unrelated Git object histories. Keep the migration ID unique in the target tree. Do not include unrelated Cost Dashboard branch history or configuration secrets.

Before staging deployment:

1. Back up the staging database and current application image/configuration, then verify the backup can be read and restored in isolation.
2. Keep both AWS collector gates unset/false. Set the designated owner email only to the approved staging owner account, and verify that a live global administrator grant is required on every request.
3. Apply the additive migration through the normal VMS startup/migration path. Verify all three snapshot tables exist and a second migration run is idempotent.
4. Run the dashboard, owner authorization, adjacent identity-grant, migration, and relevant VMS regression suites. Seed only synthetic snapshot data.
5. Verify the browser routes make no AWS calls, customer/partner users receive no dashboard access, access is audit logged, and empty or incomplete snapshots remain “Not measured.”
6. Enable AWS collection in staging only after a separately approved read-only role and exact Inventory manifest source are available. Confirm the role has no write, delete, IAM, EC2, or recording-object read permissions. Do not create Inventory configuration as part of application deployment.

Rollback is an application rollback, not an AWS rollback: disable the collector flags, restore the previously backed-up application image and configuration, and verify the prior VMS health/authentication paths. Leave the three additive snapshot tables and their migration record in place; the old application ignores them, and this avoids a risky destructive/down migration. Restore the database backup only if the migration or database itself is damaged, and only after stopping the staging application and confirming the backup; do not delete AWS reports or alter bucket settings. Production rollout remains a separate owner approval gate.

## 1.2.7 isolated CI verification

The branch-scoped workflow at `.github/workflows/vms-1.2.7-cost-dashboard-ci.yml` runs on GitHub-hosted Ubuntu only. Its Linux job runs the dashboard plus the previously verified migration/Analytics Control Center neighbors, then the platform-owner and identity-grant authorization suites. The job uses a temporary SQLite database and synthetic identities; it has repository read permission only and receives no AWS, Stripe, database, or deployment secrets.

A separate PostgreSQL 16 service job creates two disposable databases. Its test initializes the pre-dashboard schema, seeds synthetic customer/site/appliance/camera rows, takes a `pg_dump` backup, applies the dashboard migration twice, checks migration idempotence and record preservation, then restores the pre-migration dump into the separate restore database and verifies the old schema/data are present there. Both jobs publish JUnit XML and raw logs as workflow artifacts, and write pass/fail/error/skip counts to the GitHub job summary. These jobs do not validate a production or shared staging database and do not grant permission to deploy.
