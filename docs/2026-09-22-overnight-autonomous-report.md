# Overnight autonomous session report — 2026-09-22

Consolidated report for the five priorities requested before signing off. Read-only investigation stayed read-only; no Ryzen configuration, product mode, or Intrusion/Line-Crossing enablement was changed; no physical LPR/People Counting/Intrusion/Face tests were performed.

## Top line for the morning

1. **`anyaicam-vms` on Ryzen is restarting on its own, roughly every 6–13 minutes, and was still doing it as of the last check before this report was written (04:18:50 UTC, ~3 min before this was written).** Root initiator not identified — see Priority 1. This is the most urgent open item.
2. Two real "5-minute assumption" bugs found and fixed tonight (one already reported earlier: `build_manual_clip()`; one new: `build_motion_event_clip()` — the actual primary clip pipeline). Both committed and pushed.
3. The customer-visible Playback page problem is **not on Ryzen** — it's `portal-staging.anyaicam.com` (= `app.anyaicam.com`, same backend), a separate cloud service that hasn't been redeployed in ~23 hours and has no live data path from Ryzen at all. See Priority 2 for the exact rollout checklist.
4. Intrusion/Line-Crossing's core logic (multi-rule, debounce, direction, tenant scoping) looks sound on review. One real gap: **no exclusion/mask zone support exists anywhere** (front end or back end). Still fully disabled on Ryzen.
5. Local/Hybrid product-mode code is on Ryzen already, inert (`current_mode() == ''`), and cannot possibly be the restart-loop cause tonight because the cloud-side code that would queue a restart from a mode change (`_queue_product_mode_restart()`) isn't deployed anywhere yet — confirmed, not assumed.

---

## Priority 1 — Ryzen restart investigation

### What's definitively proven

- Docker's own daemon log for one restart (23:00:09–23:00:10 CDT) records: `"ShouldRestart failed, container will not be restarted"`, `error="restart canceled"`, **`hasBeenManuallyStopped=true`**, `restartCount=0`. This is dockerd's own bookkeeping saying an explicit stop/kill was issued via its API — **this is not Docker's native crash-restart policy** (which would show a plain die→start with no preceding kill, and would increment `RestartCount`).
- Every restart follows the identical shape: `container health_status: unhealthy` → ~15–20s later, `kill(signal=15)` → 10s later `kill(signal=9)` (force) → `stop` → `die (exitCode=137)` → `start`, all within about half a second at the end.
- Observed restart timestamps (UTC): 03:40:10 (mine, the deploy itself), 03:47:20, 03:53:45, ~04:00:10, 04:06:00, 04:18:50. Cadence is irregular (roughly 6–13 min), not a fixed timer.
- `RestartCount` stays at 0 across every check — consistent with "manually stopped" each time, never Docker's own policy-triggered restart.
- Each restart correlates with sustained ~750–790% CPU (of ~8 cores) immediately after boot, easing over a few minutes — plausible cause of the healthcheck (5s timeout) intermittently failing 3x in a row and flipping to "unhealthy".

### What was ruled out (with evidence, not assumption)

| Candidate | Evidence it's not the cause |
|---|---|
| Docker's native `restart: unless-stopped` policy | `RestartCount` stays 0; daemon log explicitly says "restart canceled... hasBeenManuallyStopped=true" |
| `anyaicam-agent.service` (the unprivileged appliance agent) | Process never restarted during the window (single PID, `active (running)` since 22:39:56 CDT throughout); its own journal has zero log lines mentioning restart_vms/commands queued in the entire window |
| `anyaicam-privileged-watcher.service`/`.path` (the `restart_vms` marker→`docker compose up -d` mechanism) | `journalctl -u anyaicam-privileged-watcher.service --since -40m` returns **zero entries** — it never fired |
| Product-mode automatic restart (`appliance_cloud._queue_product_mode_restart()`) | This is **cloud-side** code. `portal-staging.anyaicam.com` (confirmed same backend as `app.anyaicam.com`) is running `build_id: "local"`, up ~23h — it does not have this function deployed at all. It structurally cannot have queued anything tonight. (Also: even if it could, this Ryzen's `activation_status` is `'pending'`, and `describe_transition()` requires a real non-empty `new_mode`, which a customer with no resolvable entitlement never gets.) |
| Cron / systemd timers | `crontab -l` empty; `systemctl list-timers` shows only stock Ubuntu timers (apt, logrotate, sysstat, etc.), nothing docker/vms-related |
| A second "autoheal"-style container | `docker ps -a` shows only `anyaicam-vms`, no other container, running or stopped |
| Another human session | `who`/`last` show only the one continuous Tailscale session since 20:14 CDT (yours) — the same one you were using; you've confirmed you weren't restarting it |
| `uvicorn --reload` / bind-mount-triggered reload | Launch command confirmed: `uvicorn main:app --host 0.0.0.0 --port 8000 --proxy-headers` — no `--reload` flag |
| `anyaicam-vms.service` restart policy | It's a plain `Type=oneshot, RemainAfterExit=yes` unit with `ExecStart=docker compose up -d` and **no `Restart=` directive at all** — systemd itself never re-invokes it on its own |
| Docker daemon auto-heal config | No `/etc/docker/daemon.json`; plain `dockerd -H fd:// --containerd=...`, no exotic flags |

### Unresolved — what I could not check without sudo

- `/var/lib/anyaicam/pending_actions/` (root-owned) — could not confirm whether a `restart_vms.json` marker is repeatedly reappearing.
- The privileged-watcher's own journal came back empty without sudo, which is suggestive it never fired, but I could not `sudo journalctl` to be fully certain the read wasn't silently filtered.

**No reproducible software bug was conclusively identified**, so per your instruction I made no fix — fixing something I can't prove risks masking the real cause. I did not find a "smoking gun" producer for the explicit stop/kill; every mechanism this codebase actually contains was checked and ruled out with direct evidence.

### Exact next steps for you (sudo required, so I staged nothing to run — these are just the commands)

```bash
sudo journalctl -u anyaicam-privileged-watcher.service --since "-1 hour" --no-pager
sudo ls -la /var/lib/anyaicam/pending_actions/
sudo journalctl -u anyaicam-agent.service --since "-1 hour" --no-pager | grep -i restart
```

If the watcher's journal shows real firings with fresh `command_id`s, that's the mechanism — the next question becomes *why* it keeps re-arming (a new marker requires a new `restart_vms` command from `GET /api/appliance/commands`, and the cloud's own delivery code sets `status='delivered'` atomically on the same read that selects `status='pending'`, so a genuine one-command-delivered-N-times bug would itself be surprising and worth its own investigation). If the watcher's journal is genuinely empty even under sudo, the initiator is something entirely outside this codebase's own tracked files (worth checking `auditd`/`ausearch` against the Docker socket, or asking whoever manages this box's base image whether anything else has Docker API access).

**As of the last check before writing this (04:18:50 UTC), the container had been up for ~3 minutes — inside the range of every previous gap, so this cannot be called resolved.**

---

## Priority 2 — Cloud/portal Playback readiness

### The gap, precisely

| | Ryzen (edge) | `portal-staging.anyaicam.com` / `app.anyaicam.com` (same backend) |
|---|---|---|
| `runtime_role` | `edge` | `cloud` |
| `environment` | `production` | `staging` |
| `build_id` | `19836588417a3...` (tonight's `1983658`) | `"local"` — no pinned commit |
| uptime (as of investigation) | fresh (just redeployed) | ~23 hours, since 2026-09-21 05:03 UTC |
| `/playback` `Cache-Control` | `no-store` (fixed tonight) | absent — pre-dates the fix |

Confirmed via `/version`, identical `hostname`/`started_at` down to the microsecond, identical Cloudflare edge IP, and a byte-for-byte identical `/customer-login.html` response — `app.anyaicam.com` and `portal-staging.anyaicam.com` are the same origin process, not two independently-deployed environments.

### Why Ryzen's own fixes can never reach the customer through this cloud service

The cloud's customer Playback catalog is populated by exactly one path: `POST /api/appliance/recordings/{camera_id}/available`, called by the appliance's `recording_uploader.py` after a real S3 upload. Ryzen's own startup log (captured live tonight):

```
"cloud_upload_enabled": false, "cloud_foundation_ready": false, "cloud_recording_ready": false,
"missing_cloud_requirements": ["database_configured","s3_configured","public_url_configured","secrets_manager_configured"]
"upload_worker": "disabled"
```

Ryzen has never called that endpoint. Whatever the cloud's `recordings` table holds for this customer is frozen at whatever it last received from some other source, independent of tonight's fixes or Ryzen's actual current state.

### Exact rollout checklist (nothing performed — this is the plan only, per your instruction not to touch AWS/infra tonight)

**A. Deploy the current code to the cloud service:**
1. Confirm which real deployment mechanism serves `portal-staging.anyaicam.com`/`app.anyaicam.com` (this was not identified tonight — the box behind Cloudflare wasn't located; `anyaicam-staging` in `~/.ssh/config` points at `34.194.19.113`, an AWS EC2 host, but this was never confirmed to be the actual origin — needs verification via AWS, not the cached SSH config, per your own standing project rule).
2. Deploy commit `eb95450` (current branch tip) or later to that service. This brings: the `Cache-Control: no-store` fix, the `linked_recording` backfill fixes (motion/AI-detection/people-counting/facial-recognition), the recording-catalog lock-contention fix, and tonight's `build_motion_event_clip()` fix.
3. Verify post-deploy: `curl -D - https://portal-staging.anyaicam.com/version` shows a real commit hash (not `"local"`) and a fresh `started_at`; `curl -D - https://portal-staging.anyaicam.com/playback` (unauthenticated) shows the redirect *plus* the fact that the underlying build actually changed.

**B. Give Ryzen a real path to the cloud (separate, larger effort, needs infra work you asked to defer):**
1. Provision whichever of `database_configured` / `s3_configured` / `public_url_configured` / `secrets_manager_configured` are missing for this specific appliance's `.env` (`/etc/anyaicam/vms.env`, root-owned).
2. Once `cloud_foundation_ready: true`, confirm `recording_uploader`'s worker actually starts (`upload_worker: "running"` in the next startup log line) and begins calling `POST /api/appliance/recordings/{camera_id}/available` for real Event-mode clips.
3. Only after both A and B are done does a customer visiting the real portal see anything resembling Ryzen's current state.

**C. Cloud DB verification checklist (for when admin access is restored)** — this was already written up in a prior session's `docs/product-mode-local-hybrid-2026-09-21.md` ("Full entitlement-driven migration plan", §1) and still applies verbatim: confirm the cloud database has run the `appliances.last_reported_product_mode` migration before any code that reads/writes it is deployed, or the very first poll will 500 with `no such column`.

---

## Priority 3 — Event-linkage / software audit

Re-verified every event-type producer end to end (`grep`-enumerated every `AnalyticsEventModel(`/`MotionEventModel(` construction site in `main.py` and `customer_analytics_rule_worker.py` — 7 real sites plus one mock-data generator, correctly excluded):

| Event type | Producer | `linked_recording` mechanism | Status |
|---|---|---|---|
| `motion` | `store_motion_event()` | Deterministic path (`/recordings/clips/motion/motion_{id}.mp4`), written after `build_motion_event_clip()` actually completes — correct by construction, no backfill needed | OK |
| `smart_motion` | same function | Inherits the base motion event's already-correct value | OK |
| `person`/`car`/`truck`/`ppe`/other YOLO classes | `save_yolo_events()` | Backfilled (fixed earlier tonight) | OK |
| `plate` | same | Backfilled | OK |
| `facial_recognition` | same | Backfilled (this session's own gap-in-the-earlier-fix, closed) | OK |
| `people_counting_in`/`_out` | `people_counting_worker()` | `persist_event_recording()` + backfill scheduling (fixed earlier tonight) | OK |
| `intrusion`/`line_crossing` | `customer_analytics_rule_worker.py` | Same scheduling pattern, mirrored (fixed earlier tonight) — feature still inert | OK, inert |

**Consistency check**: the customer-facing event card fallback (`linked_recording = event.get("linked_recording") or "/playback"`) degrades gracefully to a generic Playback link when null — no event type can produce a broken link, regardless of whether backfill has completed yet.

**New "5-minute assumption" found and fixed**: `build_motion_event_clip()`'s own candidate-file shortlist used a hardcoded `2 * RECORDING_SEGMENT_SECONDS` (10-minute) early cutoff before ever considering a source file — the exact same bug class as `build_manual_clip()`'s already-fixed lookback window, but in the *primary* clip-building pipeline used by every event type above. Fixed to `2 * max(RECORDING_SEGMENT_SECONDS, camera's own max_event_seconds)`. Two new regression tests added (`test_a_source_starting_past_the_old_hardcoded_shortlist_window_is_found_when_max_event_seconds_is_configured_longer`, `test_a_source_starting_past_both_real_maximums_is_still_correctly_excluded`), both passing; full `test_motion_clip_shortlist_fix.py` + adjacent clip-wiring suites re-run clean (one pre-existing, unrelated Windows-path-separator test failure confirmed present on unmodified `HEAD` too).

No other hardcoded 5-minute/300-second assumptions found in `main.py` outside the two now-fixed sites.

---

## Priority 4 — Intrusion/Line-Crossing hardening review

Reviewed `analytics_rules_engine.py` and `customer_analytics_rule_worker.py` in full (code review only — feature stays disabled on Ryzen, no physical testing performed).

- **Multi-rule behavior**: correct. Every rule evaluated independently per cycle; all per-track state (`_dwell_entered_at`, `_line_last_side`, `_last_fired_at`) keyed by `(camera_number, rule_id, track_id)`, so two simultaneous rules on one camera (even two line-crossing rules, or a line + a zone) cannot cross-contaminate each other's state.
- **Debounce**: intrusion uses a real dwell timer (`DEFAULT_DWELL_SECONDS=5.0`) plus fire-once-per-continuous-dwell dedup; line-crossing uses an actual side-flip state machine (not distance-threshold guessing) plus a `MIN_REFIRE_SECONDS=0.5` floor against boundary jitter. Both are sound, deliberate designs, not naive placeholders.
- **Direction handling**: `"both"/"inbound"/"outbound"` maps cleanly to the rule author's own two-point line orientation; documented, not inferred.
- **Tenant isolation**: enforced upstream by `load_rules_for_camera(camera_id)`'s own `WHERE camera_id=?` scoping against the tenant-safe `customer_analytics_rules` table (never the legacy `analytics_rules.json`). Each physical appliance belongs to exactly one customer, so no additional in-process isolation is needed beyond that query.
- **CPU impact**: the worker's own detection call (`detect_objects_frame()`) only runs `if rules:` — with the real `customer_analytics_rules` table empty (confirmed again tonight), every camera's cycle is a cheap identity lookup + empty-list check every 1.5s, negligible. One shared detection/tracking pass per camera per cycle covers *all* of that camera's rules — no per-rule multiplication of the expensive work.
- **Real gap found**: **no exclusion/mask zone concept exists anywhere** — not in `analytics_rules_engine.py`, not in the rule editor, not even a stubbed/broken half-implementation. This is a legitimate feature gap worth deciding on before a broader rollout (e.g. a customer wanting to exclude a public sidewalk from an intrusion zone has no way to do that today), not a bug — nothing to fix without a real design decision.

No code changes made for this priority (review only, as requested).

---

## Priority 5 — Local/Hybrid readiness review

- `product_mode.py` is present and importable on Ryzen's current `1983658` build; `current_mode()` correctly resolves to `''` (unset/legacy), matching every prior deploy tonight — untouched, as required.
- Confirmed installer scripts (`install.sh`, `03-product-mode.sh`) have no outstanding `TODO`/`FIXME` markers.
- The **known, already-documented blockers** from the prior `docs/product-mode-local-hybrid-2026-09-21.md` still apply unchanged tonight, re-verified:
  1. The cloud deployment serving Ryzen doesn't have `_queue_product_mode_restart()` at all (same "local"/stale build confirmed under Priority 1/2 above) — the fully-automatic entitlement-driven restart path is not usable yet, only the manual env-var override path is.
  2. A real business decision (documented as "Option A vs Option B" in that doc) is still open for what happens on a Hybrid→Local downgrade with no prior Local entitlement — nothing in tonight's work resolves this, and nothing should.
- Ryzen's own `cloud_foundation_ready: false` (Priority 2 finding) is itself a Local/Hybrid readiness blocker in a different sense: even once the cloud side is deployed, Hybrid-mode features that depend on the same missing `database_configured`/`s3_configured`/etc. prerequisites won't work on this specific physical unit until that infrastructure work happens.

No mode changes made; nothing enabled.

---

## Commits made tonight (this autonomous session)

- `1983658` (earlier this evening, already reported): Cache-Control fix, `linked_recording` backfill completeness (people_counting, customer_analytics_rule_worker, facial_recognition), `build_manual_clip()` lookback-window fix.
- `eb95450`: `build_motion_event_clip()`'s own hardcoded 5-minute shortlist-window fix (Priority 3), with 2 new regression tests.

Both pushed to `reconcile/golden-foundation-20260911`. Nothing else was committed — Priority 4/5 were review-only per your instructions.

## Tests run

- `tests/test_motion_clip_shortlist_fix.py` (13 tests, 1 pre-existing unrelated failure confirmed present on unmodified `HEAD` too) + `test_build_manual_clip_lookback_window.py` + `test_ai_event_clip_wiring.py` + `test_motion_event_media_wiring.py` + `test_smart_motion_event_media_wiring.py` + `test_ppe_lpr_facial_event_media_gap_audit.py`: 52 passed.
- Syntax-checked `main.py` after every edit.

## What still needs daylight/physical validation (unchanged from earlier tonight, reconfirmed)

- Live/organic confirmation that person/car/vehicle/PPE events actually receive `linked_recording` under real daytime activity (code path confirmed correct; not yet observed with real detections).
- Any physical LPR/People Counting/Intrusion/Line-Crossing/Face testing.
- Cloud deployment of `eb95450`+ to `portal-staging.anyaicam.com`, and the AWS-side cloud-upload provisioning for Ryzen (Priority 2) — both explicitly deferred per your instructions.

## Exact next step for you in the morning

1. **First**, run the three `sudo` commands in Priority 1 above and tell me what they show — that's the fastest path to actually identifying the restart-loop initiator, which is the most disruptive live issue right now (repeated camera reconnects/CPU spikes every several minutes).
2. Separately, decide whether/when to pursue the Priority 2 cloud deployment — that's what actually gets the customer-facing Playback page fixed; nothing further on Ryzen's own code will move that needle.
