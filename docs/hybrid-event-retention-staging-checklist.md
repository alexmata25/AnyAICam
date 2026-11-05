# Hybrid 14-day cloud event retention — staging checklist

Status: **configuration required on the cloud host; nothing here has been
applied.** The code that decides what expires is in place
(`app/recording_retention_sweep.py`): Billing-v2 Hybrid event clips expire
after the entitlement's `cloud_event_retention_days` (14), an explicit RDM
override (7/14/30) wins, and legacy plans keep their `plans.retention_days`.
The sweeper only runs, and can only delete, once everything below is set.

## Check the host first (no AWS call)

Inside the cloud container:

```bash
python -m recording_retention_sweep --check
```

It prints `{"ready": true|false, "missing": [...]}` and exits 0 only when
ready. The running worker logs `recording_retention.not_enforced
missing=...` at startup on a cloud host that is not ready.

`--check` is configuration only. Cloud Gateway uploads need operational
evidence too (2026-11-05, Codex review of 9ba528b):

```bash
python -m recording_retention_sweep --diagnostics
```

It is read only: no deletion, and no AWS call from this command. It prints:
- `operational`: ready only when a worker has recorded, in
  `recording_retention_sweep_health`, a successful deletion probe in the
  last 26 h and a successful tick within two intervals;
- `workers`: each worker's last probe, tick, error and classification;
- `would_delete` and `classification`: what the next tick would delete
  or hold, and why (`held_historical_ambiguous`,
  `held_historical_lookup_unavailable`, `held_invalid_attribution`,
  `held_no_retention`, `held_bad_timestamp`).

The deletion probe: at worker start and every 24 h, the worker assumes
the lifecycle role and calls `DeleteObject` on
`recordings/_retention_probe/<host>.probe`. Nothing is ever uploaded there,
so no customer data is touched. S3 returns success only when the role,
bucket, region and permission all work. A failed probe clears the
readiness and the worker does not sweep until a later probe succeeds.
If the bucket has versioning enabled, each probe leaves a delete marker
under that prefix, and a "deleted" clip leaves noncurrent versions. A
lifecycle rule for noncurrent versions is then required for retention to
mean physical deletion; confirm it on the bucket.

## Required configuration

| Item | Value | Notes |
|---|---|---|
| `ANYAICAM_RUNTIME_ROLE` | `cloud` (or `combined`) | The sweeper never runs on an appliance. |
| `ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED` | `true` | Off by default; it permanently deletes customer media. |
| `ANYAICAM_RECORDING_RETENTION_SWEEP_INTERVAL_SECONDS` | optional, default `3600`, minimum `300` | |
| `ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN` | `arn:aws:iam::880690594006:role/anyaicam-recording-lifecycle` | Delete-only role, see below. |
| `ANYAICAM_RECORDING_S3_BUCKET` | the bucket the event media and recordings are uploaded to | Same bucket as uploads. |
| `AWS_REGION` (or `AWS_DEFAULT_REGION`) | the bucket's region (`us-east-1` for the current staging account) | |
| `boto3` | installed in the image | |

## IAM (docs/r5-recording-lifecycle-iam.md)

1. Role `anyaicam-recording-lifecycle`, trusted by `anyaicam-ec2-app-role`.
2. Its only permission: `s3:DeleteObject` on `arn:aws:s3:::{BUCKET}/recordings/*`
   (event clips and thumbnails are stored under `recordings/`).
3. `anyaicam-ec2-app-role` gets `sts:AssumeRole` on that role only.

## S3 lifecycle failsafe (backstop, not the 14-day mechanism)

A bucket rule cannot read per-customer retention, so it is only a backstop
for a sweeper that is down for a long time. Note that it would also remove
historical clips the sweep holds as ambiguous, but never before the
longest (30-day) option. Recommended: expire
`recordings/` objects after **60 days** (longer than the longest 30-day
retention option), as in docs/r5-recording-lifecycle-iam.md.

## Before enabling on staging

* The first enabled run deletes **every** Billing-v2 Hybrid event clip already
  older than 14 days (media uploaded before this change was never expired),
  plus legacy media past its plan's retention. Expect a one-time catch-up.
* Verify with an isolated test account: a Hybrid event clip older than 14
  days is removed from S3 and from `detection_event_media`; a 13-day-old clip
  and an AI Local account's media are untouched; a failed S3 delete keeps the
  catalog row for the next tick.
