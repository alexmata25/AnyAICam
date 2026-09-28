# Vehicle Access (car-wash lanes): technical design

**Status:** design only (2026-09-28). No code, no deployment. This is a new licensed AnyAiCam module, not a separate application.

## 1. Architecture

Vehicle Access follows the standing cloud/edge rule (`anyaicam-cloud-edge-responsibility-split`).

**The EDGE appliance decides and acts, per vehicle, with no cloud round trip:**
- LPR reads from the lane camera (existing pipeline);
- RFID reads from the lane reader (new adapter);
- correlation of both into ONE transaction;
- member lookup against the locally synced member/vehicle/tag table;
- rule evaluation;
- the gate command through the controlled-output interface;
- snapshot and clip.

It keeps working through an internet outage, and transactions queue for the cloud.

**The CLOUD owns:**
- customers/members, vehicles, plates and tags;
- plans and statuses;
- sites, lanes and readers configuration;
- access-rule policy;
- the Activity/Customers/Gates/Rules UI;
- attendant manual Grant/Deny;
- audit history;
- notifications;
- licensing.

Config syncs down to the edge; transactions and audits sync up. An attendant's remote Grant is routed to the edge, which performs it, exactly like remote door unlock today.

```
Lane camera --(existing AI loop + lpr.scan_frame)--> plate read ─┐
RFID reader --(RfidReader adapter)-------------------> tag read ──┤
                                                                 ▼
                                   vehicle_access.correlator (per lane, window)
                                                                 ▼
                       member/vehicle lookup (edge-synced tables) + rules engine
                                                                 ▼
                 decision ──► GateOutput (simulated first) ──► audit record
                     │                                              │
                     └─► transaction (+ snapshot, clip) ──► analytics_sync ──► cloud
```

## 2. Reused components

| Need | Existing component |
|---|---|
| Plate reads with confidence and voting | `lpr.py`: detector, glyph reader, `confirm_plate` (≥80 confidence votes, near-duplicate suppression), independent cadence (`scan_frame`) |
| Plate crop, thumbnail, clip | `main.run_lpr_scan`: plate crop, annotated thumbnail, Event-mode recording trigger; `event_media_sharing` and `_schedule_owned_analytics_clip` |
| Edge→cloud delivery, retries, idempotency | `analytics_sync` (local event queue, durable retry), `appliance_cloud` analytics-event route (idempotent on `local_event_id`) |
| Cloud→edge config | `edge_camera_sync` / `/api/appliance/configuration` (already carries analytics rules, entrance cameras) |
| Gate output abstraction, pulse, cooldown, dry-run | `relay_control.py` (`RelayProvider`, `MockRelayProvider`, `RelayRequest`/`RelayResult`, cooldown) |
| Edge decision → audited physical action | facial access (`facial_events.evaluate_access_rules`, `door_access.record_door_access_event`) |
| Remote command routed to the right appliance | AAC Voice Call door unlock (`aac_voice_call_door`: two-step confirm, cloud authorizes, edge acts) |
| Tenant isolation, roles, audit | `partner_portal` identities, `customer_id`/`site_id` scoping everywhere, `audit()` |
| Licensing | `camera_analytics_entitlements` plus a new feature key `vehicle_access`, Stripe price like the other analytics |
| Notifications, deep links | `notification_engine`, `notification_email` (new event types `vehicle_access_denied` / `vehicle_access_review`) |
| Playback of the clip | Playback event-clip path (one card per clip) |

## 3. Database (cloud tables mirrored to the edge where needed)

- **`va_members`:** id, customer_id (tenant = the car-wash operator), site_id NULL (NULL = all sites), account_ref, display_name, plan_code, status (`active` | `expired` | `suspended`), starts_at, expires_at, created_at, updated_at.
- **`va_vehicles`:** id, member_id, customer_id, plate_normalized, plate_display, plate_state NULL, description NULL, active.
  - UNIQUE (customer_id, plate_normalized) WHERE active.
- **`va_tags`:** id, vehicle_id, member_id, customer_id, tag_epc (normalized hex), active, issued_at, revoked_at.
  - UNIQUE (customer_id, tag_epc) WHERE active.
- **`va_lanes`:** id, customer_id, site_id, appliance_id, name, camera_id (FK cameras), reader_id, output_id, direction, correlation_window_ms (default 4000), vehicle_cooldown_s (default 30), enabled.
- **`va_readers`:** id, customer_id, lane_id, kind (`simulated` | `llrp` | `tcp_ascii` | `serial` | `http`), host/port/serial params (no secrets in plain text; credentials encrypted like `camera_credentials`), antenna_ports, rssi_min, last_seen_at, health.
- **`va_outputs`:** id, customer_id, lane_id, kind (`simulated` | `relay_control` | `http`), channel, pulse_ms, last_command_at, state.
- **`va_rules`:** id, customer_id, site_id NULL, lane_id NULL (most specific wins), version, policy_json (§7), updated_by, updated_at.
- **`va_transactions`:**
  - id, local_transaction_id (edge id; UNIQUE per appliance), customer_id, site_id, lane_id;
  - started_at, decided_at;
  - plate_read, plate_confidence, plate_event_id (FK detection_events), tag_epc, tag_rssi;
  - member_id NULL, vehicle_id NULL, membership_status_at_decision;
  - evidence (`both_match` | `plate_only` | `tag_only` | `mismatch` | `none`);
  - decision (`granted` | `denied` | `review` | `mismatch`), reason_code, reason_text;
  - rule_version, snapshot_key, clip_event_id, gate_command_id NULL.
- **`va_gate_commands`:**
  - id, transaction_id, lane_id, output_id;
  - source (`auto` | `attendant`), actor (for attendant);
  - requested_at, idempotency_key (UNIQUE), result (`simulated` | `pulsed` | `failed` | `refused`), error;
  - immutable (an audit record).
- **`va_audit`:** append-only per transaction: at, actor, action, before/after decision, note.

Event lists never show account_ref, contact details or payment data, only display name and status.

## 4. APIs

Customer APIs are tenant-scoped with role checks; each module screen needs a `vehicle_access` entitlement.

- `GET /api/customer/vehicle-access/transactions?site=&lane=&decision=&q=&before=` returns paged rows.
- `GET /api/customer/vehicle-access/transactions/{id}` returns the full detail: snapshot, clip, plate, tag, member (minimal), decision, audit, gate commands.
- `POST /api/customer/vehicle-access/transactions/{id}/attendant` with `{action: grant|deny, note}`.
  - It uses a two-step confirm token like door unlock.
  - It is routed to the edge; the edge pulses and reports back.
- CRUD endpoints:
  - `/api/customer/vehicle-access/members` (+ `/vehicles`, `/tags`), with CSV import later;
  - `/api/customer/vehicle-access/lanes`, `/readers`, `/outputs` (with health);
  - `/api/customer/vehicle-access/rules` (versioned).
- Edge: the configuration payload gains `vehicle_access` (lanes, readers, outputs, rules, members/vehicles/tags for that appliance's sites, as a hash-diffed delta).
- Edge: transactions and gate commands go up as `analytics_sync` event types `vehicle_access_transaction` / `vehicle_access_gate_command` (idempotent).

## 5. UI

A new top-level nav item, **Vehicle Access**. It's shown only when entitled. It has four tabs.

1. **Activity**
   - A transaction table: time, lane, snapshot thumbnail, plate, LPR confidence, tag (masked, e.g. `…4F2A`), member display name, membership status, a decision chip (Granted / Denied / Review / Mismatch), a clip play button, and the reason.
   - Filters: lane, decision, date.
   - It live-updates via the existing recent-events polling pattern.
   - Clicking a row opens the detail drawer: snapshot, clip (event player), plate crop and reads, tag reads (RSSI, antenna), member card, rule applied, gate command result, audit timeline, and attendant Grant/Deny.
2. **Customers / Vehicles**
   - A member list with search and status filter.
   - The member page shows vehicles, plates, tags, plan, dates and access history.
   - Add/edit/suspend members, and issue or revoke tags.
3. **Gates & Readers**
   - Per site, the lanes with camera health (existing camera status), reader health/last seen, output state/last command, and a "Test (simulated)" button.
4. **Access Rules**
   - The policy editor from §7, with presets ("Strict", "Attendant-assisted").
   - A dry-run against the last N transactions shows what would have changed.

## 6. RFID adapter interface (vendor-neutral)

```python
class TagRead(NamedTuple):
    epc: str            # normalized upper-hex
    rssi: float | None
    antenna: int | None
    read_at: float      # monotonic
    reader_id: str

class RfidReader(Protocol):
    def start(self, on_read: Callable[[TagRead], None]) -> None   # background thread/async
    def stop(self) -> None
    def health(self) -> dict      # {"connected": bool, "last_read_at": ..., "error": ...}
```

- **Implementations:**
  - `SimulatedRfidReader`, for tests and the first milestone (reads injected via an admin/test API);
  - `LlrpReader`: LLRP over TCP 5084, the common standard for Impinj/Zebra and similar;
  - `TcpAsciiReader`: many low-cost UHF readers stream EPCs as text lines;
  - `SerialReader`;
  - `HttpPushReader`, for readers that POST reads.
- **Adapter responsibilities:**
  - de-duplicate raw bursts (the same EPC is read many times per pass);
  - apply `rssi_min` and antenna filtering;
  - normalize the EPC;
  - reconnect with backoff;
  - report health.
- No vendor is chosen yet.

## 7. Gate output interface

```python
class GateOutput(Protocol):
    def open(self, command: GateCommand) -> GateResult   # pulse; never "hold open"
    def health(self) -> dict
```

- **Implementations:**
  - `SimulatedGateOutput` (records the command, result `simulated`) is the ONLY one enabled in the first milestone;
  - `RelayControlGateOutput` wraps `relay_control.RelayProvider` (pulse_ms, channel), reusing its cooldown;
  - `HttpGateOutput`, for car-wash controllers that expose an API.
- Every call carries an `idempotency_key` = `lane_id:transaction_id`.

**Policy (configurable per site or lane, versioned):**

| Evidence | Membership | Default action | Configurable to |
|---|---|---|---|
| Plate and tag agree (same vehicle) | active | GRANT | — |
| Plate and tag agree | expired / suspended | DENY | REVIEW |
| Tag valid, plate belongs to a different vehicle or is unreadable-but-present | any | MISMATCH, then REVIEW | DENY |
| Plate recognized, no tag | active | REVIEW | GRANT (plate-only sites) / DENY |
| Tag recognized, plate uncertain (below the confirm threshold) | active | GRANT | REVIEW |
| Neither | — | DENY | REVIEW |

A mismatch is never auto-granted. The policy engine is data-driven: `policy_json` maps `(evidence, status)` to an action, and each rule must include a reason code.

## 8. Plate + RFID correlation (per lane)

- **A lane state machine:** IDLE, then COLLECTING (on the first plate read OR tag read), then DECIDED, then COOLDOWN.
- **COLLECTING** lasts `correlation_window_ms` (default 4 s) after the first read, or ends early once both a confirmed plate and a tag exist.
- **Plate input:**
  - uses the existing `confirm_plate` result, so it's already multi-frame, ≥80 confidence and deduplicated;
  - a sub-threshold best read is kept only as "uncertain" evidence, never as identity.
- **Tag input:** the strongest-RSSI EPC seen in the window (after adapter filtering).
- **Match rule:** `tag → vehicle` and `plate → vehicle` must resolve to the SAME `va_vehicles.id`.
  - A plate matching the tag's vehicle within one OCR edit, with confidence ≥80, counts as a match; the tag is the strong identity.
  - Anything else is a MISMATCH.
- **Duplicates:**
  - after DECIDED, the same tag or plate on the same lane within `vehicle_cooldown_s` joins the same transaction and never issues a second gate command;
  - the gate command idempotency key prevents double pulses across retries and restarts (persisted before the pulse).
- **Two vehicles close together:**
  - a new tag arriving while a transaction is collecting with a different tag closes the first transaction (with the evidence it has) and starts a new one;
  - a lane is single-file by design.

## 9. Security and fail-safe behaviour

- **Fail closed:** any exception, timeout, missing config, unreachable member data or rule-version mismatch produces NO gate command and a DENY/REVIEW transaction carrying the error reason. A gate command is only ever issued by an explicit GRANT (automatic or attendant) that has been persisted first.
- **Pulse only:** no latched "open" state exists in software. The output returns to closed on its own (relay pulse / controller-side timeout).
- **No double openings:** per-lane cooldown plus a persisted idempotency key before actuation, re-checked after a restart.
- **No OCR-only identity where RFID exists:** plate-only grants happen only if a site explicitly enables them.
- **Attendant actions:** two-step confirm, role-checked, audited with the actor; the edge still performs the pulse.
- **Network loss:** the edge keeps deciding from its last synced member data (with a configurable maximum staleness, after which it falls back to REVIEW), queues transactions, and never opens on cloud silence.
- **Tenant isolation:** car-wash operators' members are visible only to that operator. Event lists minimize PII; tags are masked.
- **Simulated-output banner:** until a real output is configured and approved, every screen shows "Gate output: SIMULATED".

## 10. Phases and effort (engineering days, one developer, with tests)

| Phase | Scope | Effort |
|---|---|---|
| **P1: decision core (simulated)** | Schema and migrations, member/vehicle/tag model, `SimulatedRfidReader`, correlator, policy engine, `SimulatedGateOutput`, transaction and audit records, reuse of the LPR plate event, snapshot and clip; unit tests and a lane simulator | 6–8 |
| **P2: UI and cloud** | Activity table and detail drawer, Customers/Vehicles, Gates & Readers (health), Rules editor with dry-run; cloud↔edge sync; entitlement and licence key; tenant-isolation tests | 7–10 |
| **P3: attendant and notifications** | Manual Grant/Deny (two-step, routed to edge), review queue, notifications/deep links for Denied/Review | 3–4 |
| **P4: real RFID** | One vendor adapter (LLRP or TCP-ASCII) plus a bench test with real tags; health and reconnect | 3–5 |
| **P5: real gate output** | `RelayControlGateOutput`/HTTP, bench validation on a test relay (not a live gate), then a supervised on-site test with explicit approval | 2–4 |
| **P6: multi-lane / multi-site hardening** | Lane concurrency, load test, CSV member import, reporting | 4–6 |

P1 plus the simulated end-to-end demo take about 1.5–2 weeks. A production-ready single lane (P1–P5) takes about 4–6 weeks.

## 11. Test strategy

- **Unit:** correlator timing (both, plate-only, tag-only, late tag, two vehicles back to back, repeated reads, window expiry), every policy cell, near-plate edit distance, idempotency across restart, and fail-closed on every injected error.
- **Integration:** the lane simulator feeds synthetic plate events (through the real `confirm_plate`) and tag reads, and asserts the transaction, audit, simulated gate command, snapshot and clip linkage.
- **Tenant isolation:** operator A can never read or grant operator B's members, lanes or transactions.
- **Sync:** the edge decides while the cloud is unreachable; replays are idempotent.
- **UI:** API contract tests plus browser tests of the Activity drill-down and attendant confirm.
- **Hardware (later, separately approved):** a reader bench test with known tags, then a relay bench test, and only then a supervised gate test.

## 12. Hardware for the physical prototype (one lane)

- **1 IP camera for plates,** positioned for a head-on or rear plate view, ideally with a dedicated LPR lens/IR. Existing AnyAiCam cameras are fine for the bench.
- **1 UHF RFID reader (860–960 MHz),** plus a directional antenna for the lane (e.g. a fixed reader with LLRP support) and PoE.
- **A few windshield UHF tags** (tamper-evident, destructible on removal, as car washes use).
- **1 relay output:**
  - the existing Numato 3-channel Ethernet relay already supported by `relay_control`; or
  - the car-wash controller's input (dry contact), or its API.
- **A test gate substitute** for the bench: an indicator lamp or buzzer on the relay. No real gate until P5 approval.
- **The existing AnyAiCam appliance (Ryzen)** on the same LAN.
