# Local vs Hybrid product mode (2026-09-21)

Built and tested in this branch. **Nothing on the live Ryzen appliance has been changed** -- it keeps running exactly as it does today until someone deliberately applies the migration steps in this doc.

## What exists now, before this work

Every cloud-dependent edge behavior (analytics sync, event-media upload, facial-embedding sync, live relay, bulk recording upload, P2P signaling) was gated by its own independent `ANYAICAM_..._ENABLED` env var, each defaulting to `false` in code. There was no single "what kind of deployment is this" concept -- an operator had to set (or not set) six separate flags by hand, and nothing tied those flags to what the customer had actually purchased (Local one-time vs Hybrid recurring camera-slot capacity).

The live Ryzen pilot unit currently has five of those six flags set to `true` explicitly in its `docker-compose.yml` environment (confirmed by reading the running container, 2026-09-21) -- i.e. it is running in a Hybrid-like configuration today, regardless of what its customer record says.

## What this branch adds

### 1. `app/product_mode.py` -- the resolver

One authoritative mode, `"local"` or `"hybrid"`, resolved in this order:

1. **`ANYAICAM_PRODUCT_MODE` env var** -- an explicit installer/manual choice. This is also the only option for a genuinely air-gapped Local install that never reaches the cloud to learn a mode any other way.
2. **Persisted state file** (`/opt/anyaicam/data/config/product_mode.json` by default, overridable via `ANYAICAM_PRODUCT_MODE_STATE_FILE`) -- the mode this appliance last learned from the cloud.
3. **Unset/legacy** (`""`) -- no mode configured at all. Every governed flag then falls back to its own original hardcoded default (`false`, unchanged).

`product_mode.resolve_cloud_flag(env_var, legacy_default=False)` is the single function every governed flag now calls instead of a raw `os.environ.get(...)`:

- An **explicitly-set env var always wins**, byte-for-byte the same as before this module existed.
- Otherwise, **Local mode defaults the flag to `False`**, **Hybrid mode defaults it to `True`**.
- With **no mode configured**, it returns `legacy_default` (`False` for every flag here) -- so an appliance that has never opted in behaves identically to today.

This is why introducing this module is a **zero-behavior-change event** for the live Ryzen unit and every other already-deployed appliance: none of them set `ANYAICAM_PRODUCT_MODE`, so `current_mode()` returns `""` and every flag keeps reading its own explicitly-set env var exactly as before.

### 2. Six flags migrated to the resolver

| Flag | Module | Governed? |
|---|---|---|
| `ANYAICAM_ANALYTICS_SYNC_ENABLED` | `analytics_sync.py` | Yes |
| `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` | `event_media_uploader.py` | Yes |
| `ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED` | `facial_embedding_sync.py` | Yes |
| `ANYAICAM_LIVE_RELAY_ENABLED` | `live_relay_uploader.py` | Yes |
| `ANYAICAM_RECORDING_UPLOAD_ENABLED` | `recording_uploader.py` | Yes |
| `ANYAICAM_LIVE_P2P_ENABLED` | `webrtc_publisher.py` (edge-side worker gate) | Yes |

**Deliberately NOT governed** (see `product_mode.py`'s own docstring for the full reasoning):

- **Local recording, playback, LAN live view, on-device analytics** -- no flag exists for these today; they simply run, in both modes, unaffected by construction.
- **`ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED` / `ANYAICAM_LOCAL_STORAGE_AUTO_DELETE_ENABLED`** -- pure local disk housekeeping, not a cloud dependency.
- **`ANYAICAM_LPR_ENABLED` / `ANYAICAM_FACIAL_RECOGNITION_ENABLED` / `ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED`** -- these gate paid analytics *entitlements* (Advanced Analytics, Face Access), independent of Local vs Hybrid camera-slot tier. A Local customer can buy Face Access without that implying Hybrid.
- **`appliance_cloud.py`'s own same-named flags, and `live_view_p2p.py`'s `LIVE_P2P_ENABLED`** -- these are cloud-side platform kill-switches (does the multi-tenant cloud deployment accept/serve a given request type from *any* appliance at all), a completely different axis from one customer's product mode. Left reading the raw env var directly, unchanged.

### 3. P2P live view: confirmed Hybrid-only, no LAN-only path exists today

`live_view_p2p.py`'s own module docstring: "SIGNALING ONLY" -- `webrtc_publisher.py` (the edge-side bridge) polls `ANYAICAM_CLOUD_URL` for pending offers/ICE candidates; there is no local/mDNS signaling alternative anywhere in this codebase. Per your instruction, this makes `ANYAICAM_LIVE_P2P_ENABLED` a Hybrid-only governed flag (defaults off in Local), not "Local, always on." Plain LAN live view (opening the appliance's own web UI on the same network) has no such dependency and is unaffected either way -- P2P and the S3/CloudFront relay are both specifically the *remote*-access enhancement layered on top of that baseline.

### 4. Entitlement-driven mode, not an admin toggle

`customer_entitlements.product_mode_for_customer(customer_id)` derives the mode from the customer's real, Stripe-verified camera-slot entitlement:

- Active `camera_slots_hybrid` row -> `"hybrid"`
- Else active `camera_slots_local` row -> `"local"`
- Neither -> `""` (never guessed)

`upsert_entitlement()` is idempotent per `(customer_id, product)`, so a customer's original one-time Local purchase and a later Hybrid subscription are two independent rows that can coexist -- Hybrid wins the moment it's active, with no separate step to first retire the Local row.

`GET /api/appliance/configuration` (the cloud route every appliance already polls periodically for camera config) now includes a `product_mode` field sourced from this function. `edge_camera_sync.py`'s existing poll loop reads it and calls `product_mode.persist_mode(...)`, which writes the state file only when the mode actually changed and returns `True` in that case (surfaced in `sync_provisioned_cameras()`'s result as `product_mode_restart_required`).

**This is the whole upgrade-is-billing-driven loop, now closed end to end in code, including the automatic restart (see §4a below):**

```
Customer clicks "Upgrade to Hybrid"
  -> POST /api/customer/camera-slots/checkout {plan_type: "hybrid", tier_label: ...}
     (create_camera_slot_checkout() -- already existed, reused as-is)
  -> Stripe Checkout -> webhook -> upsert_entitlement(product="camera_slots_hybrid")
     (already existed, reused as-is)
  -> product_mode_for_customer() now returns "hybrid"
  -> next GET /api/appliance/configuration poll (edge_camera_sync.py, ~60s cadence):
     - cloud side (appliance_cloud.appliance_configuration()) detects
       appliances.last_reported_product_mode != "hybrid", computes
       product_mode.describe_transition("local","hybrid") -> 6 changed
       flags, updates last_reported_product_mode, queues ONE
       appliance_commands row (command="restart_vms", created_by=
       "product-mode-transition"), logs old->new + changed flags,
       records an audit_logs "appliance.product_mode_changed" entry
     - edge side: response includes product_mode="hybrid" ->
       edge_camera_sync.py calls persist_mode("hybrid"), writing the
       local state file
  -> appliance's own agent (anyaicam_agent) polls GET /api/appliance/commands
     (its own existing, unrelated poll cycle), receives the queued
     restart_vms command, writes a pending_actions marker (already-
     existing, already-tested _queue_privileged_action())
  -> anyaicam-privileged-watcher.service (host-side, root, already
     existing) picks up the marker after a 10s cancellation grace
     window and runs `docker compose --project-directory /opt/anyaicam
     up -d` -- recreates the anyaicam-vms container only because its
     resolved env actually changed, never a host reboot
  -> the new container's process-start re-reads the persisted mode ->
     every governed flag now resolves to its Hybrid default
```

### 4a. Automatic restart -- now wired

`appliance_cloud._queue_product_mode_restart()` (called from inside `appliance_configuration()`, in the same `connection()` already open for cloud_policy/storage_policy/identity) is the piece that closes the loop above. It:

- Compares this specific appliance's `appliances.last_reported_product_mode` (a new column, migrated in `db_migrations.py`) against the freshly-computed `product_mode_for_customer()` value.
- Calls `product_mode.describe_transition(old, new)` to compute which of the six governed flags would actually flip by default -- an empty result (e.g. a customer's very first Local purchase, `"" -> "local"`, which changes nothing since both resolve every flag to `False`) updates the tracking column but queues nothing.
- On a real change, queues **exactly one** `restart_vms` `appliance_commands` row -- reusing the existing, already-fully-wired, already-tested edge/agent/watcher path (`appliance-agent/anyaicam_agent/commands.py` -> `appliance-agent/system/privileged_watcher.py`), the same `docker compose up -d` action an admin's manual "Restart VMS" button would trigger. No new command type, no new agent code, no new watcher dispatch entry.
- Logs `product_mode.transition appliance_id=... old_mode=... new_mode=... changed_flags=[...] command_id=...` at `WARNING` (visible without raising log verbosity) and records an `audit_logs` entry (`appliance.product_mode_changed`) with the same old/new/changed-flags/command-id detail, addressing "log old mode -> new mode and which services/features changed."

**Loop/duplicate prevention, two layers:**

1. **Primary**: `last_reported_product_mode` is updated the moment a transition is detected, so the very next poll's `new_mode == stored` comparison is `False`-equivalent (no-op) -- no restart is queued again until a genuinely different mode is reported. This means any number of poll cycles with an unchanged mode queue nothing beyond the original one restart (tested: `test_repeated_polls_with_an_unchanged_mode_never_queue_a_second_restart`, 5 consecutive polls).
2. **Secondary**: an explicit `SELECT ... WHERE command='restart_vms' AND status IN ('pending','delivered')` dedup check (mirroring `queue_command()`'s own existing "identical command already queued" guard immediately below it in the same file) refuses to queue a second `restart_vms` for this appliance if one from an earlier transition hasn't been consumed yet -- covers the edge case of the mode flipping again before the agent/watcher has processed the first one (tested: `test_a_rapid_flip_before_the_first_restart_is_consumed_does_not_double_queue`).

`confirmed: true` is set in the queued command's payload deliberately, not as a bypass of a human safety gate -- `_queue_privileged_action()` on the agent side requires it before it will even write the marker (defense-in-depth, matching the cloud's own `queue_command()` requiring it from an admin's request body). Here, the customer's own completed Stripe purchase or cancellation **is** the confirmation; there is no separate human-in-the-loop step for an entitlement-driven mode change, by design -- this is what "without manual SSH" means.

A cancelled entitlement with no fallback (`product_mode_for_customer()` returns `""`, "Decision B" below) deliberately neither updates `last_reported_product_mode` nor queues a restart -- the appliance keeps running its last real mode, untouched, until a real mode is reported again (tested: `test_a_lapsed_entitlement_never_clears_or_restarts`).

### 4b. Hot-reload vs. restart-required, per service

**None of the six governed services can hot-reload today -- all require a restart.** This is a structural fact about the current code, not a choice this feature made:

| Service | Flag | Hot-reload? | Why |
|---|---|---|---|
| `analytics_sync.py` | `ANYAICAM_ANALYTICS_SYNC_ENABLED` | No | Read into a module-level constant once at import; `main.py`'s startup event decides whether to `asyncio.create_task()` the worker AT ALL based on that constant's value at that moment. |
| `event_media_uploader.py` | `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED` | No | Same shape as above. |
| `facial_embedding_sync.py` | `ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED` | No | Same shape as above. |
| `live_relay_uploader.py` | `ANYAICAM_LIVE_RELAY_ENABLED` | No | Same shape as above. |
| `recording_uploader.py` | `ANYAICAM_RECORDING_UPLOAD_ENABLED` | No | Same shape as above. |
| `webrtc_publisher.py` | `ANYAICAM_LIVE_P2P_ENABLED` | No | Same shape as above (gate checked once per worker invocation, but the underlying constant it checks is still import-time-frozen). |

None of these workers' own loop bodies re-check their flag on each iteration -- the gate is evaluated once, before the loop/task is even created, and never again for the life of the process. Making any of them genuinely hot-reloadable would mean changing each worker's own loop to re-read `product_mode.resolve_cloud_flag(...)` fresh every iteration instead of trusting a cached import-time constant -- a real, separate change to each of the six modules, out of scope here. This is why a restart is the correct and only mechanism today, and why this feature's entire design centers on making that restart automatic, safe, and non-repeating rather than trying to avoid it.

### 5. Admin/partner portal: "Upgrade to Hybrid" button

Added to the customer setup/review page (`partner_workspace.py`), shown only when the customer currently has an active Local entitlement (never shown to an existing Hybrid customer, never alongside the "no plan yet" first-purchase panel) and only when a real Stripe Price ID is configured for the equivalent-capacity Hybrid tier (same fail-closed discipline as the existing "Buy camera capacity" panel). It calls the exact same `/api/customer/camera-slots/checkout` endpoint with `plan_type: "hybrid"` -- no new checkout logic, no separate billing path, genuinely entitlement/billing-driven as required.

### 6. Tests

- `tests/test_product_mode.py` (19 tests) -- resolver logic: env-var-always-wins, mode defaults, legacy fallback, persist/no-op/change detection, an empty-string env var treated as unset (matching every flag's pre-existing behavior).
- `tests/test_product_mode_cloud_config.py` (17 tests) -- `product_mode_for_customer()` directly; the real `GET /api/appliance/configuration` route end to end (TestClient), including the upgrade-flips-the-field-immediately scenario and per-customer isolation; and the automatic-restart regression suite:
  - `test_local_to_hybrid_upgrade_queues_exactly_one_restart_vms_command`
  - `test_hybrid_to_local_downgrade_also_queues_a_restart`
  - `test_repeated_polls_with_an_unchanged_mode_never_queue_a_second_restart` (5 consecutive polls, one command)
  - `test_a_rapid_flip_before_the_first_restart_is_consumed_does_not_double_queue`
  - `test_first_ever_local_purchase_queues_no_restart` (`"" -> "local"` changes nothing)
  - `test_a_lapsed_entitlement_never_clears_or_restarts` (Decision B case)
  - `test_product_mode_changed_audit_entry_is_recorded`
- All six migrated worker modules' existing test suites, plus every test file touching `appliance_commands`/`queue_command()`/db migrations (501 tests total across the full combined run) re-run clean.
- `tests/test_edge_camera_sync.py` updated (5 assertions) for the `product_mode_restart_required` key in `sync_provisioned_cameras()`'s result.

19 pre-existing test failures were found and confirmed **unrelated** to this work (verified by reverting each changed file individually and re-running): 19 in `test_recording_uploader_*` (this dev machine's shell has no `AWS_REGION` set) and 26 in `test_camera_discovery_provisioning.py` plus 7 in the customer-setup-page test files (a pre-existing "licensed for 0 cameras" / stale-assertion issue in this environment, reproduced identically on the unmodified `main` branch state).

A real SQLite concurrency bug was found and fixed during this work: calling `audit()` (which opens its own `connection()`) from inside the still-open write transaction already used to update `last_reported_product_mode` and insert the `appliance_commands` row deadlocked with `sqlite3.OperationalError: database is locked` on every test run until `_queue_product_mode_restart()` was changed to return the audit payload for the caller to record only after the `with connection() as db:` block closes.

## What's still a decision (not built yet)

### A. Hybrid -> Local downgrade when no prior Local entitlement exists

`product_mode_for_customer()` returns `""` (not `"local"`) for a customer whose Hybrid subscription is cancelled and who never held a `camera_slots_local` entitlement (e.g. a customer who started on Hybrid from day one). This is a genuine, flagged business decision, not something silently assumed:

- **Option 1**: cancelling Hybrid automatically grants an equivalent one-time Local entitlement (same `camera_slot_maximum`), so the customer keeps their paid-for camera-slot capacity and their appliance falls back to `"local"` mode with cameras/recordings/analytics settings untouched.
- **Option 2**: cancelling Hybrid leaves the customer with no camera-slot entitlement at all (`total_camera_slots()` returns 0) until they make a fresh purchase -- harsher, but never silently grants something never paid for.

Nothing in this branch decides between these -- `product_mode_for_customer()` reports `""` honestly in that case, matching this codebase's existing "never guess, fail closed" discipline for every other resolver (`resolve_tier()`, `resolve_addon()`).

In either case, **no local data is ever deleted** by a downgrade: `product_mode.persist_mode("local")` only flips which cloud-dependent workers start on next restart. Cameras, recordings, analytics settings (`analytics_subscriptions`, `camera_analytics_entitlements`), and all local files under `/app/recordings` are completely untouched by any code path this branch adds -- none of it writes to those tables or deletes files.

## Exact steps to migrate the live Ryzen appliance (not yet performed)

1. **Confirm the customer's real entitlement first.** Query `customer_entitlements` for this Ryzen unit's `customer_id` -- if it has no `camera_slots_hybrid` row (likely, since this is a pilot unit predating this billing model), decide whether to backfill one before flipping the appliance, or set `ANYAICAM_PRODUCT_MODE=hybrid` explicitly as a manual override (bypassing entitlement-driven resolution entirely -- `current_mode()` prefers the env var first).
2. **Pull this branch's code** onto the Ryzen unit (or wait for it to merge through the normal release path) -- `product_mode.py` must exist before any flag migration takes effect; deploying it alone changes nothing (see "zero-behavior-change" above).
3. **Decide the target mode.** Given the pilot unit's current live config (`ANALYTICS_SYNC_ENABLED=true`, `EVENT_MEDIA_UPLOAD_ENABLED=true`, `FACIAL_EMBEDDING_SYNC_ENABLED=true`, `LIVE_RELAY_ENABLED=true`, `LIVE_P2P_ENABLED=true`, `RECORDING_UPLOAD_ENABLED=false`), it is already Hybrid-shaped except for bulk recording upload. Setting `ANYAICAM_PRODUCT_MODE=hybrid` and then **removing** the five individual `_ENABLED=true` overrides would make the mode itself the authoritative source going forward (recommended) -- or leave the explicit flags in place indefinitely, since they always win over the mode regardless.
4. **Restart `anyaicam-vms`** to apply -- either manually (`docker compose --project-directory /opt/anyaicam up -d`) or, once both the cloud deployment and Ryzen's own `anyaicam-vms` are actually running this branch's code (see "Is the automatic path usable yet?" below), by giving the customer a real `camera_slots_hybrid` entitlement and letting the automatic loop in §4a queue it. Verify via `docker exec anyaicam-vms env | grep ANYAICAM_.*ENABLED` (should show nothing, if you removed the explicit overrides) and confirm each worker's own startup log line still shows it running (or not) as intended.
5. **To test Local instead**: set `ANYAICAM_PRODUCT_MODE=local`, remove the explicit overrides, restart, and verify: local recording continues (check `/app/recordings` for fresh files, exactly as confirmed live today), live view works from the LAN, and `docker logs anyaicam-vms` shows no further `analytics_sync.http_call_begin` / `event_media.registered` / `live_relay.segment_upload_*` lines (the three cloud-dependent behaviors confirmed actively running today).
6. **Roll back** at any point by re-setting the explicit `_ENABLED` env vars to their prior values (they always override the mode) or unsetting `ANYAICAM_PRODUCT_MODE` entirely (falls back to whatever the explicit flags say, i.e. today's exact behavior).

No step above has been performed. This is the plan, not an action taken.

## Is the automatic path usable on Ryzen for a real validation test yet?

**Checked live on Ryzen (read-only, 2026-09-21), independent of this branch's code:**

- `anyaicam-agent.service`: `active` and `enabled`.
- `anyaicam-privileged-watcher.path`: `active` and `enabled` (the systemd path-watch unit that arms `anyaicam-privileged-watcher.service` on a new marker file).
- `anyaicam-privileged-watcher.service` itself: `inactive` -- this is its normal resting state (a oneshot triggered by the `.path` unit, not something that stays running).

**The mechanism itself is alive and ready on the real hardware right now.** But two things this feature depends on have not been deployed anywhere yet:

1. **The cloud deployment** (`portal-staging.anyaicam.com`, or whatever serves this Ryzen unit's `ANYAICAM_CLOUD_URL`) is not running this branch's code -- `_queue_product_mode_restart()` only exists in this git branch. Until the cloud side is deployed with it, `GET /api/appliance/configuration` will keep returning its current shape with no `product_mode` field at all, and nothing will ever queue a `restart_vms` command automatically no matter what this customer's entitlement says.
2. **Ryzen's own `anyaicam-vms` container** is not running this branch's code either -- `product_mode.py` and the six migrated flags don't exist on the box yet (migration step 2 above).

**Recommendation: yes, Ryzen can be safely used for a real Local-mode validation test, but only via the manual path (steps 2-5 above), not the fully-automatic entitlement-triggered path, until both deployments happen.** The manual path is low-risk and has a clean rollback (step 6): local recording, cameras, and analytics settings are never touched by any code this branch adds (confirmed by code review -- no path in `product_mode.py`, the six migrated flags, or `_queue_product_mode_restart()` writes to `analytics_subscriptions`, `camera_analytics_entitlements`, `cameras`, or any recording file), and the four cloud-dependent behaviors currently confirmed live (analytics sync, event-media upload, facial-embedding sync, live-relay-to-S3) are exactly what a Local-mode restart is designed to stop. Once this branch is deployed to both the cloud and the Ryzen edge, the same validation could be repeated end-to-end through the automatic path (grant/revoke a test entitlement and watch the restart happen with no SSH), which would additionally prove out the `restart_vms` command's full round trip on real hardware for the first time -- it is currently exercised only by this branch's own test suite (mocked SQLite, no real agent/watcher involved) and, per the code audit above, has never had a producer anywhere in the codebase until now.

## Full entitlement-driven migration + rollback plan (2026-09-21) -- NOT PERFORMED

This is the complete, exact plan for switching Ryzen from its current Hybrid-shaped state to true Local mode via the **automatic, entitlement-driven** path built in this branch (`_queue_product_mode_restart()` in `appliance_cloud.py`), rather than the manual env-var override. Nothing below has been executed.

### 1. Code that must reach the cloud deployment first

The cloud process serving this Ryzen unit's `ANYAICAM_CLOUD_URL` (`https://portal-staging.anyaicam.com`, confirmed live) must be running a build that includes:
- `app/product_mode.py` (the `describe_transition()` helper the cloud route calls).
- `app/customer_entitlements.py`'s `product_mode_for_customer()`.
- `app/appliance_cloud.py`'s updated `appliance_configuration()` route and new `_queue_product_mode_restart()` function.
- `app/db_migrations.py`'s new `appliances.last_reported_product_mode` column migration -- **this must run against the real cloud database** before the first poll after deployment, or the very first `UPDATE appliances SET last_reported_product_mode=...` will fail with `sqlite3.OperationalError: no such column` (or the Postgres equivalent). Confirm with a schema check against the live cloud DB, not assumed from a successful local test run.

Verify with: `curl https://portal-staging.anyaicam.com/api/appliance/config` should still respond normally (this route is unauthenticated and unrelated, just a smoke test the deployment is up), and a real appliance's own next `GET /api/appliance/configuration` response should include a `"product_mode"` key at all (even `""`) -- its total absence means the old build is still serving.

### 2. Code that must reach Ryzen

Ryzen's own `anyaicam-vms` container needs:
- `app/product_mode.py`.
- The six migrated modules: `analytics_sync.py`, `event_media_uploader.py`, `facial_embedding_sync.py`, `live_relay_uploader.py`, `recording_uploader.py`, `webrtc_publisher.py`.
- `app/edge_camera_sync.py`'s updated `sync_provisioned_cameras()` (the `persist_mode()` call).

Verify with: `docker exec anyaicam-vms python3 -c "import product_mode; print('ok')"`.

**Critical manual step, same deployment window** -- remove these five lines from `/opt/anyaicam/docker-compose.yml`'s `anyaicam-vms` service environment:
```
ANYAICAM_ANALYTICS_SYNC_ENABLED=true
ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED=true
ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED=true
ANYAICAM_LIVE_RELAY_ENABLED=true
ANYAICAM_LIVE_P2P_ENABLED=true
```
This is not optional: `resolve_cloud_flag()` always prefers an explicit env var over the mode. If these five lines stay in the compose file, the entitlement change below will still fire a real restart -- but the restart will change nothing, because each flag re-reads its own unchanged `=true` literal on the new process's startup. Do **not** run `docker compose up -d` yet after removing them -- that happens automatically in step 5, and doing it manually now would just make the appliance run with all six flags at their `""`/no-mode-configured legacy default (`false`, harmless, but skips the point of proving the automatic path).

### 3. The entitlement/account change that sets this customer to Local

Look up this Ryzen unit's real `customer_id` (via `appliances.customer_id` in the cloud DB, keyed by its `cloud_id`), then confirm its actual licensed camera count -- **do not assume 5**: this session's own read of the appliance found 12 `camera*` directories under `/app/recordings` (only cameras 1-5 showed active `_event_buffer` writes in earlier log samples; 6-12 may be inactive/placeholder slots). Query `cameras` for this `customer_id` to get the real count before picking a `PLAN_TIERS` tier.

Two ways to create the entitlement, in order of preference:

- **Real purchase (not available yet)**: Local's own Stripe Price IDs (`ANYAICAM_STRIPE_PRICE_LOCAL_1_8`/`9_16`/`17_32`/`33_64`) are still unset in every environment (confirmed earlier this session) -- `create_camera_slot_checkout()` would 503 with `PRICE_ID_REQUIRED` for `plan_type="local"` today. Not usable until those are created in Stripe.
- **Direct entitlement grant (what's actually usable right now)**: an admin/support action calling `customer_entitlements.upsert_entitlement(customer_id=<real id>, product="camera_slots_local", camera_slot_quantity=<real count>, status="active")` directly against the cloud database. This is the same function the real checkout webhook would have called -- there is no separate/lesser code path, so a manually-granted entitlement is indistinguishable from a Stripe-driven one to every downstream reader (`product_mode_for_customer()`, the dashboard's Plan stat, `/subscription-portal`).

If this customer currently holds an active `camera_slots_hybrid` entitlement (likely, given the pilot unit's current Hybrid-shaped config), decide explicitly whether to also cancel/deactivate that row (`status='cancelled'`) as part of this step -- `product_mode_for_customer()` returns `"hybrid"` whenever both are active simultaneously (Hybrid always wins), so the Local-only grant above would have **no visible effect** until the Hybrid row is no longer active.

### 4. How the appliance detects the change

No push mechanism -- pure poll, on the existing cadence:

1. `edge_camera_sync.camera_configuration_sync_worker()` (already running on Ryzen, `~60s` interval via `ANYAICAM_CAMERA_CONFIG_SYNC_INTERVAL_SECONDS`) calls `sync_provisioned_cameras()`, which does its normal `GET /api/appliance/configuration` call.
2. **Cloud side, same request**: `appliance_configuration()` computes `product_mode_for_customer(customer_id)` -> now `"local"` (assuming step 3's Hybrid row is deactivated), compares it against this appliance's stored `last_reported_product_mode` (still `"hybrid"` or `""` from before), finds a real transition, updates the column, and queues a `restart_vms` `appliance_commands` row (`created_by="product-mode-transition"`). The response body includes `"product_mode": "local"`.
3. **Edge side, same response**: `sync_provisioned_cameras()` reads `response["product_mode"]` and calls `product_mode.persist_mode("local")`, which writes `/opt/anyaicam/data/config/product_mode.json` and logs `product_mode.changed previous=hybrid new=local restart_required=true`.
4. Separately, `anyaicam_agent`'s own existing command-poll cycle (`GET /api/appliance/commands`) picks up the queued `restart_vms` row, writes a marker to `/var/lib/anyaicam/pending_actions/restart_vms.json` via `_queue_privileged_action()`.
5. `anyaicam-privileged-watcher.path` (confirmed `active`/`enabled` on Ryzen right now) triggers the watcher, which waits a 10-second cancellation grace window, then runs `docker compose --project-directory /opt/anyaicam up -d`.

Worst-case detection latency: one `edge_camera_sync` poll interval (~60s) plus one `anyaicam_agent` command-poll interval (check its own cadence before relying on a number) plus the 10s grace window.

### 5. What turns off, automatically, once the new container starts

With the five explicit overrides removed (step 2) and mode now `"local"`:
- `ANALYTICS_SYNC_ENABLED` -> `False` (analytics_sync.py's worker never starts)
- `EVENT_MEDIA_UPLOAD_ENABLED` -> `False`
- `FACIAL_EMBEDDING_SYNC_ENABLED` -> `False`
- `LIVE_RELAY_ENABLED` -> `False`
- `LIVE_P2P_ENABLED` -> `False` (webrtc_publisher.py's worker never starts)
- `RECORDING_UPLOAD_ENABLED` -> `False` (already was)

Untouched by this or any other governed flag: `ANYAICAM_LPR_ENABLED`, `ANYAICAM_FACIAL_RECOGNITION_ENABLED`, `ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED`, `ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED`, `ANYAICAM_LOCAL_STORAGE_AUTO_DELETE_ENABLED`, `ANYAICAM_TALK_DOWN_DISCOVERY_ENABLED` -- these stay exactly as currently configured, since they're paid-entitlement/local-housekeeping flags, not product-mode-governed.

### 6. What stays working locally, unaffected

Local recording (per-camera `_event_buffer` writers), local playback, local live view over the LAN (direct connection to the appliance's own web UI/HLS -- not the relay/P2P/WireGuard remote-access paths), on-device analytics processing (smart motion/LPR/PPE detection itself, as opposed to syncing the *results* to the cloud), camera provisioning/access, and local storage management/retention. None of these read any of the six governed flags -- confirmed by code review, not inference.

### 7. Does a controlled restart occur automatically?

Yes -- see step 4's full chain. It is a container recreate (`docker compose up -d`), never a host `reboot`. It is controlled in three ways: a 10-second human-cancellable grace window (delete the marker file to abort), a hard dedup check that refuses to queue a second `restart_vms` while one is still `pending`/`delivered`, and the fact that only a genuine mode transition (not a repeated poll) ever queues one at all.

### 8. Verifying recording, playback, analytics, local live view, and camera access still work

Run these within a few minutes after the restart completes (`docker ps` shows a new `CONTAINER ID` for `anyaicam-vms`):

```bash
# Recording -- fresh local files continuing to appear
docker exec anyaicam-vms sh -c 'find /app/recordings/camera1/_event_buffer -newermt "-2 minutes"'

# Playback -- the existing local HLS-backed route still serves
curl -sI https://<ryzen-lan-or-tailscale-address>/playback   # expect 200/303 to login, not 5xx

# On-device analytics still detecting (not synced to cloud, just running)
docker logs anyaicam-vms --since 2m | grep -i "smart_motion\|people_counting\|lpr\|ppe" | grep -v "analytics_sync"

# Camera access/provisioning -- appliance-agent still healthy
systemctl is-active anyaicam-agent.service   # active
docker exec anyaicam-vms curl -s localhost:8000/health   # or whatever /health reports

# Local live view -- from a browser ON THE SAME LAN (not remote), open
# /customer-live and confirm at least one tile reaches 'playing' (see the
# black-tile fix in this same branch) without any relay/P2P transport
# available -- it should still resolve via direct local HLS.
```

### 9. Verifying AWS/cloud-dependent services are actually off

```bash
docker exec anyaicam-vms env | grep ANYAICAM_.*ENABLED   # expect empty output

docker exec anyaicam-vms python3 -c "
import analytics_sync, event_media_uploader, facial_embedding_sync, live_relay_uploader, recording_uploader, webrtc_publisher
print('analytics_sync', analytics_sync.ANALYTICS_SYNC_ENABLED)
print('event_media', event_media_uploader.EVENT_MEDIA_UPLOAD_ENABLED)
print('facial_embedding', facial_embedding_sync.FACIAL_EMBEDDING_SYNC_ENABLED)
print('live_relay', live_relay_uploader.LIVE_RELAY_ENABLED)
print('recording_upload', recording_uploader.RECORDING_UPLOAD_ENABLED)
print('live_p2p', webrtc_publisher.LIVE_P2P_ENABLED)
"   # every line must print False

# Absence of the exact log lines confirmed actively firing before migration:
docker logs anyaicam-vms --since 5m | grep -E "analytics_sync\.(scan_tick_begin|http_call_begin)|event_media\.(registered|environmental_motion_skipped)|live_relay\.segment_upload_"
# expect zero matches

# No outbound connections to the cloud portal or S3 from this container
docker exec anyaicam-vms sh -c "netstat -tn 2>/dev/null | grep -E 'ESTABLISHED' | grep -v 127.0.0.1"
# cross-reference any remaining lines' remote IPs against portal-staging.anyaicam.com / *.amazonaws.com -- expect none related to the six disabled features (other unrelated local/LAN connections are fine)
```

### 10. Exact rollback steps, back to Hybrid

**Fast path (seconds, doesn't wait for a poll cycle)** -- re-run steps 2-3 in reverse right on the box:
```bash
cp /opt/anyaicam/docker-compose.yml.pre-local-migration /opt/anyaicam/docker-compose.yml  # restores the five explicit =true lines
cd /opt/anyaicam && docker compose up -d
```
This alone restores Hybrid-shaped behavior immediately, regardless of what the cloud-side entitlement says, since the explicit env vars always win.

**Full rollback (also restores the entitlement-driven state)**:
```sql
-- against the cloud DB, for this customer_id
UPDATE customer_entitlements SET status='active' WHERE customer_id=<id> AND product='camera_slots_hybrid';
UPDATE customer_entitlements SET status='cancelled' WHERE customer_id=<id> AND product='camera_slots_local';
```
This makes `product_mode_for_customer()` report `"hybrid"` again; the next `edge_camera_sync` poll detects the reverse transition, the cloud queues a second `restart_vms` automatically (same dedup/grace-window protections), and Ryzen restarts back into Hybrid on its own -- no SSH required, mirroring the forward migration exactly. Use the fast path first if immediate reversal matters more than proving the automatic path both directions.

Neither rollback path touches `cameras`, `analytics_subscriptions`, `camera_analytics_entitlements`, or any file under `/app/recordings` -- confirmed by code review, same as the forward migration.
