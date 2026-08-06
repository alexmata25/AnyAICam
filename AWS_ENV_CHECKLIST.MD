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

- [ ] `ANYAICAM_CLOUDFRONT_URL`

## Phase 13A safety

- [ ] `ANYAICAM_CLOUD_UPLOAD_ENABLED=false`

## Verification

- [ ] `/health` returns 200
- [ ] `/version` reports expected environment and build
- [ ] `/ready` returns 200 after cloud requirements are configured
- [ ] ALB health check uses `/health`
- [ ] CloudWatch receives JSON logs
- [ ] ECS task contains no plaintext credentials
