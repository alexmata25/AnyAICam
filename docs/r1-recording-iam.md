# R1 — Recording-upload IAM design

**Status: DESIGNED, NOT YET APPLIED.** Mirrors Phase 1's own live-relay IAM
design/execution split exactly (see `AI_HANDOFF.md` §8, Phase 1). The
templates below are ready to apply, but nothing in this document has been
created in the real AWS account. The application code that will use this
role (`app/recording_credentials.py`, wired into
`app/appliance_cloud.py`) is built and reviewable now, and stays inert
until the role exists and `ANYAICAM_RECORDING_UPLOAD_ENABLED=true` is set
— the same fail-closed pattern Phase 2 used for live relay.

**Why this wasn't executed as part of R1**: creating an IAM role is an
IAM-admin action. The EC2 instance's own role (`anyaicam-ec2-app-role`)
is deliberately scoped to *assume specific, already-approved role ARNs
only* — it has no permission to create new IAM roles or policies, by the
same design that already prevents it from having direct S3 access. That
scoping is a security property, not a gap — this step needs to be run by
someone with IAM-admin AWS credentials, outside of anything this
application (or an agent operating only through it) can do on its own.

## Two role-scoped resources needed, same shape as the live-relay pair

### 1. Recording-upload role — trust policy

Trusts `anyaicam-ec2-app-role` specifically, not any EC2 instance:

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

### 2. Recording-upload role — base permissions policy

No `s3:ListBucket`/`GetObject`/`DeleteObject` — write-only, exactly like
the live-upload role. The *actual* write scope for any single session is
narrowed further by the per-camera session policy `app/recording_credentials.py`
already generates (`recording_session_policy()`), passed as the `Policy`
parameter on `sts:AssumeRole` — this base policy is the outer bound, not
the real-time scope.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:PutObject",
    "Resource": "arn:aws:s3:::{RECORDING_BUCKET}/recordings/*"
  }]
}
```

### 3. `anyaicam-ec2-app-role` — permissions policy addition

The existing app role's permissions policy currently allows `sts:AssumeRole`
on the live-upload role ARN only. It needs a second, additive statement —
**a second manual step beyond creating the new role**:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::880690594006:role/anyaicam-recording-upload"
}
```

## Illustrative AWS CLI commands (NOT executed)

Same pattern as Phase 1's own documented illustrative commands — for
whoever runs this with IAM-admin credentials:

```bash
# Dedicated recording bucket (see architecture doc §07 decision 02 —
# recommended over reusing the live bucket, so lifecycle rules never
# cross-apply between live/'s fixed 1-day expiry and per-plan retention).
aws s3api create-bucket --bucket anyaicam-recordings-2026 --region us-east-1

# Recording-upload role
aws iam create-role \
  --role-name anyaicam-recording-upload \
  --assume-role-policy-document file://recording-upload-trust-policy.json

aws iam put-role-policy \
  --role-name anyaicam-recording-upload \
  --policy-name recording-upload-base \
  --policy-document file://recording-upload-permissions-policy.json

# Extend the existing app role to also assume the new role
aws iam put-role-policy \
  --role-name anyaicam-ec2-app-role \
  --policy-name recording-upload-assume \
  --policy-document file://ec2-app-role-recording-assume-addition.json
```

## Once applied

Set on the production host (not yet set, deliberately — this keeps the
route this design supports returning `503 Recording upload is not
configured.` until someone deliberately configures it):

```
ANYAICAM_RECORDING_UPLOAD_ENABLED=false   # flip to true only after R2-R3 exist
ANYAICAM_RECORDING_UPLOAD_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-recording-upload
ANYAICAM_RECORDING_S3_BUCKET=anyaicam-recordings-2026
```

## Verification checklist (run only after execution, mirroring Phase 1's own)

1. Positive test: a session scoped to one real `camera_id` can `PutObject`
   under that camera's own `recordings/{customer_id}/{site_id}/{appliance_id}/{camera_id}/` prefix.
2. Negative test: that same session's credentials **fail** to `PutObject`
   under a different `camera_id`'s prefix.
3. Confirm `anyaicam-ec2-app-role` still has no direct S3 access of its own
   — only the two `sts:AssumeRole` grants (live-upload, recording-upload).
