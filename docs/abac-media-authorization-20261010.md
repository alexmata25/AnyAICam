# Entitlement-based, per-customer AWS media authorization (ABAC), 2026-10-10

Status: code ready on `green/abac-media-auth-20261010` (flag defaults off). **No IAM change is applied; every IAM step below needs owner approval.**

## Problem
Production media access depends on two IAM roles whose inline policies hard-code individual customers' S3 paths:
- `anyaicam-recording-upload-camera1-pilot` (PutObject): the Ryzen customer, plus the approved Samsung pilot statement;
- `anyaicam-recording-read-camera1-pilot` (GetObject): the same.

Every new customer is AccessDenied on upload and on portal playback until someone edits IAM by hand.

## Design: two independent layers, no per-customer AWS edits
1. **IAM role policy (ABAC).** One generic statement per role. Its resource is built from STS session tags set by the server:
   `recordings/${aws:PrincipalTag/cust}/${aws:PrincipalTag/site}/${aws:PrincipalTag/appl}/${aws:PrincipalTag/cam}/...`
2. **STS session policy (already in code).** The server still narrows each credential to one camera's prefix (`event_media_session_policy` / `recording_session_policy`).

Both layers have to agree. A bug in either one alone cannot reach another customer's prefix. Tag and prefix values come only from the database identity:
- `_authorized_camera`: the camera belongs to the authenticated appliance, **and** to the appliance's own customer (new check);
- every id segment must match `^[A-Za-z0-9_-]{1,64}$` (new check).

## Code (this branch)
- `recording_credentials.py`:
  - `recording_s3_prefix()` validates every segment;
  - `recording_session_tags()` returns `cust, site, appl, cam, tier` (`events` | `continuous`).
- `appliance_cloud.py`:
  - when `ANYAICAM_RECORDING_SESSION_TAGS_ENABLED=true`, AssumeRole carries the tags. **Default false**: AssumeRole with Tags is refused until the trust policy allows `sts:TagSession`;
  - `_authorized_camera` adds the camera.customer == appliance.customer check;
  - an unsafe id returns 403.
- `main.py` (P7): read credentials refresh when their remaining life is less than `PRESIGNED_URL_REUSE_SECONDS + 60`. Before, URLs signed in the last 60–300 s of a credential's life were cached for 300 s and died early, which showed up as intermittent thumbnail and clip 403s.

## IAM changes (prepared, NOT applied; owner approval for each step)
**A. Upload role trust policy: add `sts:TagSession`, limited to these tag keys**
```json
{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::880690594006:role/anyaicam-ec2-app-role"},
 "Action":["sts:AssumeRole","sts:TagSession"],
 "Condition":{"ForAllValues:StringEquals":{"aws:TagKeys":["cust","site","appl","cam","tier"]}}}
```
**B. Upload role: generic inline policy `media-upload-abac`** (added next to the existing pilot policies first)
```json
{"Version":"2012-10-17","Statement":[
 {"Sid":"EventMediaByTags","Effect":"Allow","Action":"s3:PutObject",
  "Resource":"arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/${aws:PrincipalTag/cust}/${aws:PrincipalTag/site}/${aws:PrincipalTag/appl}/${aws:PrincipalTag/cam}/*/events/*"},
 {"Sid":"ContinuousByTags","Effect":"Allow","Action":"s3:PutObject",
  "Resource":"arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/${aws:PrincipalTag/cust}/${aws:PrincipalTag/site}/${aws:PrincipalTag/appl}/${aws:PrincipalTag/cam}/*",
  "Condition":{"StringEquals":{"aws:PrincipalTag/tier":"continuous"}}}]}
```
Continuous is granted only with `tier=continuous`. The server sets that only for `RECORDING_UPLOAD_ENABLED` or pilot cameras (the Ryzen, today). Continuous recording is not sold and stays off for everyone else.

**C. Read role: generic GetObject.** The server alone assumes it, and it is used only to presign keys taken from database rows of a camera the portal user is authorized for:
```json
{"Version":"2012-10-17","Statement":[{"Sid":"ReadRecordings","Effect":"Allow","Action":"s3:GetObject",
 "Resource":"arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/*"}]}
```
Follow-up hardening (separate change): assume it with a per-customer session policy (`recordings/{customer}/*`), cached per customer.

**D. Bucket lifecycle backstop.** Merge into the full existing configuration, because `put-bucket-lifecycle-configuration` replaces it:
`{"ID":"recordings-backstop-35d","Filter":{"Prefix":"recordings/"},"Status":"Enabled","Expiration":{"Days":35},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}`.
35 days is above the 30-day maximum plan retention; the in-app retention sweep stays the primary mechanism.

## Rollout (each step reversible and individually approved)
1. Deploy the cloud with this branch, flag off. No behaviour change.
2. Apply A (trust policy), then B (added, pilots kept). Simulate with real ids for the Ryzen, the Samsung and a synthetic third customer: own prefix allowed, every other prefix implicitDeny.
3. Set `ANYAICAM_RECORDING_SESSION_TAGS_ENABLED=true` (cloud env, container recreate). Verify Ryzen and Samsung uploads, then a new customer in staging.
4. Apply C. Verify portal playback for every customer.
5. Remove the pilot statements (Ryzen, Samsung). Rollback: re-add them; the JSON is saved in the review folder.
6. Apply D after confirming the retention sweep's own configuration.

**Rollback at any step:** set the flag false and/or `put-role-policy` the saved previous documents. No data is touched.

## Verification
- IAM simulator, with custom policy and context keys `aws:PrincipalTag/*`:
  - allowed: own events key; own continuous key only with `tier=continuous`;
  - implicitDeny: another customer, another camera, `tier=events` on a continuous key.
- Unit tests: `tests/test_event_media_credential_path.py` (tags, tier, flag off, tenant mismatch, unsafe ids), `tests/test_recording_read_credentials_cache.py` (P7).
- Isolated stack journey: two customers. moto does not enforce IAM, so policy evaluation is covered by the simulator, and real AWS remains **NOT VERIFIED** until staging end-to-end acceptance.
