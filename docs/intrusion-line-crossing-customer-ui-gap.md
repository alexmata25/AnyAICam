# Intrusion zone / line-crossing: no real customer-facing drawing UI exists (2026-09-22)

Investigated before starting Intrusion Motion / Line Crossing physical
validation, per explicit instruction not to use `/sites-management` or
any legacy static-JSON page and not to fabricate or restore the old
admin Live view.

## Second finding, added after further investigation: "intrusion" (zone) has NO backend implementation at all -- only "line_crossing" is real

Checked every reference to `analytic_type=="intrusion"`/`"intrusion"`
across `main.py`: every single one is UI plumbing -- a dropdown option,
a filter-lane label, an event-type string in a select box (Events/
Investigate filters, notification-preference options, the rule-builder
page itself). There is no backend consumer anywhere that reads an
`intrusion`-type `AnalyticsRuleModel` row and evaluates it against real
detections, unlike `line_crossing`, whose one real consumer is
`_load_people_counting_rule()`/`people_counting_worker()` -- confirmed
working end-to-end earlier tonight (see the People Counting validation
turns and `docs/people-counting-sampling-rate-gap-report.md`).

In other words: **"Line Crossing" is not a separate analytic from
People Counting -- it is the exact same feature, same rule storage,
same worker.** There is no additional "line crossing" engine to
validate beyond what People Counting's own validation already covered.
"Intrusion" (a drawn zone, 3+ points) is unimplemented on the backend
entirely -- an admin can draw and save an intrusion rule via the
partner-only rule builder, but nothing ever reads it back for real
detection. This matches the "Coming soon" / mock-banner findings above;
it is not a new, separate defect, and it should not be reported as a
runtime bug -- there is no runtime path to be buggy in.

Per standing instruction not to request a physical test overnight:
line-crossing's algorithmic correctness (exactly-once crossing count,
reverse-direction handling, no duplicate counts while lingering near
the line) is already proven by `people_counting.py`'s own 26+
deterministic unit tests (`tests/test_people_counting.py`), and the
integration-level pieces (rule loading, event/timestamp/camera
association, thumbnail, `linked_recording_for()`-based clip linkage) are
the same code paths already validated tonight for every other analytic
event type on this appliance. What remains genuinely unverified is a
live, physical crossing producing a real counted event on Living
Room -- the one People Counting walk-test window tonight was spent
proving/fixing the semaphore-starvation bug and testing facial
recognition instead, and produced zero real `people_counting_in`/`_out`
events. That live confirmation is the correct next daytime/physical-test
item, not a new "intrusion" test.

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

## Update, 2026-09-21: the tenant-safe UI/API/storage foundation above now exists -- execution still does not

Built `app/customer_analytics_rules.py` (new `customer_analytics_rules`
DB table, migration `20260921_customer_analytics_rules`) exactly along the
lines this doc's own Recommendation section called for, as a second Codex
session on this same rule-drawing task hit its session limit before
writing any code (no checkout/commit/bundle was left behind to resume --
confirmed absent before starting fresh from this branch).

What is real and tenant-safe today:

- `customer_analytics_rules` table: `customer_id`, `site_id`,
  `appliance_id`, `camera_id` all present and enforced on every read and
  write (a camera or rule belonging to another customer_id is a 404, never
  a leak or a silent cross-tenant write) -- a genuine second, additive
  store, completely separate from `analytics_rules.json`/
  `AnalyticsRuleModel`. Nothing in this feature reads from or writes to
  the legacy file, and People Counting's own worker/rule storage is
  untouched.
- Customer-authenticated routes under `/api/customer/cameras/{camera_id}/
  analytics-rules` (list/get/create/update/delete) plus a customer-facing
  drawing page at `/customer/cameras/{camera_id}/analytics-rules`, reusing
  this camera's existing relay-only live preview (`.../live/start` +
  `.../live/playlist.m3u8`) as the background to draw over -- a captured
  video frame, not a live feed, is what a line/zone gets drawn onto.
- Real validation: a line-crossing rule requires exactly 2 points and a
  `direction` (`both`/`inbound`/`outbound`); an intrusion rule requires
  3-20 points and rejects a `direction`; every point must be a normalized
  `{x,y}` in `[0,1]`; an empty name is rejected. All fail with a 400 and a
  specific message, never silently clamped or guessed.
- Real permissions: `customer_owner` has full read/write across their own
  fleet, matching every other customer-owner capability in this codebase.
  A `customer_viewer` can read a camera's rules with either `can_live` or
  `can_playback` (i.e. any existing visibility into that camera), but can
  only create/update/delete rules with the existing `can_settings` grant
  for that specific camera -- the same column, and the same owner-vs-
  viewer split, `door_access.py`'s own `can_unlock` gate already
  established for a different per-camera configuration action.
- 21 regression tests (`app/tests/test_customer_analytics_rules.py`)
  covering tenant isolation, camera-ownership validation, create/update/
  delete, invalid geometry, invalid direction, and owner-vs-viewer
  permissions, all passing alongside the full existing suite (3309 tests
  collected, no new failures).

What is explicitly still NOT real, and must not be described as working:

- **No execution path exists for either rule type on this branch.**
  Saving a rule here only persists geometry -- it does not evaluate
  anything, ever. Traced in full: `app/analytics_rules_engine.py` (the
  real IoU tracker, `_point_in_polygon()`/`_signed_distance_to_line()`
  geometry, and per-rule dwell/crossing state machines) exists only on
  the divergent, unmerged `analytics-rules-foundation-20260821` branch,
  is not an ancestor of this branch, and is not wired to anything here --
  grep for `analytics_rules_engine`/`evaluate_rules`/`update_tracker(`
  across this branch returns zero matches.
- Line-crossing detection that genuinely runs today does so only through
  People Counting's own separate worker (`people_counting_worker()`)
  reading People Counting's own rule/line, which this new feature
  deliberately does not touch or share storage with (per explicit
  instruction to keep People Counting separate unless intentionally
  sharing geometry primitives -- it does not, yet).
- Intrusion detection has zero execution anywhere on this branch, in
  either the legacy admin path or this new one.
- Building the real edge-worker execution path -- reading
  `customer_analytics_rules`, running tracking/geometry against live
  frames, and writing real detection events -- remains a separate,
  unbuilt phase. Porting `analytics_rules_engine.py` from the divergent
  branch and re-pointing it at this tenant-scoped table (instead of that
  branch's own still-non-tenant-safe JSON file) is the most direct next
  step, but was not done here per the explicit instruction not to fake
  detector behavior while building this foundation.
