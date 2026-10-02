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

## Update, 2026-09-21 (continued): full runtime-path trace, checkpoint by checkpoint

Traced by direct code inspection (not inference) exactly what happens
after a rule is saved, for both rule types, against the 7 checkpoints
requested: storage -> edge appliance receipt -> worker -> frames/
detections -> event-creation condition -> Event-mode clip link ->
Events/Investigate. Documented here rather than only asserted, and
backed by 4 new regression tests
(`test_creating_updating_and_deleting_a_rule_never_writes_the_local_
analytics_events_file`, `test_creating_and_updating_a_rule_never_
creates_a_detection_events_row`, `test_no_existing_worker_or_sync_
module_reads_the_new_rules_table_yet`, and a page-copy guard against
ever implying live detection) that fail the moment any of these
boundaries starts being crossed without a real implementation behind it.

### First, the architecture this trace depends on (established by code, not assumed)

The appliance and the cloud portal are two separate deployments of this
codebase with two separate SQLite databases, linked only by a small set
of narrow, purpose-built one-way channels -- confirmed directly from
`analytics_sync.py`'s own module docstring: it runs ON the appliance,
reads the appliance's own **local file** (`ANALYTICS_EVENTS_FILE`,
written by `save_yolo_events()`/`append_analytics_event()`), and
forwards each event with an outbound HTTP POST to
`/api/appliance/analytics/{camera_id}/events` -- a route defined in
`appliance_cloud.py` that runs on the **cloud** side and inserts into
the cloud's own tenant-scoped `detection_events` table
(`appliance_cloud.py:834-903`). This sync is gated by
`ANYAICAM_ANALYTICS_SYNC_ENABLED`, one of the six flags this session's
own product-mode work makes default-OFF in Local mode and default-ON in
Hybrid mode. The only channel running the other direction (cloud ->
appliance) found anywhere in this codebase is the coarse
`appliance_commands` table, used today solely to request a `restart_vms`
container restart (e.g. for a product-mode change) -- it carries no
per-camera configuration or rule data of any kind.

`customer_analytics_rules` (this new feature's table) is written and
read exclusively through `partner_db.connection()` -- the same database
every other customer-portal table already uses (`customers`, `cameras`,
`customer_camera_permissions`, `live_view_sessions`, `recordings`,
`detection_events`, etc.). Confirmed by a repository-wide grep: outside
`customer_analytics_rules.py` itself, its own migration entry, and its
own test file, **the string `customer_analytics_rules` appears nowhere
else in this codebase.**

### Line Crossing

1. **Where the saved rule is stored.** Through this new feature:
   `customer_analytics_rules` (real, tenant-scoped SQL table). The only
   OTHER thing called "line crossing" that actually runs today is a
   wholly separate, pre-existing feature -- People Counting's own line,
   stored in the legacy, non-tenant-safe `analytics_rules.json`
   (`ANALYTICS_RULES_FILE`) and read by `_load_people_counting_rule()`.
   These are two disconnected storage locations; a rule saved through
   this new customer UI is invisible to People Counting and vice versa.
2. **How the edge appliance receives it.** For this new feature:
   **no mechanism exists.** There is no code anywhere that reads a
   `customer_analytics_rules` row and moves it onto an appliance for
   local consumption, and (per the architecture section above) there is
   no general-purpose cloud-to-appliance config-push channel it could
   even ride on if one were written. For the legacy People-Counting
   line: "receipt" is a non-issue by construction -- `analytics_rules.json`
   is a local file written directly on the same machine
   `people_counting_worker()` runs on (an admin/technician-configured
   file, not something synced from anywhere).
3. **Which worker/process reads it.** New feature: **none.** Legacy
   People-Counting line: `people_counting_worker()`
   (`app/main.py:38132`), one `asyncio` task per camera, started when
   `PEOPLE_COUNTING_ENABLED` and re-checking entitlement + rule every
   cycle.
4. **What camera frames/detections feed it.** New feature: nothing --
   no worker exists to call anything. Legacy People-Counting line:
   `detect_objects_frame(camera_number)` -- the exact same YOLO
   inference `ai_person_detector()` already uses, invoked under the
   shared `ai_inference_semaphore` so it cannot starve other detectors
   of the one shared model (a real, previously-fixed starvation bug --
   see that function's own 2026-09-22 comment).
5. **What condition creates an event.** New feature: no condition
   exists -- there is no geometry evaluation code path for this table on
   this branch. `app/analytics_rules_engine.py` (the real
   `_signed_distance_to_line()` side-flip/direction state machine) exists
   only on the divergent, unmerged `analytics-rules-foundation-20260821`
   branch and is wired to nothing here. Legacy People-Counting line:
   `people_counting.PeopleCounter.update(centroids)` -- a real,
   deterministic tracker keyed on each detected person's foot-point
   crossing the configured line, direction-aware.
6. **How the event links to the Event-mode recording.** New feature:
   N/A -- no event is ever created. Legacy People-Counting line:
   `linked_recording_for(camera_number, now)` locates the real local
   Event-mode `.mkv` segment covering the crossing's timestamp and
   windows playback to pre-roll + event + post-roll via
   `compute_clip_window()` -- the same clip-linking function every other
   event type in this codebase uses.
7. **How it appears in Events/Investigate.** New feature: it never does
   -- nothing is ever appended anywhere. Legacy People-Counting line:
   `append_analytics_event()` appends the event to the same local
   `ANALYTICS_EVENTS_FILE`; `analytics_events()` reads that file back for
   the Investigate page, which categorizes it under the existing
   `people_counting` filter chip (`_aaco_event_category()`). In Hybrid
   mode only, `analytics_sync.py` additionally forwards that same event
   to the cloud's `detection_events` table (see architecture section
   above) so it is also visible from a remote customer-portal session;
   in Local mode it stays local-only, visible on-device immediately.

### Intrusion

1. **Where the saved rule is stored.** Through this new feature:
   `customer_analytics_rules` (real, tenant-scoped SQL table, `rule_type
   ='intrusion'`). Unlike line-crossing, there is no OTHER intrusion
   storage anywhere that does anything -- the legacy admin rule builder
   can save an `analytic_type="intrusion"` row into
   `analytics_rules.json` too, but nothing has ever read that row back
   for detection (confirmed by this doc's own earlier "second finding"
   section, from before this new feature existed).
2. **How the edge appliance receives it.** No mechanism exists -- same
   finding as line-crossing, and here there is not even a legacy analog
   to fall back on.
3. **Which worker/process reads it.** None. There is no
   `intrusion_worker()` or equivalent anywhere in this codebase, on this
   branch or the legacy path.
4. **What camera frames/detections feed it.** Nothing -- no worker
   exists to call `detect_objects_frame()` or anything else on behalf of
   an intrusion rule.
5. **What condition creates an event.** No condition exists. The real
   dwell-timer/point-in-polygon logic
   (`_evaluate_intrusion()`/`_point_in_polygon()`) exists only on the
   divergent `analytics-rules-foundation-20260821` branch and, even
   there, still reads the same non-tenant-safe JSON file -- it was never
   pointed at a real per-tenant table on any branch.
6. **How the event links to the Event-mode recording.** N/A -- no event
   is ever created, so `linked_recording_for()` is never invoked on
   intrusion's behalf.
7. **How it appears in Events/Investigate.** It never does. A customer
   drawing and saving an intrusion zone today gets confirmation that the
   zone was saved -- nothing more. No alert, no event, no clip, ever,
   for this rule type on this branch.

### The three real, independent gaps between "rule saved" and "customer sees a detection"

Stated separately because each one is its own unit of future work, not
one monolithic "wire it up" task:

1. **No cloud -> appliance delivery channel** for this data type (or any
   per-camera configuration/rule data) -- only the coarse `restart_vms`
   command channel exists in that direction today.
2. **No consuming worker** on the appliance side, even hypothetically --
   confirmed by grep against `people_counting_worker()`, `analytics_
   sync.py`, `appliance_cloud.py`, and `event_media_uploader.py`,
   enforced going forward by
   `test_no_existing_worker_or_sync_module_reads_the_new_rules_table_yet`.
3. **No tenant-safe geometry/tracking engine wired to anything** --
   `analytics_rules_engine.py`'s real implementation is stranded on an
   unmerged branch and, even there, was never pointed at a real
   per-tenant table.

Any one of these being fixed in isolation would still leave "a customer
draws a rule and it does nothing" true. All three need real
implementations, in this order (storage -> delivery -> evaluation),
before either rule type is an operating analytics feature rather than a
drawing tool.

## Update, 2026-09-21 (continued again): all three gaps closed in software

Built the execution backend the three-gap list above called for, in the
same order: delivery, edge persistence, evaluation, event integration.
Nothing here has been deployed or physically validated -- see the final
section below for exactly what that leaves open.

### 1. Cloud -> appliance rule delivery

`GET /api/appliance/configuration` (`appliance_cloud.py`) -- the SAME
route `edge_camera_sync.py`/`recording_uploader.py` already poll every
cycle for camera config/entitlements/product_mode, reused rather than a
new endpoint -- now also returns `analytics_rules`: every `enabled=1`
`customer_analytics_rules` row belonging to the calling appliance's own
cameras, scoped by the exact same `WHERE c.appliance_id=?` join the
route already trusts for `cameras` itself. A disabled rule is never
sent at all (disabled and deleted are deliberately indistinguishable to
the edge). Proven by `test_appliance_analytics_rules_delivery.py`: a
second appliance/customer's rule never appears in the first's poll, a
disabled rule never appears, and an invalid credential reveals nothing.

### 2. Edge rule persistence

`edge_camera_sync.py`'s existing `sync_provisioned_cameras()` (the
established cloud->edge reconciliation pass) now also calls a new
`_reconcile_analytics_rules()`: it fully replaces this appliance's LOCAL
`customer_analytics_rules` table (same schema/table name the cloud
portal writes into -- a real, additive 1:1 mirror, exactly like
`cameras`/`customers`/`sites`/`appliances` already are, never the legacy
`analytics_rules.json`) with exactly what this poll reported. Unlike
camera sync's own deliberate "never delete a camera locally" policy, a
rule's disappearance from the response is trusted immediately (a
disabled/deleted rule must actually stop being enforced) -- but a
malformed or missing `analytics_rules` field in the response leaves
local state untouched entirely, the same fail-safe posture as the
camera list's own `malformed_response` bail-out. Proven by
`test_edge_camera_sync_analytics_rules.py`: upsert, in-place geometry
update, deletion on removal, multi-camera/multi-rule sync, and identity
stamping.

### 3 & 4. Line-Crossing and Intrusion execution, and event integration

Ported `app/analytics_rules_engine.py` (and its own 32-test unit suite)
unmodified from the divergent `analytics-rules-foundation-20260821`
branch -- real per-camera IoU tracking (`update_tracker()`), real
geometry (`_point_in_polygon()`, `_signed_distance_to_line()`), and a
real per-rule state machine (`evaluate_rules()`): a dwell timer for
intrusion (enter -> dwell -> fire once per continuous dwell -> clear on
exit, re-arms only after a real re-entry) and a confirmed-side-flip
detector for line-crossing (fires once per actual crossing, with a
small refire floor against boundary jitter, filtered by the rule's own
`direction`). This is the debounce answer to "one person lingering
does not create endless events" -- proven at the unit level in the
ported suite and again at the integration level (a lingering person
produces zero events before the real dwell threshold).

New `app/customer_analytics_rule_worker.py` -- one asyncio task per
camera, gated by a new `CUSTOMER_ANALYTICS_RULES_ENABLED` master flag
(mirroring `PEOPLE_COUNTING_ENABLED`'s own shape, deliberately
separate), started in main.py's existing per-camera worker-startup
block alongside `people_counting_tasks`. Each cycle: resolves this
camera's real `camera_id` (reusing `recording_uploader._camera_
identity()`'s existing cache -- no new cloud round-trip), loads its
enabled rules from the LOCAL mirror table, and -- only if any exist --
calls the exact same `detect_objects_frame()` every other detector uses
(under the same `ai_inference_semaphore`, avoiding the exact YOLO-
starvation bug People Counting's own worker already hit once), tracks
detections through `analytics_rules_engine.update_tracker()`, and
evaluates them against this camera's rules. A fired event becomes a
real `AnalyticsEventModel` row -- correct `camera`/`timestamp`/
`rule_id`/`track_id`, a real thumbnail (one saved frame per cycle,
reused across every event that cycle, matching every other detector's
convention), and a real Event-mode clip link via the same
`linked_recording_for()` every other event type uses -- appended
through the same `append_analytics_event()` path, which is what makes
it readable back through `analytics_events()`/Investigate with zero
Investigate-page-specific code needed. `event_type="line_crossing"`
deliberately reuses a value that already existed, unreachable, in
main.py's own Investigate/analytics filter dropdowns; `event_type=
"intrusion"` matches the pre-existing category exactly. Both got their
own entry in `_aaco_event_category()`/the client-side `filterCategory()`/
`EVENT_COLORS` map so they render with a real category/color rather
than falling into the generic "All" bucket. Deliberately kept semantically
separate from People Counting throughout -- its own worker, its own
master flag, its own event_type family -- even though both now share
`analytics_rules_engine.py`'s tracking/geometry code.

Proven by `test_customer_analytics_rule_worker.py`: rule loading/
translation, event metadata correctness, Event-mode clip linkage,
real Investigate-path visibility and categorization, an idle-harmless
no-op when no camera/rules resolve, a full real line-crossing cycle
producing a thumbnailed event, and the dwell-threshold debounce holding
under the real worker loop.

### What is now real, and what still is not

Real, tested, in software on this branch: the full path from a
customer-drawn rule through cloud storage, cloud->appliance delivery,
local edge persistence, real per-camera tracking/geometry evaluation,
debounced event firing, and Event-mode/Investigate integration.
Combined regression count for this feature: 161 tests, all passing
alongside the full existing suite.

Still true, and explicitly not claimed otherwise:

- **Nothing has been deployed.** `customer_analytics_rule_worker.py`,
  the extended `appliance_configuration()` response, and edge_camera_
  sync.py's rule reconciliation exist only in this repository -- Ryzen
  is still running whatever it was running before this branch, and
  `CUSTOMER_ANALYTICS_RULES_ENABLED` has not been set true anywhere.
- **No physical validation has been performed.** Every test above uses
  synthetic detections (`detect_objects_frame()` monkeypatched) --
  real-camera geometry accuracy (does a customer's actually-drawn line/
  polygon, viewed through a real lens at a real mounting angle, produce
  the crossing/intrusion decision they expect), real YOLO confidence/
  class behavior against this rule type, and real multi-rule/multi-
  camera CPU cost on real appliance hardware are all unverified.
- `analytics_rules_engine.py`'s dwell/line-side state is per-process,
  in-memory only (matching the ported module's own original design,
  and `people_counting.py`'s own equivalent limitation) -- an appliance
  restart mid-dwell loses that one in-flight state, exactly like a
  restart mid-crossing already does for People Counting.
- A pre-existing, unrelated bug was found (not fixed, out of scope):
  `appliance_configuration()`'s `configuration_version` field computes
  `max()` over camera statuses and raises `TypeError` if two or more of
  an appliance's cameras share a `NULL` status -- surfaced only because
  this work's own test harness was the first to seed two cameras
  through this exact route in one test. Worked around in this feature's
  own tests by giving seeded cameras a real status; left unfixed
  because it is unrelated to rule delivery.
