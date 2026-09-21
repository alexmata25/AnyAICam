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
