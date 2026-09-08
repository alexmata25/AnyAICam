# Customer registration Partner VMS migration

Migration `20260907_customer_registration_requests` adds the authoritative
`customer_registration_requests` table. Existing partner, customer, site,
identity, grant, and audit data is preserved. Existing `users.json` identities
are deliberately not imported or changed.

Public requests begin unassigned and pending. A master administrator may assign
an existing approved partner, or select that partner while approving. Once
assigned, a partner administrator can see and approve only that partner's
requests. Sites are not created during account approval; the normal customer
onboarding workflow creates real sites when their details are known.

Approval inserts the customer, active `partner_users` identity, customer-scoped
`identity_grants` row, request decision, and audit entry in one database
transaction. A failure rolls the entire transaction back. Rejection creates no
identity or grant; rejecting an already approved request revokes the identity
and live grants transactionally.

Before upgrading, back up the configured Partner VMS database. The migration is
additive and can be retried safely through the existing `schema_migrations`
mechanism. Recovery consists of restoring the pre-upgrade database backup.
Dropping the new table is optional when rolling application code back because
older versions do not reference it.
