# Phase 6 Sprint 1 Verification

## Automated coverage

- Legacy-to-canonical identity mapping.
- Tenant check before role permission.
- Cross-tenant access rejection.
- Explicit platform camera-data permission.
- Customer Admin camera-sharing authority.
- Viewer camera grants.
- Platform/customer navigation separation.
- Transactional creation of tenant, administrator, site, subscription, license, appliance, and invitation.
- Rejection of cross-tenant camera sharing.
- Main login integration for database identities.
- Migration ordering and modular route registration.

Local result on 2026-08-06: 54 focused Python regression tests passed across Phase 6, authentication/CSRF/logout, administrator pages, Edge discovery, Edge streaming, camera provisioning/health, and partner-account compatibility. The browser-wrapper JavaScript regression also passed all relative and absolute same-origin login variants.

The repository-wide one-process discovery command is not the release command because several older database tests mutate `ANYAICAM_PARTNER_DB` while sharing the already-imported `partner_db` module. Those suites pass in their intended isolated processes. Existing unrelated baseline assertions for the camera-settings test harness, PWA route naming, customer mobile CSS, and server-header implementation remain outside this branch and are not masked by this report.

## Required EC2 validation

1. Run the focused Phase 6 tests.
2. Run the existing authentication, CSRF, logout, Edge, streaming, administrator-page, and camera-settings regression suites.
3. Build the Docker image.
4. Start only the application container in the test environment.
5. Sign in as a platform Owner and verify the New Customer page.
6. Create a test customer and replace the temporary password through the unified login.
7. Sign in as the Customer Admin and confirm platform URLs return 403.
8. Create a Viewer, grant one camera, and verify other tenant cameras remain unavailable.
9. Sign in as Support and verify customer video/event/playback URLs return 403.

No production merge, deployment, or release tag is part of this branch.
