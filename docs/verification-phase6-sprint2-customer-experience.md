# Phase 6 Sprint 2 Verification Report

## Scope

Verified the guided onboarding UI, customer dashboard read model, customer administration pages, tenant isolation, existing tenant onboarding, camera sharing, and identity-domain integration.

## Automated verification

Command:

```powershell
python -m unittest -v tests.test_customer_experience tests.test_tenant_onboarding tests.test_tenant_migration tests.test_multitenant_integration tests.test_tenancy_policy
```

Result: **20 tests passed**. The existing JavaScript CSRF wrapper regression also passed.

Covered behavior:

- All eight required wizard stages are rendered.
- Wizard submits through the tenant-safe onboarding API.
- Invitation delivery uses the configured preview or SMTP email abstraction.
- Completion links to Edge camera discovery.
- Customer dashboard excludes a second tenant's cameras, events, alerts, and appliances.
- Customer user, site, and camera administration lists are tenant scoped.
- Viewer accounts cannot open Customer Admin user management.
- Cross-tenant camera sharing is rejected.
- Tenant onboarding remains atomic.
- Legacy rows remain covered by the migration backfill test.
- Platform and customer identity domains remain separated.

The repository-wide discovery command was also inspected. It still contains unrelated, pre-existing environment/order-dependent failures involving optional FastAPI dependencies, shared temporary databases, deployment-security expectations, and legacy camera tests. Those failures are outside this Sprint 2 change; the isolated Sprint 2 and tenant regression suites above pass cleanly.

## Manual EC2 checklist

1. Sign in as Platform Owner or Sales and open `/admin/customers/new`.
2. Move through all eight wizard steps and create a test customer.
3. Confirm the tenant, administrator, site, subscription, license, invitation, and appliance rows share the same tenant ID.
4. Sign in as the new Customer Admin and replace the temporary password.
5. Confirm `/customer-portal` loads without platform navigation.
6. Open Users, Sites, Cameras, and Permissions.
7. Confirm a Viewer receives HTTP 403 for `/customer-admin/users`.
8. Assign a camera and confirm another tenant's users and cameras never appear.
9. Start camera discovery from the assigned Edge Appliance and confirm no cloud-to-LAN RTSP request occurs.

## Production status

Not deployed, merged, or tagged. EC2 validation is still required before release consideration.
