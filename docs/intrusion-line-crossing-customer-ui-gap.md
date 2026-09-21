# Intrusion zone / line-crossing: no real customer-facing drawing UI exists (2026-09-22)

Investigated before starting Intrusion Motion / Line Crossing physical
validation, per explicit instruction not to use `/sites-management` or
any legacy static-JSON page and not to fabricate or restore the old
admin Live view.

## Finding: confirmed -- no multi-tenant customer drawing UI exists

The only functional rule-drawing UI in this codebase is
`GET /analytics/line-crossing` and `GET /analytics/intrusion`
(`main.py`'s `analytics_detail()` -> `analytics_rule_builder()`), reached
via the `/analytics` listing page. Both are **partner/admin-only**, not
customer-facing:

- `/analytics` itself gates on `current_user(request)` +
  `has_permission(user, "view_analytics")` -- the PARTNER portal's own
  role/permission system (`partner_users`), completely separate from
  customer authentication.
- The rule it saves (`AnalyticsRuleModel`, `POST /api/analytics/rules`)
  has **no `customer_id` field at all** -- only `id`, `camera`, `site`,
  `name`, `analytic_type`, `direction`, `sensitivity`,
  `confidence_threshold`, `schedule_start`/`end`, `retention_days`,
  `alerts_enabled`, `geometry`. Rules are stored in one single, local,
  non-tenant-scoped JSON file (`ANALYTICS_RULES_FILE`,
  `analytics_rules.json`) keyed only by camera number.
- The camera dropdown on that page (`get_camera_numbers()`) is not
  scoped to any one customer either.

This is a single-appliance, installer/admin-configured mechanism -- it
happens to work correctly for a single-tenant edge deployment like
Ryzen (camera numbers are unique per-appliance there), but it is not a
real multi-tenant customer self-service feature. Tonight's own People
Counting rule setup was done exactly this way: a direct backend
function call (`customer_analytics_panel.assign_entitlement()` +
inserting the `AnalyticsRuleModel` row directly), not through any
customer-reachable page, because no such page exists.

A second, unrelated page (`main.py` line ~120376, a generic "AI
analytics module" detail template) explicitly shows a "Coming soon"
pill next to "Detection zones: Draw regions or lines over a camera
image -- Not configured", confirming this area of the product is
intentionally unfinished, not something misconfigured or hidden.

The admin rule-builder page's own on-page copy is also stale: "Rule
configuration is real. Event generation remains mock until a
compatible object-detection model is installed." -- untrue today for
`line_crossing` (People Counting genuinely consumes and acts on this
exact geometry, validated live tonight); left unchanged since fixing
copy on an admin-only page is out of scope for this validation pass.

## What this means for tonight

Per explicit instruction: documenting this as a real product gap
(customers cannot self-service draw a line/zone today) rather than
fabricating a UI or reviving the old admin Live view. Line-crossing
physical validation tonight (People Counting) used the backend/API
configuration path directly, matching how this feature is actually
configured today in the absence of a customer UI.

## Recommendation (not implemented tonight)

A real customer-facing drawing UI, if built, would need: (1) a
customer-authenticated route (not the partner `/analytics` path), (2)
`customer_id` added to `AnalyticsRuleModel` and enforced on every read/
write the way every other customer-scoped table in this codebase
already is, and (3) the rules file (or a proper DB table) partitioned
or filtered by tenant so two customers' camera-number rules can never
collide on a shared multi-tenant cloud deployment.
