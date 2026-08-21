# R4 — Recording-read IAM design

**Status: DESIGNED, NOT YET APPLIED.** Same split as `docs/r1-recording-iam.md`:
this document's templates are ready to apply, but nothing here has been
created in the real AWS account. `_presigned_recording_url()`
(`app/main.py`) is built and reviewable now, and fails closed (returns
`None`, the recording is skipped rather than shown as a dead link) until
the role below exists and `ANYAICAM_RECORDING_READ_ROLE_ARN` is set.

## Why this is a third role, not a reuse of R1's upload role

R1's upload role is deliberately write-only (`s3:PutObject` only, no
`GetObject`/`ListBucket`/`DeleteObject`) — that's what makes it safe to
hand short-lived credentials for it to an appliance. A presigned **GET**
URL can only be valid if the principal that signed it actually has
`s3:GetObject` on that object, so serving recordings to a customer's
browser needs a *separate*, read-only role. This mirrors the same
"separate role per direction" pattern R1 itself already established
relative to the live-relay upload role, and the existing
CloudFront-signing-key-reader role already does for a different purpose
(§8 of `AI_HANDOFF.md`).

### 1. Recording-read role — trust policy

Trusts `anyaicam-ec2-app-role` specifically, same shape as R1's:

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

### 2. Recording-read role — permissions policy

Read-only, scoped to the recording bucket's own prefix — never write,
never list (a customer's browser only ever needs one specific object it
already has the key for from the catalog query, not a directory listing):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::{RECORDING_BUCKET}/recordings/*"
  }]
}
```

### 3. `anyaicam-ec2-app-role` — permissions policy addition

A second additive `sts:AssumeRole` statement, alongside R1's own addition
for the upload role:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::880690594006:role/anyaicam-recording-read"
}
```

## Illustrative AWS CLI commands (NOT executed)

```bash
aws iam create-role \
  --role-name anyaicam-recording-read \
  --assume-role-policy-document file://recording-read-trust-policy.json

aws iam put-role-policy \
  --role-name anyaicam-recording-read \
  --policy-name recording-read-base \
  --policy-document file://recording-read-permissions-policy.json

aws iam put-role-policy \
  --role-name anyaicam-ec2-app-role \
  --policy-name recording-read-assume \
  --policy-document file://ec2-app-role-recording-read-assume-addition.json
```

## Once applied

```
ANYAICAM_RECORDING_READ_ROLE_ARN=arn:aws:iam::880690594006:role/anyaicam-recording-read
```

`ANYAICAM_RECORDING_S3_BUCKET` and `AWS_REGION` are already required by
R1/R3 and are reused as-is — no new bucket/region configuration needed.

## Verification checklist (run only after execution)

1. Positive test: a presigned URL generated for one real recording's
   `s3_key` actually downloads that object's bytes.
2. Negative test: a hand-modified presigned URL (wrong key, expired,
   or truncated signature) is rejected by S3.
3. Confirm `anyaicam-ec2-app-role` still has no direct S3 access of its
   own — only three `sts:AssumeRole` grants total (live-upload,
   recording-upload, recording-read).
4. Confirm the recording-read role cannot `PutObject`/`DeleteObject` —
   a read-only role must stay read-only even under an assumed session.
