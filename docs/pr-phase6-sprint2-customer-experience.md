# PR Summary — Phase 6 Sprint 2 Customer Experience

## Summary

Adds the guided customer onboarding and tenant-scoped customer experience on `feature/phase6-sprint2-customer-experience`.

## Changes

- Replaces the basic New Customer form with an eight-step guided wizard.
- Extends tenant onboarding with company contact and site address information.
- Adds an explicit Edge discovery handoff and completion summary.
- Adds a tenant-scoped customer dashboard for camera state, AI events, appliance health, storage, alerts, and subscription information.
- Adds Customer Admin pages for users, sites, cameras, and permissions.
- Keeps camera-level sharing on the tenant-validated authorization service.
- Introduces `app/customer_experience` instead of adding the implementation to `main.py`.
- Adds tenant-isolation and permission regression tests.

## Security

- No authentication or CSRF changes.
- No direct AWS-to-camera communication.
- Customer pages reject platform identities.
- Customer records are selected by the authenticated tenant ID.
- Cross-tenant camera sharing remains denied.

## Verification

- Focused Phase 6 suite: 19/19 passed; CSRF browser-wrapper regression passed.
- Deployment: not performed.
- Merge/tag: not performed.

## Review note

The previously reported legacy onboarding and supporting-table tenant gaps remain prerequisites for approving the complete Phase 6 foundation. This Sprint 2 wizard uses only the new tenant-safe onboarding API.
