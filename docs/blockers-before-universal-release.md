# Blockers before universal release

Tracked issues that must be resolved before AnyAiCam-VMS ships as a
single universal release across all appliances (Ryzen, Samsung, future
mini PCs). Each entry records what was found, the evidence, and the
direction of the fix -- not an implementation, unless noted.

## Edge appliances must not maintain independently authoritative customer passwords

**Found:** 2026-08-28, while unblocking live camera streaming on the
Samsung appliance.

**Problem:** `RUNTIME_ROLE=edge` appliances (confirmed on Samsung) run
the full application stack locally, including a fully self-contained,
independently-writable `partner_users` table in their own local SQLite
database. `POST /api/partner-login` (`app/partner_portal.py`) never
delegates or checks against any cloud identity service -- it is 100%
local. This means the same customer email can end up with **different
password hashes on different appliances**, silently, with no sync or
reconciliation.

**Confirmed in practice:** a customer (`alexmata25@gmail.com`) reset
their password successfully at `app.anyaicam.com` (a separate,
Cloudflare-fronted production environment) and could log in there, but
the identical login against Samsung's local `/api/partner-login`
returned `403` / `reason: invalid` -- because Samsung's local
`password_reset_tokens` table had **zero rows, total**, proving the
reset never touched Samsung's database at all. The two environments'
`partner_users` rows for the same email had simply drifted apart.

**Direction for the real fix:** this codebase already has a working
precedent for exactly this shape of problem -- the appliance's own
identity is cloud-delegated (`appliance_identity.py`: signed manifest,
cloud-delegated auth, revocation reconciliation), not locally
authoritative. Customer authentication for `RUNTIME_ROLE=edge` should
follow the same pattern: the edge role must stop treating its local
`partner_users` table as authoritative for customer login, and instead
verify/delegate against the canonical cloud identity service, with any
local data retained only as a controlled cache for offline/local
operation -- never a second, independently-writable password store.

**Explicitly not done today:** as a strictly-scoped development
unblock (approved separately), Samsung's local `password_hash` for
this one account was synchronized to match the working cloud password,
via the application's own `password_hash()` helper, with an
before/after verification that email, role, `customer_id`,
`approved`/`account_status`, and `identity_grants` were all otherwise
unchanged. That is a one-account, one-appliance patch, not a fix for
the underlying architecture -- the next appliance provisioned, or the
next password reset on any existing one, will hit the exact same
divergence until the delegation redesign above is implemented.
