# R5 — Recording-lifecycle (delete) IAM design

**Status: DESIGNED, NOT YET APPLIED.** Same split as `docs/r1-recording-iam.md`
and `docs/r4-recording-read-iam.md`: templates ready to apply, nothing
created in the real AWS account yet. `_delete_recording_object()`
(`app/recording_retention_sweep.py`) is built and reviewable now, and
fails closed (deletes nothing, keeps the catalog row for retry) until
the role below exists and `ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN` is set.

## Why this is a fourth role, not a reuse of R1's or R4's

Three distinct S3 capabilities now exist for the same `recordings/`
prefix, each its own role:

| Role | Action | Used by |
|---|---|---|
| `anyaicam-recording-upload` (R1) | `s3:PutObject` only | Appliance uploader (R3) |
| `anyaicam-recording-read` (R4) | `s3:GetObject` only | Customer Playback presigned URLs |
| `anyaicam-recording-lifecycle` (R5, this doc) | `s3:DeleteObject` only | This sweep, server-side only |

None of the three can perform another's action — an appliance that can
upload can never read or delete; a customer's presigned URL can never
delete; this sweep can never read a recording's contents, only remove
it once expired. Each role is independently revocable without affecting
the other two.

### 1. Recording-lifecycle role — trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::880690594006:role/anyaicam-ec2-app-role"},
    "Action": "sts:AssumeRole"
  }]
}
```

### 2. Recording-lifecycle role — permissions policy

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:DeleteObject",
    "Resource": "arn:aws:s3:::{RECORDING_BUCKET}/recordings/*"
  }]
}
```

### 3. `anyaicam-ec2-app-role` — permissions policy addition

A third additive `sts:AssumeRole` statement, alongside R1's and R4's:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::880690594006:role/anyaicam-recording-lifecycle"
}
```

## Companion S3 bucket lifecycle rule (failsafe, not the primary mechanism)

S3 bucket lifecycle rules can't read a per-customer database column, so
they can't enforce `plans.retention_days` directly — that's what
`run_retention_sweep_tick()` is for. A bucket-level rule is still worth
adding as a **failsafe backstop** in case the sweep is ever disabled or
broken for an extended period: expire anything older than the longest
realistic plan tier (illustrative — not applied):

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket anyaicam-recordings-2026 \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "recordings-failsafe-max-age",
      "Filter": {"Prefix": "recordings/"},
      "Status": "Enabled",
      "Expiration": {"Days": 60}
    }]
  }'
```

60 days is illustrative only — pick a value comfortably longer than the
longest plan's `retention_days` so this rule only ever fires as a
backstop, never as the customer-visible retention boundary itself.

## Illustrative AWS CLI commands for the role (NOT executed)

```bash
aws iam create-role \
  --role-name anyaicam-recording-lifecycle \
  --assume-role-policy-document file://recording-lifecycle-trust-policy.json

aws iam put-role-policy \
  --role-name anyaicam-recording-lifecycle \
  --policy-name recording-lifecycle-base \
  --policy-document file://recording-lifecycle-permissions-policy.json

aws iam put-role-policy \
  --role-name anyaicam-ec2-app-role \
  --policy-name recording-lifecycle-assume \
  --policy-document file://ec2-app-role-recording-lifecycle-assume-addition.json
```

## Once applied

```
ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-recording-lifecycle
ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=false   # flip to true only after this is verified against a real customer/plan
```

## Verification checklist (run only after execution)

1. Positive test: with the sweep enabled against a real, isolated test
   object past its plan's retention, both the S3 object and the
   `recordings` row are gone afterward.
2. Negative test: a recording still within its plan's retention window
   is untouched by the same sweep run.
3. Confirm the lifecycle role cannot `PutObject`/`GetObject` — a
   delete-only role must stay delete-only even under an assumed
   session.
4. Confirm a recording whose customer has no plan row on file is never
   deleted by the sweep, regardless of age.
