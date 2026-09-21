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

**This is the whole upgrade-is-billing-driven loop, already closed end to end in code:**

```
Customer clicks "Upgrade to Hybrid"
  -> POST /api/customer/camera-slots/checkout {plan_type: "hybrid", tier_label: ...}
     (create_camera_slot_checkout() -- already existed, reused as-is)
  -> Stripe Checkout -> webhook -> upsert_entitlement(product="camera_slots_hybrid")
     (already existed, reused as-is)
  -> product_mode_for_customer() now returns "hybrid"
  -> next GET /api/appliance/configuration poll (edge_camera_sync.py, ~60s cadence)
     reports product_mode="hybrid" -> persist_mode("hybrid") writes the state file,
     returns True
  -> (NOT YET WIRED -- see "What's still a decision" below) appliance restarts
     to pick up the new mode's flag defaults
```

### 5. Admin/partner portal: "Upgrade to Hybrid" button

Added to the customer setup/review page (`partner_workspace.py`), shown only when the customer currently has an active Local entitlement (never shown to an existing Hybrid customer, never alongside the "no plan yet" first-purchase panel) and only when a real Stripe Price ID is configured for the equivalent-capacity Hybrid tier (same fail-closed discipline as the existing "Buy camera capacity" panel). It calls the exact same `/api/customer/camera-slots/checkout` endpoint with `plan_type: "hybrid"` -- no new checkout logic, no separate billing path, genuinely entitlement/billing-driven as required.

### 6. Tests

- `tests/test_product_mode.py` (19 tests) -- resolver logic: env-var-always-wins, mode defaults, legacy fallback, persist/no-op/change detection, an empty-string env var treated as unset (matching every flag's pre-existing behavior).
- `tests/test_product_mode_cloud_config.py` (10 tests) -- `product_mode_for_customer()` directly, and the real `GET /api/appliance/configuration` route end to end (TestClient), including the exact upgrade-flips-the-field-immediately scenario and per-customer isolation.
- All six migrated worker modules' existing test suites (165 + additional files, ~200 tests total) re-run clean -- every test that toggles these flags does so via `monkeypatch.setattr(module, "FLAG_NAME", ...)` directly on the module attribute, which is completely unaffected by how that attribute was originally computed.
- `tests/test_edge_camera_sync.py` updated (5 assertions) for the new `product_mode_restart_required` key in `sync_provisioned_cameras()`'s result.

19 pre-existing test failures were found and confirmed **unrelated** to this work (verified by reverting each changed file individually and re-running): 19 in `test_recording_uploader_*` (this dev machine's shell has no `AWS_REGION` set) and 26 in `test_camera_discovery_provisioning.py` plus 7 in the customer-setup-page test files (a pre-existing "licensed for 0 cameras" / stale-assertion issue in this environment, reproduced identically on the unmodified `main` branch state).

## What's still a decision (not built yet)

### A. Automatic restart on mode change

Every governed flag is read once at process start (unchanged from before this module existed) -- a mode change only takes effect on the next restart of the `anyaicam-vms` container. `product_mode.persist_mode()` logs a `product_mode.changed ... restart_required=true` warning and `sync_provisioned_cameras()` surfaces `product_mode_restart_required: True`, but **nothing currently acts on that signal**.

The appliance already has a real, existing cloud-to-edge privileged command channel that could close this loop: `appliance_protocol.ALLOWED_COMMANDS` includes `restart_vms`, handled by the host-side `anyaicam-privileged-watcher.service` already running on the Ryzen unit. The natural next step is: have the Stripe webhook handler (or `edge_camera_sync.py`, on detecting `product_mode_restart_required=True`) enqueue a `restart_vms` command for that appliance. **Not implemented in this branch** -- it touches the webhook/command-queue path, which felt like a decision to surface rather than bundle in silently. Until it exists, an upgrade/downgrade requires a manual restart (`docker compose restart` / `systemctl restart anyaicam-vms`) to take visible effect, even though the mode itself updates automatically.

### B. Hybrid -> Local downgrade when no prior Local entitlement exists

`product_mode_for_customer()` returns `""` (not `"local"`) for a customer whose Hybrid subscription is cancelled and who never held a `camera_slots_local` entitlement (e.g. a customer who started on Hybrid from day one). This is a genuine, flagged business decision, not something silently assumed:

- **Option 1**: cancelling Hybrid automatically grants an equivalent one-time Local entitlement (same `camera_slot_maximum`), so the customer keeps their paid-for camera-slot capacity and their appliance falls back to `"local"` mode with cameras/recordings/analytics settings untouched.
- **Option 2**: cancelling Hybrid leaves the customer with no camera-slot entitlement at all (`total_camera_slots()` returns 0) until they make a fresh purchase -- harsher, but never silently grants something never paid for.

Nothing in this branch decides between these -- `product_mode_for_customer()` reports `""` honestly in that case, matching this codebase's existing "never guess, fail closed" discipline for every other resolver (`resolve_tier()`, `resolve_addon()`).

In either case, **no local data is ever deleted** by a downgrade: `product_mode.persist_mode("local")` only flips which cloud-dependent workers start on next restart. Cameras, recordings, analytics settings (`analytics_subscriptions`, `camera_analytics_entitlements`), and all local files under `/app/recordings` are completely untouched by any code path this branch adds -- none of it writes to those tables or deletes files.

## Exact steps to migrate the live Ryzen appliance (not yet performed)

1. **Confirm the customer's real entitlement first.** Query `customer_entitlements` for this Ryzen unit's `customer_id` -- if it has no `camera_slots_hybrid` row (likely, since this is a pilot unit predating this billing model), decide whether to backfill one before flipping the appliance, or set `ANYAICAM_PRODUCT_MODE=hybrid` explicitly as a manual override (bypassing entitlement-driven resolution entirely -- `current_mode()` prefers the env var first).
2. **Pull this branch's code** onto the Ryzen unit (or wait for it to merge through the normal release path) -- `product_mode.py` must exist before any flag migration takes effect; deploying it alone changes nothing (see "zero-behavior-change" above).
3. **Decide the target mode.** Given the pilot unit's current live config (`ANALYTICS_SYNC_ENABLED=true`, `EVENT_MEDIA_UPLOAD_ENABLED=true`, `FACIAL_EMBEDDING_SYNC_ENABLED=true`, `LIVE_RELAY_ENABLED=true`, `LIVE_P2P_ENABLED=true`, `RECORDING_UPLOAD_ENABLED=false`), it is already Hybrid-shaped except for bulk recording upload. Setting `ANYAICAM_PRODUCT_MODE=hybrid` and then **removing** the five individual `_ENABLED=true` overrides would make the mode itself the authoritative source going forward (recommended) -- or leave the explicit flags in place indefinitely, since they always win over the mode regardless.
4. **Restart `anyaicam-vms`** to apply. Verify via `docker exec anyaicam-vms env | grep ANYAICAM_.*ENABLED` (should show nothing, if you removed the explicit overrides) and confirm each worker's own startup log line still shows it running (or not) as intended.
5. **To test Local instead**: set `ANYAICAM_PRODUCT_MODE=local`, remove the explicit overrides, restart, and verify: local recording continues (check `/app/recordings` for fresh files, exactly as confirmed live today), live view works from the LAN, and `docker logs anyaicam-vms` shows no further `analytics_sync.http_call_begin` / `event_media.registered` / `live_relay.segment_upload_*` lines (the three cloud-dependent behaviors confirmed actively running today).
6. **Roll back** at any point by re-setting the explicit `_ENABLED` env vars to their prior values (they always override the mode) or unsetting `ANYAICAM_PRODUCT_MODE` entirely (falls back to whatever the explicit flags say, i.e. today's exact behavior).

No step above has been performed. This is the plan, not an action taken.
