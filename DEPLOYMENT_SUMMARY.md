# Phase 13A — AWS Deployment Foundation

This build prepares a separate AWS test copy without changing existing VMS features.

## Added

- `local`, `staging`, and `production` deployment identity.
- `edge`, `cloud`, and `combined` runtime roles.
- ECS/ALB-compatible HTTPS and forwarded-header handling.
- Cloud-aware `/health`, `/ready`, and `/version`.
- Structured JSON logs for CloudWatch.
- RDS, S3, SES, Secrets Manager, CloudFront and public URL configuration placeholders.
- Disabled cloud-upload queue/worker placeholders.
- Production Dockerfile, Docker ignore file, ECS Fargate task definition and environment template.
- Authenticated `/api/operations/deployment` diagnostics.

## Runtime roles

- `edge`: Samsung laptop behavior; local cameras, FFmpeg, recording and detection run.
- `cloud`: AWS web/API foundation; camera workers do not run.
- `combined`: both edge workers and cloud configuration are required.

## Not enabled in Phase 13A

- PostgreSQL migration
- S3 uploads
- CloudFront playback
- Camera streaming through AWS
- Changes to authentication, permissions, invitations, portals or UI
