# Phase 6 Sprint 2 — Customer Experience

## Purpose

Sprint 2 places a guided customer experience on top of the Phase 6 tenant foundation. It does not change authentication, CSRF, licensing enforcement, Cloudflare, Docker networking, or the Edge RTSP boundary.

## Guided onboarding

Platform Owners and Sales users can open `/admin/customers/new`. The wizard collects and reviews:

1. Company information
2. Primary administrator
3. First site
4. Edge appliance assignment
5. Camera discovery handoff
6. Subscription selection
7. Invitation email preparation
8. Completion summary

Submission uses `/api/tenants/onboard` and the transactional `TenantOnboardingService`. The customer, administrator, membership, site, subscription, license, invitation, and appliance assignment therefore share one tenant ID. The configured email abstraction sends or previews the invitation without changing onboarding success if an external provider is temporarily unavailable. Camera discovery remains an Edge operation; the wizard never asks AWS to reach a private RTSP address.

## Customer dashboard

`/customer-portal` reports:

- Cameras online and offline
- Recent tenant-owned AI events
- Edge appliance state and health summary
- Recording metadata and storage consumption
- Recent alerts
- Subscription summary

Every query is filtered by the authenticated customer identity's `tenant_id`.

## Customer administration

Customer Administrators receive dedicated pages:

- `/customer-admin/users`
- `/customer-admin/sites`
- `/customer-admin/cameras`
- `/customer-admin/permissions`

Camera sharing continues through `/tenant/camera-sharing` and the tenant-validated `camera_user_access` service. A user and camera must both belong to the requested tenant before a grant can be saved.

## Module layout

```text
app/customer_experience/
├── __init__.py
├── pages.py       # server-rendered dashboard, tables, and wizard
├── routes.py      # authenticated HTTP routes
└── service.py     # tenant-scoped read models and authorization
```

`main.py` only registers the module and declares navigation entries. Business queries and page construction do not return to the monolith.

## Security boundaries

- Customer identities must have a non-empty tenant ID.
- Tenant authorization runs before role authorization.
- Customer administration operations require Customer Admin capabilities.
- Platform identities are rejected from customer experience routes.
- Dashboard joins include tenant equality where related resources are combined.
- Camera discovery remains local to the assigned Edge Appliance.

## Known follow-up

The legacy partner onboarding endpoint predates the tenant service and should be retired or delegated to `TenantOnboardingService` before the Phase 6 foundation is merged. Sprint 2's new wizard does not use that legacy path.
