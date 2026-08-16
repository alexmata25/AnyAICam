# AWS Environment Variable Checklist

## Cloud readiness

- [ ] `ANYAICAM_ENV=staging` or `production`
- [ ] `ANYAICAM_RUNTIME_ROLE=cloud`
- [ ] `ANYAICAM_PUBLIC_URL`
- [ ] `ANYAICAM_DATABASE_URL`
- [ ] `AWS_REGION`
- [ ] `ANYAICAM_S3_BUCKET`
- [ ] `ANYAICAM_S3_PREFIX`
- [ ] `ANYAICAM_SECRETS_SECRET_ID`
- [ ] `ANYAICAM_PORTAL_SECRET`
- [ ] `ANYAICAM_SECURE_COOKIES=true`
- [ ] `ANYAICAM_FORCE_HTTPS=true`
- [ ] `ANYAICAM_TRUST_PROXY_HEADERS=true`

## SES

- [ ] `ANYAICAM_SES_SENDER`
- [ ] `ANYAICAM_SMTP_HOST`
- [ ] `ANYAICAM_SMTP_PORT`
- [ ] `ANYAICAM_SMTP_USERNAME`
- [ ] `ANYAICAM_SMTP_PASSWORD`
- [ ] `ANYAICAM_SMTP_FROM`

## Future playback

- [x] `ANYAICAM_CLOUDFRONT_URL=https://d31cxfv0l904ar.cloudfront.net`
  - CloudFront distribution ID: `E1LBM1M5362SXK` (docs/AI_HANDOFF.md §8, Phase 5 result).
  - Distribution currently remains **DISABLED** intentionally, until viewer authorization/signing (trusted key group, signed URL/cookie minting) is implemented and separately authorized.
  - Checking this off means the CloudFront endpoint now exists and is known — it does **NOT** mean live viewer traffic is enabled.

## RDM-2 (Remote Device Management) readiness

- [ ] `ANYAICAM_REMOTE_UPDATE_ENABLED=true` (Group 2D — cloud update-result reporting endpoint)
- [ ] `ANYAICAM_UPDATES_SOURCE_ENABLED=true` (Group 2G — authenticated manifest endpoint)
- [ ] `ANYAICAM_UPDATES_S3_BUCKET` (Group 2G — dedicated, private updates bucket; separate from `ANYAICAM_S3_BUCKET` above)
- [ ] `ANYAICAM_UPDATES_S3_REGION` (Group 2G — optional; defaults to `AWS_REGION`)
- [ ] `ANYAICAM_UPDATES_PRESIGNED_TTL_SECONDS` (Group 2G — optional; defaults to 1800)
  - **RDM-2 software (Groups 2A–2I) is complete, committed, documented, and pushed** to `origin/build/v1.2-modular-foundation` at `5fceca8b9f57f83b6baa33f4325ad4728eb0bfa7` (docs/AI_HANDOFF.md §11). Group 2I is the final RDM-2 subgroup — no Group 2J is planned.
    - Final code follow-up commit: `989fb6bf9f1c76830c6b3dd2d37f1d8784d01e9d` — "RDM-2: populate rollback metadata in update results."
    - Final RDM-2 documentation commit: `5fceca8b9f57f83b6baa33f4325ad4728eb0bfa7` — "docs: record RDM-2 Group 2I completion."
    - The code now covers the complete remote-update pipeline: source/publish, authenticated manifest serving, device pull, signature/hash verification, install/activation, restart/resume, health validation, rollback, and cloud result persistence.
    - Group 2I corrected `rollback_from` metadata for both `ROLLED_BACK` and `ROLLBACK_FAILED`.
    - Final verification: 324/324 `appliance-agent` tests, Group 2H E2E 3/3, updater state-machine 61/61; `app` sanity suite 261 passed with the same 4 pre-existing environment-only errors.
  - Checking the boxes above off means these env vars are actually configured in a real environment — it does **NOT** mean any of it has happened yet:
    - No RDM-2 deployment has been performed.
    - No RDM-2 AWS resources (including the updates S3 bucket) were created or modified during Groups 2A–2I.
    - No new IAM roles/policies were applied for the RDM-2 updates path.
    - The updates S3 bucket/infrastructure must not be marked deployed merely because the software is complete.
    - No real appliance/service restart was performed for RDM-2.
    - Tier 2 (live-loopback HTTP) testing also remains deferred (docs/AI_HANDOFF.md §11, Groups 2H/2I).

## Phase 13A safety

- [ ] `ANYAICAM_CLOUD_UPLOAD_ENABLED=false`

## Verification

- [ ] `/health` returns 200
- [ ] `/version` reports expected environment and build
- [ ] `/ready` returns 200 after cloud requirements are configured
- [ ] ALB health check uses `/health`
- [ ] CloudWatch receives JSON logs
- [ ] ECS task contains no plaintext credentials
