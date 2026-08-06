# Phase 6 Multi-Tenant Migration Plan

## Deployment sequence

1. Back up the partner/customer database and persistent configuration volume.
2. Deploy the application image without changing Docker networking, Cloudflare, Edge storage, or camera configuration.
3. On startup, run existing database migrations first and the Phase 6 additive migration second.
4. Verify the migration record `20260806_phase6_multitenant_foundation`.
5. Verify every legacy customer has a tenant and that related sites, cameras, appliances, plans, analytics subscriptions, invitations, events, and sessions carry the same `tenant_id`.
6. Test one platform identity and one customer identity before enabling customer onboarding.

## Backfill rules

- Each existing customer receives one UUID tenant.
- Customer-owned rows inherit the customer's tenant.
- Legacy platform roles map to the platform domain and retain a null tenant.
- Legacy customer roles map to the customer domain and receive one tenant membership.
- Existing camera permissions are copied to `camera_user_access`.
- Existing data is not deleted or moved.

Legacy local JSON users receive explicit domain claims during their existing file migration. Customer identities temporarily use the configured `ANYAICAM_LOCAL_TENANT_ID` compatibility tenant until a later, separately approved migration imports them into the database. This preserves current appliance behavior while preventing an unscoped customer identity.

## New tables

- `tenants`
- `tenant_memberships`
- `tenant_subscriptions`
- `tenant_licenses`
- `recording_assets`
- `ai_events`
- `camera_user_access`

Tenant columns and indexes are added to existing customer-owned tables. The migration is idempotent and additive.

## Verification queries

```sql
SELECT version FROM schema_migrations
WHERE version='20260806_phase6_multitenant_foundation';

SELECT COUNT(*) FROM customers WHERE tenant_id IS NULL OR tenant_id='';
SELECT COUNT(*) FROM partner_users
WHERE identity_domain='customer' AND (tenant_id IS NULL OR tenant_id='');
SELECT user_id, COUNT(*) FROM tenant_memberships
GROUP BY user_id HAVING COUNT(*) > 1;
```

All three count/duplicate checks must return zero rows or zero counts before onboarding is enabled.

## Rollback

Application rollback is safe because legacy `customer_id` and role columns remain populated. Do not drop the new tables or columns during an emergency rollback. Restore the prior image, keep the additive schema, and investigate before any destructive database operation.
