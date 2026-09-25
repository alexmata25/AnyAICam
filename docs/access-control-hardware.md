# Access-control hardware integration

Status (2026-09-25): software complete and tested against simulators and
protocol fakes. **No real door, relay, strike, maglock or Z-Wave device has
been operated.** Every new door is created in dry run.

Builds on `docs/aac-face-access-door-control-requirements.md`: fail closed,
audit every attempt, no unlock action from email.

## Modules

| Module | Role |
|---|---|
| `app/access_control.py` | `AccessControlService`: `get_status`, `health`, `unlock(door_id, duration, reason, actor, trigger)`, `lock`, `configure_door`, `recover`, `poll_inputs`; persistence (`DoorStore`); adapter factory; edge worker |
| `app/access_adapters.py` | `DoorAdapter` interface; `MockDoorAdapter` (simulator); `RelayDoorAdapter` over a `RelayDriver`; `NumatoUsbRelayDriver`; `MockRelayDriver`; serial discovery |
| `app/access_zwave.py` | `ZWaveJsClient` (zwave-js-server WebSocket), `ZWaveLockAdapter`, `ZWaveEnrollment` (S2 inclusion/exclusion), Z-Stick discovery |
| `app/access_control_api.py` | `/api/customer/access/...` routes and the `/customer/access-control` page |
| `app/door_access.py` | `trigger_door()`: the single dispatch point for the manual Unlock button, AAC Voice Call, AACO and facial rules |
| migration `20260925_access_control` | `access_doors`, `access_door_state`, `access_door_commands` (hardware command log), `access_door_events` (forced/held-open/REX/offline/relock-failed) |

Door kinds: `mock`, `zwave_lock`, `relay_strike`, `relay_maglock`. A door
may be linked to one camera. Its events go onto that camera's timeline as
`access_*` events, and viewer permission is that camera's `can_unlock`
grant. A camera with no configured door keeps the legacy relay path
unchanged.

## Fail-secure rules (enforced in `AccessControlService.unlock`)

1. Unknown, disabled or other-customer door → `denied`.
2. One command per door at a time; a second command during an unlock
   window → `duplicate`, never queued.
3. Controller or device offline, insecure, dead, or a different device on
   the enrolled node id → `offline`; nothing is sent.
4. Every non-manual trigger (face, AACO, AAC Voice Call) needs a known state: `unknown` or
   `jammed` → `uncertain_state`. A manual unlock by an authorized person on
   an unknown-state door is allowed. The human is looking at the camera.
5. `dry_run` doors log and stop. New doors are dry run until the owner
   turns it off.
6. Adapter timeout (`ANYAICAM_ACCESS_COMMAND_TIMEOUT_SECONDS`, default 8)
   or refusal → `timeout`/`failed`, then a best-effort lock.
7. The relock is persisted (`relock_due_at`) before it is scheduled. On
   restart `recover()` de-energizes every relay door and re-locks any
   overdue door.
8. Failed relock: 3 attempts, then the state becomes `unknown` and a
   `relock_failed` event is raised. Automatic unlocks on that door stop
   until the state is known again.
9. Facial rules keep their own gates (confidence, person, schedule,
   cooldown, `dry_run` default on) *before* the service's gates.

`ANYAICAM_ACCESS_CONTROL_ENABLED=false` disables the service entirely.

## Z-Wave path (residential)

Ryzen → Aeotec Z-Stick 7 (USB) → `zwave-js` container (Z-Wave JS UI,
owns the stick and the S0/S2 keys) → WebSocket `ws://zwave-js:3000` →
`ZWaveJsClient` in the VMS container.

* Serial path: `ANYAICAM_ZWAVE_STICK_BY_ID`, a `/dev/serial/by-id/...` path,
  mapped to `/dev/zwave`. It is never hard-coded to `/dev/ttyUSB0`.
  `GET /api/customer/access/zwave` lists candidate sticks. The Aeotec
  Z-Stick 7 is USB `0658:0200`.
* Inclusion uses strategy Default (S2, falling back to S0). The installer
  enters the lock's 5-digit DSK PIN in AnyAiCam. The PIN is never logged or
  audited. A lock that ends up included without S2 Access Control or S0 is
  reported as `insecure` and cannot be used as a door.
* The door stores `node_id` and the device identity (manufacturer, product
  type and product id). A different device on that node id is treated as
  offline.
* Commands are sent to Door Lock CC (98) `targetMode` (255 secured, 0
  unsecured). The state is read by polling `currentMode`. A Notification CC
  jammed event makes the state `jammed`.
* Supported targets: Kwikset SmartCode 914/916 **Z-Wave** models, and Yale
  and Schlage Z-Wave locks. The Kwikset 270 is not Z-Wave and cannot be
  used.
* Z-Wave offline detection happens when a command or status is requested:
  a dead node, a missing server or a timeout. Relay doors are also polled
  continuously.

## Relay path (commercial strike/maglock)

Ryzen → USB dry-contact relay module (Numato-protocol) → **access-control
power supply** → strike or maglock. **No lock current passes through the
Ryzen or the relay module's USB side.** The relay contact only signals the
power supply's trigger input.

* Door config: `driver` (`numato`), `path` (`/dev/serial/by-id/...`),
  `channel`, optional `dps_input` / `dps_closed_value`, `rex_input` /
  `rex_active_value`, and `held_open_seconds`.
* Unlock energizes the channel. The service's persisted relock
  de-energizes it after `unlock_seconds`, which is the pulse duration.
  Restart forces all relay doors off.
* Door-position switch (DPS) and request-to-exit (REX) inputs are polled
  every `ANYAICAM_ACCESS_INPUT_POLL_SECONDS`. They generate these events:
  * `door_forced`: the door opened without an unlock grant or REX;
  * `door_held_open`: the door stayed open longer than `held_open_seconds`;
  * `door_opened`, `door_closed`, `request_to_exit`;
  * `controller_offline` and `controller_online`.
* **Strike:** use a fail-secure strike, so a de-energized relay keeps the
  door locked. Fail-safe strikes are for doors the fire code requires to
  unlock on power loss; confirm with the AHJ.
* **Maglock:** configuring one requires `life_safety_confirmed: true`. The
  maglock must drop on the fire-alarm (FACP) input and on a local REX,
  both wired at the access-control power supply. **Maglock fire/life-safety
  release never depends on AnyAiCam, the network, Z-Wave or the VMS.**
  AnyAiCam only adds a convenience release.

## API

All routes are customer-scoped. An owner can do everything. A viewer can
unlock, lock and see history only on a door whose camera grants them
`can_unlock`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/customer/access/doors` | status of every door |
| GET | `/api/customer/access/doors/{id}` | status + health (+ config for owner) |
| POST | `/api/customer/access/doors/{id}/unlock` | `{duration_seconds?}`; 409 duplicate/dry_run/uncertain, 503 offline, 504 timeout, 502 failed |
| POST | `/api/customer/access/doors/{id}/lock` | |
| GET | `/api/customer/access/doors/{id}/history` | commands + events |
| POST | `/api/customer/access/doors` | owner; create/update (new ⇒ dry run) |
| DELETE | `/api/customer/access/doors/{id}` | owner; locks first |
| GET | `/api/customer/access/zwave` | controller health, nodes, stick candidates, enrollment |
| POST | `/api/customer/access/zwave/inclusion` | owner; `{action: start|pin|stop, pin?}` |
| POST | `/api/customer/access/zwave/exclusion` | owner; `{action: start|stop}` |
| GET | `/api/customer/access/relay/devices` | relay module candidates |

Every manual unlock also writes the existing `door_access_events` audit
row, which records actor, authorization result, relay result, success and
error.

**Known gap:** the service runs where the hardware is plugged in, which is
the edge appliance. A command from the cloud portal is not relayed to the
edge yet. On hardware day, use the appliance's local portal. Cloud-to-edge
command dispatch is a separate, later phase.

## Hardware-day validation checklist

Do these in order. Stop at the first failure. Every door stays in dry run
until step D6 or R8.

### Z-Wave (Z-Stick 7 + deadbolt)
- **Z1.** Confirm the lock is a Z-Wave model (Kwikset 914/916 Z-Wave, Yale
  or Schlage Z-Wave), not a Kwikset 270. Fresh batteries. The door stays
  open during testing.
- **Z2.** Plug in the Z-Stick. Run `ls -l /dev/serial/by-id/` and note the
  `usb-0658_0200...` path.
- **Z3.** Create `/var/lib/anyaicam/zwave-js`. Set `ZWAVE_JS_UI_TAG`
  (pinned) and `ANYAICAM_ZWAVE_STICK_BY_ID`. Start the overlay:
  `docker compose -f docker-compose.yml -f deploy/access-control/docker-compose.access-control.example.yml up -d`.
  For a Z-Wave-only site, remove the relay `devices` line first.
- **Z4.** In Z-Wave JS UI (`ssh -L 8091:127.0.0.1:8091`, then open
  localhost:8091): set the serial port to `/dev/zwave`, generate the S0/S2
  keys, enable WS Server on port 3000. Back up the store directory.
- **Z5.** Run `GET /api/customer/access/zwave`. Expect
  `controller_online: true`, a home id and the stick in the candidates.
- **Z6.** Include the lock with `POST .../zwave/inclusion {action:start}`.
  Put the lock in pairing mode. When the state is `awaiting_pin`, send the
  DSK PIN from the lock's label. Expect `included`, `is_lock: true` and
  security class S2 Access Control (2). If the state is `insecure`, exclude
  and retry.
- **Z7.** Create the door: `kind: zwave_lock`, `config.node_id` and
  `config.identity` from the inclusion result, and the linked camera.
  Leave `dry_run` on.
- **Z8.** `GET /doors/{id}` should show online, secure and the real state.
  Turn the thumb-turn by hand, and the state should follow.
- **Z9.** Unlock in dry run. Expect 409 `dry_run`, the bolt doesn't move,
  and the command is logged.
- **Z10.** Turn `dry_run` off. Unlock with `duration_seconds: 10`. The
  bolt retracts, and after 10 s it extends. History shows unlock, then
  relock.
- **Z11.** Unlock twice quickly. The second returns 409 `duplicate`.
- **Z12.** Remove the lock's batteries. Status goes offline or dead
  (Z-Wave may take a few minutes to mark it dead). Unlock returns 503, and
  nothing is sent.
- **Z13.** Stop the zwave-js container. Unlock returns 503 `offline`.
  Start it again, and status recovers.
- **Z14.** Jam the bolt, for example by holding the door ajar against it,
  and lock. Expect `jammed`. A face-triggered unlock is then refused
  (`uncertain_state`).
- **Z15.** Restart the VMS container during a 60 s unlock. After startup
  the door is re-locked (`recover`).
- **Z16.** Exclusion: `POST .../zwave/exclusion {action:start}`, then put
  the lock in exclusion mode. Expect `excluded`.

### Relay + strike/maglock
- **R1.** The installer (licensed where required) wires the strike or
  maglock to the **access-control power supply**. The relay module's dry
  contact (NO/COM) goes to the power supply's trigger input only. Nothing
  from the lock circuit touches the Ryzen.
- **R2.** For a maglock, the installer verifies before any AnyAiCam step
  that the FACP input and the local REX release the lock with the Ryzen
  **unplugged**. Record this.
- **R3.** Plug in the relay module. Run `ls -l /dev/serial/by-id/` and set
  `ANYAICAM_RELAY_BY_ID`. Start the overlay. `GET .../relay/devices` lists
  the module.
- **R4.** Create the door: `relay_strike` or `relay_maglock` (the latter
  needs `life_safety_confirmed: true`), `driver: numato`, `path`,
  `channel`, `dps_input` and `rex_input` if wired, `held_open_seconds`,
  `unlock_seconds` of about 5, and the linked camera. Leave `dry_run` on.
- **R5.** `GET /doors/{id}`: the firmware version is shown, and the module
  is online.
- **R6.** Open and close the door by hand. `door_opened` without a grant
  should come up as `door_forced`, then `door_closed`. Hold it open past
  `held_open_seconds` for `door_held_open`. Press REX for
  `request_to_exit`, and the following opening is not forced. Each event
  appears on the linked camera's timeline.
- **R7.** Unlock in dry run. The relay LED doesn't change.
- **R8.** Turn `dry_run` off. Unlock. The relay LED comes on, the lock
  releases, and after `unlock_seconds` the relay goes off and the lock
  re-secures. Open the door during the window: `door_opened`, not forced.
- **R9.** Unplug the relay module's USB. Expect `controller_offline`, and
  unlock returns 503. Plug it back in to get `controller_online`.
- **R10.** Restart the VMS container while the relay is energized. At
  startup the relay is forced off.
- **R11.** For a maglock, repeat the fire-alarm release test with AnyAiCam
  running and during an AnyAiCam-issued unlock. It must behave identically.

### Face recognition (either path)
- **F1.** Enroll a person. Create a facial rule on the door's camera
  (specific person, min_confidence, schedule), with `dry_run` on.
- **F2.** Walk up. The rule evaluates, and nothing moves. The dry-run
  outcome is logged.
- **F3.** Turn off the rule's `dry_run` and the door's `dry_run`. Walk up,
  and the door unlocks, then relocks. The command row shows
  `trigger_type=automatic`, `actor=facial_recognition`, the person and the
  facial event id.
- **F4.** An unenrolled person, a partly covered face (low confidence) or a
  time outside the schedule leaves the door locked.
- **F5.** With the lock offline or jammed, a known face leaves the door
  locked, with result `offline` or `uncertain_state`.
