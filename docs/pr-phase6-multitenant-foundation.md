# PR Summary — Phase 6 Multi-Tenant Foundation

## Summary

Introduces two independent identity domains behind the existing unified AnyAiCam login, tenant-first authorization, separate navigation, an additive tenant schema, transactional New Customer onboarding, and tenant-safe camera sharing.

## Key changes

- Added canonical platform and customer roles with legacy aliases.
- Added defense-in-depth domain route restrictions.
- Added explicit platform customer-camera-data permission.
- Added persistent tenant associations and legacy backfill.
- Added the modular `app/tenancy` package instead of expanding `main.py` with tenant business logic.
- Added first-login permanent password replacement for database-backed invitations.
- Preserved the CSRF middleware, same-origin fetch wrapper, Edge ingestion, streaming, licensing, Cloudflare, and Docker networking.

## Review focus

- Role permission matrix and the Owner-only initial camera-data exception.
- Tenant migration/backfill against an EC2 database copy.
- Customer Admin camera grant behavior.
- Compatibility behavior for legacy JSON identities.

## Release note

This is a foundation change, not a production release. Merge only after EC2 migration and smoke-test approval.
