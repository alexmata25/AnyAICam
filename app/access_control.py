"""AnyAiCam access-control service (2026-09-25).

One door-level API for every consumer -- the live-tile Unlock button,
facial-recognition access rules, AAC Voice Call, AACO and the access API --
independent of the hardware behind a door:

    get_status(door_id)   health(door_id)   lock(door_id, ...)
    unlock(door_id, duration_seconds, reason, actor, trigger=...)
    configure_door(...)   (Z-Wave enrollment: access_zwave.ZWaveEnrollment)

Adapters (access_adapters / access_zwave) do the hardware: "zwave_lock"
(residential Z-Wave deadbolt), "relay_strike" / "relay_maglock"
(commercial door on a dry-contact relay) and "mock" (simulator, so the
whole workflow runs before any hardware exists).

A camera door that has NOT been configured here keeps working exactly as
before through relay_control.get_provider() (door_access.trigger_door()).

Fail-secure rules (every one enforced here, not by callers):
  - no unlock for a missing/disabled door, an offline controller or device,
    or when health cannot be read;
  - an AUTOMATIC unlock (face recognition) additionally needs a known lock
    state (locked/unlocked) -- unknown or jammed means no;
  - one command per door at a time: a concurrent or repeated unlock is
    refused as a duplicate, never queued;
  - a command that times out or fails is followed by a lock attempt;
  - every unlock schedules a relock, persisted so a restart re-locks; lock
    retries, and a relock that still fails marks the door "unknown" and
    raises a relock_failed event;
  - on startup every relay door's output is forced off and every door with
    an unfinished unlock is re-locked before anything else;
  - a door marked dry_run never touches hardware.
Every command (and every refusal) is written to access_door_commands;
door-level events (unlocked, forced, held open, REX, offline, relock
failed) to access_door_events and, through event_sink, to the camera's
Events/Playback timeline.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone

from access_adapters import (JAMMED, KNOWN_STATES, LOCK_TYPES, LOCKED, UNKNOWN, UNLOCKED, AdapterError,
                             MockDoorAdapter, MockRelayDriver, NumatoUsbRelayDriver, RelayDoorAdapter)

DOOR_KINDS = ("mock", "zwave_lock", "relay_strike", "relay_maglock")
DEFAULT_UNLOCK_SECONDS = 5
MAX_UNLOCK_SECONDS = 300
COMMAND_TIMEOUT_SECONDS = float(os.environ.get("ANYAICAM_ACCESS_COMMAND_TIMEOUT_SECONDS", "8"))
LOCK_RETRIES = 3
DEFAULT_HELD_OPEN_SECONDS = 30
SERVICE_ENABLED = os.environ.get("ANYAICAM_ACCESS_CONTROL_ENABLED", "true").strip().lower() == "true"


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Door:
    id: str
    customer_id: str
    name: str
    kind: str
    config: dict
    camera_id: str | None = None
    unlock_seconds: int = DEFAULT_UNLOCK_SECONDS
    enabled: bool = True
    dry_run: bool = False


@dataclass
class Outcome:
    ok: bool
    result: str               # unlocked|locked|dry_run|denied|duplicate|offline|uncertain_state|timeout|failed
    door_id: str | None
    detail: str | None = None
    command_id: str | None = None
    state: str | None = None
    relock_at: str | None = None


def validate_door(door: Door) -> None:
    if door.kind not in DOOR_KINDS:
        raise ValueError(f"kind must be one of {DOOR_KINDS}")
    if not door.name.strip():
        raise ValueError("a door needs a name")
    if not (1 <= int(door.unlock_seconds) <= MAX_UNLOCK_SECONDS):
        raise ValueError(f"unlock_seconds must be between 1 and {MAX_UNLOCK_SECONDS}")
    cfg = door.config
    if door.kind == "zwave_lock":
        if not isinstance(cfg.get("node_id"), int) or cfg["node_id"] < 2:
            raise ValueError("a Z-Wave lock needs the node_id it was included as")
    if door.kind in ("relay_strike", "relay_maglock"):
        if cfg.get("driver") not in ("numato", "mock"):
            raise ValueError("relay driver must be 'numato' or 'mock'")
        if cfg.get("driver") == "numato" and not str(cfg.get("path") or "").strip():
            raise ValueError("a relay module needs its serial path (/dev/serial/by-id/...)")
        if not isinstance(cfg.get("channel"), int) or cfg["channel"] < 0:
            raise ValueError("a relay door needs its relay channel number")
        for key in ("dps_input", "rex_input"):
            if cfg.get(key) is not None and (not isinstance(cfg[key], int) or cfg[key] < 0):
                raise ValueError(f"{key} must be an input number")
    if door.kind == "relay_maglock" and cfg.get("life_safety_confirmed") is not True:
        raise ValueError("A maglock must release through the fire-alarm interface and a REX device wired at the "
                         "access-control power supply, independent of AnyAiCam. Confirm that wiring "
                         "(life_safety_confirmed) before configuring it.")


# ------------------------------------------------------------------ persistence

class DoorStore:
    """SQL persistence (partner_db connection factory injected so tests use
    their own database)."""

    def __init__(self, connection):
        self._connection = connection

    def _row_to_door(self, row) -> Door:
        return Door(id=row["id"], customer_id=row["customer_id"], name=row["name"], kind=row["kind"],
                    config=json.loads(row["config_json"] or "{}"), camera_id=row["camera_id"],
                    unlock_seconds=row["unlock_seconds"], enabled=bool(row["enabled"]), dry_run=bool(row["dry_run"]))

    def doors(self, customer_id: str | None = None) -> list[Door]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM access_doors" + (" WHERE customer_id=?" if customer_id else "") + " ORDER BY name",
                              (customer_id,) if customer_id else ()).fetchall()
        return [self._row_to_door(row) for row in rows]

    def door(self, door_id: str) -> Door | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM access_doors WHERE id=?", (door_id,)).fetchone()
        return self._row_to_door(row) if row else None

    def door_for_camera(self, camera_id: str) -> Door | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM access_doors WHERE camera_id=? AND enabled=1", (camera_id,)).fetchone()
        return self._row_to_door(row) if row else None

    def save(self, door: Door, now: str) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO access_doors(id,customer_id,camera_id,name,kind,config_json,unlock_seconds,enabled,dry_run,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET camera_id=excluded.camera_id,name=excluded.name,"
                "kind=excluded.kind,config_json=excluded.config_json,unlock_seconds=excluded.unlock_seconds,"
                "enabled=excluded.enabled,dry_run=excluded.dry_run,updated_at=excluded.updated_at",
                (door.id, door.customer_id, door.camera_id, door.name, door.kind, json.dumps(door.config, sort_keys=True),
                 int(door.unlock_seconds), 1 if door.enabled else 0, 1 if door.dry_run else 0, now, now))
            if door.camera_id and door.enabled:
                # The camera becomes a door for the live tile, face rules, AACO.
                db.execute("UPDATE cameras SET door_access_enabled=1 WHERE id=? AND customer_id=?", (door.camera_id, door.customer_id))

    def delete(self, door_id: str) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM access_doors WHERE id=?", (door_id,))
            db.execute("DELETE FROM access_door_state WHERE door_id=?", (door_id,))

    def state(self, door_id: str) -> dict:
        with self._connection() as db:
            row = db.execute("SELECT * FROM access_door_state WHERE door_id=?", (door_id,)).fetchone()
        return dict(row) if row else {"door_id": door_id, "state": UNKNOWN, "relock_due_at": None}

    def set_state(self, door_id: str, state: str, relock_due_at: float | None, now: str) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO access_door_state(door_id,state,relock_due_at,updated_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(door_id) DO UPDATE SET state=excluded.state,relock_due_at=excluded.relock_due_at,updated_at=excluded.updated_at",
                       (door_id, state, relock_due_at, now))

    def log_command(self, row: dict) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO access_door_commands(id,door_id,customer_id,camera_id,command,trigger_type,actor,reason,"
                       "person_id,facial_event_id,result,detail,duration_seconds,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (row["id"], row["door_id"], row.get("customer_id"), row.get("camera_id"), row["command"], row.get("trigger_type"),
                        row.get("actor"), row.get("reason"), row.get("person_id"), row.get("facial_event_id"), row["result"],
                        row.get("detail"), row.get("duration_seconds"), row["created_at"]))

    def log_event(self, door: Door, event_type: str, detail: dict, now: str) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO access_door_events(id,door_id,customer_id,camera_id,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex[:16], door.id, door.customer_id, door.camera_id, event_type, json.dumps(detail, sort_keys=True), now))


# ------------------------------------------------------------------ scheduling

class ThreadScheduler:
    """Keyed one-shot timers (a newer schedule for the same key replaces the old)."""
    def __init__(self):
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def schedule(self, key: str, delay: float, fn) -> None:
        with self._lock:
            old = self._timers.pop(key, None)
            if old:
                old.cancel()
            timer = threading.Timer(max(0.0, delay), fn)
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def cancel(self, key: str) -> None:
        with self._lock:
            old = self._timers.pop(key, None)
            if old:
                old.cancel()


# ------------------------------------------------------------------ adapters

class AdapterFactory:
    """Builds (and caches) an adapter per door; hardware clients are shared
    (one Z-Wave JS connection, one driver per relay module)."""
    def __init__(self, *, zwave_client=None, relay_driver_factory=None, mock_adapters: dict | None = None):
        self._zwave_client = zwave_client
        self._relay_driver_factory = relay_driver_factory
        self._drivers: dict[str, object] = {}
        self._adapters: dict[str, object] = {}
        self.mock_adapters = mock_adapters if mock_adapters is not None else {}
        self._lock = threading.Lock()

    def zwave_client(self):
        if self._zwave_client is None:
            import access_zwave
            self._zwave_client = access_zwave.ZWaveJsClient()
        return self._zwave_client

    def _driver(self, cfg: dict):
        key = f"{cfg.get('driver')}:{cfg.get('path') or 'mock'}"
        if key not in self._drivers:
            if self._relay_driver_factory is not None:
                self._drivers[key] = self._relay_driver_factory(cfg)
            elif cfg.get("driver") == "mock":
                self._drivers[key] = MockRelayDriver()
            else:
                self._drivers[key] = NumatoUsbRelayDriver(cfg["path"])
        return self._drivers[key]

    def invalidate(self, door_id: str) -> None:
        with self._lock:
            self._adapters.pop(door_id, None)

    def adapter(self, door: Door):
        with self._lock:
            if door.id in self._adapters:
                return self._adapters[door.id]
            cfg = door.config
            if door.kind == "mock":
                adapter = self.mock_adapters.setdefault(door.id, MockDoorAdapter())
            elif door.kind == "zwave_lock":
                import access_zwave
                adapter = access_zwave.ZWaveLockAdapter(self.zwave_client(), node_id=cfg["node_id"],
                                                        expected_identity=cfg.get("identity") or {})
            else:
                adapter = RelayDoorAdapter(self._driver(cfg), channel=cfg["channel"],
                                           lock_type="maglock" if door.kind == "relay_maglock" else "strike",
                                           dps_input=cfg.get("dps_input"), dps_closed_value=cfg.get("dps_closed_value", True),
                                           rex_input=cfg.get("rex_input"), rex_active_value=cfg.get("rex_active_value", True))
            self._adapters[door.id] = adapter
            return adapter


# ------------------------------------------------------------------ service

@dataclass
class _InputState:
    open_since: float | None = None
    held_reported: bool = False
    grant_until: float = 0.0
    online: bool | None = None


class AccessControlService:
    def __init__(self, *, store: DoorStore, adapters: AdapterFactory, clock=time.time, scheduler=None,
                 event_sink=None, command_timeout: float = COMMAND_TIMEOUT_SECONDS, lock_retries: int = LOCK_RETRIES,
                 retry_pause: float = 0.5):
        self.store = store
        self.adapters = adapters
        self.clock = clock
        self.scheduler = scheduler or ThreadScheduler()
        self.event_sink = event_sink
        self.command_timeout = command_timeout
        self.lock_retries = max(1, lock_retries)
        self.retry_pause = retry_pause
        self._door_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="access-cmd")
        self._inputs: dict[str, _InputState] = {}

    # -- plumbing
    def _now(self) -> str:
        return _utc(self.clock())

    def _door_lock(self, door_id: str) -> threading.Lock:
        with self._guard:
            return self._door_locks.setdefault(door_id, threading.Lock())

    def _call(self, fn, *args):
        """Run an adapter call with the command timeout; a hung device can
        never hang the caller."""
        future = self._executor.submit(fn, *args)
        try:
            return future.result(timeout=self.command_timeout)
        except FutureTimeout as error:
            raise TimeoutError(f"no answer within {self.command_timeout:.0f}s") from error

    def _event(self, door: Door, event_type: str, **detail) -> None:
        now = self._now()
        self.store.log_event(door, event_type, detail, now)
        if self.event_sink:
            try:
                self.event_sink(door, event_type, detail)
            except Exception:
                pass

    def _record(self, door: Door | None, door_id: str, command: str, outcome: Outcome, **context) -> Outcome:
        outcome.command_id = uuid.uuid4().hex[:16]
        self.store.log_command({
            "id": outcome.command_id, "door_id": door_id, "customer_id": door.customer_id if door else None,
            "camera_id": door.camera_id if door else None, "command": command, "result": outcome.result,
            "detail": outcome.detail, "created_at": self._now(), **context})
        return outcome

    # -- configuration
    def list_doors(self, customer_id: str | None = None) -> list[Door]:
        return self.store.doors(customer_id)

    def door(self, door_id: str) -> Door | None:
        return self.store.door(door_id)

    def door_for_camera(self, camera_id: str) -> Door | None:
        return self.store.door_for_camera(camera_id)

    def configure_door(self, door: Door) -> Door:
        validate_door(door)
        self.store.save(door, self._now())
        self.adapters.invalidate(door.id)
        return door

    def remove_door(self, door_id: str) -> None:
        self.scheduler.cancel(f"relock:{door_id}")
        self.store.delete(door_id)
        self.adapters.invalidate(door_id)

    # -- status
    def health(self, door_id: str) -> dict:
        door = self.store.door(door_id)
        if door is None:
            return {"door_id": door_id, "online": False, "error": "unknown door"}
        try:
            info = dict(self._call(self.adapters.adapter(door).health))
        except Exception as error:
            info = {"online": False, "controller_online": False, "error": str(error)}
        info.setdefault("online", False)
        info.update(door_id=door.id, name=door.name, kind=door.kind, dry_run=door.dry_run, enabled=door.enabled)
        return info

    def _read_state(self, door: Door, attempts: int = 2) -> str:
        adapter = self.adapters.adapter(door)
        for attempt in range(attempts):
            try:
                state = self._call(adapter.get_state)
                return state if state in (LOCKED, UNLOCKED, JAMMED) else UNKNOWN
            except Exception:
                if attempt + 1 < attempts:
                    time.sleep(self.retry_pause)
        return UNKNOWN

    def get_status(self, door_id: str) -> dict:
        door = self.store.door(door_id)
        if door is None:
            return {"door_id": door_id, "state": UNKNOWN, "online": False, "error": "unknown door"}
        health = self.health(door_id)
        state = self._read_state(door) if health.get("online") else UNKNOWN
        stored = self.store.state(door_id)
        relock = stored.get("relock_due_at")
        return {"door_id": door.id, "name": door.name, "kind": door.kind, "camera_id": door.camera_id,
                "state": state, "online": bool(health.get("online")),
                "controller_online": bool(health.get("controller_online", health.get("online"))),
                "relock_at": _utc(relock) if relock else None, "dry_run": door.dry_run, "enabled": door.enabled}

    # -- commands
    def unlock(self, door_id: str, *, duration_seconds: float | None = None, reason: str = "", actor: str = "",
               trigger: str = "manual", person_id: str | None = None, facial_event_id: str | None = None) -> Outcome:
        context = {"trigger_type": trigger, "actor": actor, "reason": reason, "person_id": person_id,
                   "facial_event_id": facial_event_id}
        door = self.store.door(door_id)
        if door is None or not door.enabled:
            return self._record(door, door_id, "unlock", Outcome(False, "denied", door_id, "door is not configured or disabled"), **context)
        seconds = int(round(duration_seconds)) if duration_seconds else door.unlock_seconds
        seconds = max(1, min(MAX_UNLOCK_SECONDS, seconds))
        context["duration_seconds"] = seconds
        door_lock = self._door_lock(door.id)
        if not door_lock.acquire(blocking=False):
            return self._record(door, door.id, "unlock", Outcome(False, "duplicate", door.id, "another command for this door is in progress"), **context)
        try:
            stored = self.store.state(door.id)
            if stored.get("relock_due_at") and stored.get("state") == UNLOCKED:
                return self._record(door, door.id, "unlock", Outcome(
                    False, "duplicate", door.id, "the door is already unlocked", state=UNLOCKED,
                    relock_at=_utc(stored["relock_due_at"])), **context)
            health = self.health(door.id)
            if not health.get("online"):
                self._event(door, "offline", error=health.get("error"))
                return self._record(door, door.id, "unlock", Outcome(False, "offline", door.id, health.get("error") or "controller or device offline"), **context)
            # Everything except a person pressing Unlock (face rules, AACO,
            # AAC Voice Call) needs a known lock state.
            if trigger != "manual":
                state = self._read_state(door)
                if state not in KNOWN_STATES:
                    return self._record(door, door.id, "unlock", Outcome(
                        False, "uncertain_state", door.id, f"lock state is {state}; automatic unlock refused", state=state), **context)
            if door.dry_run:
                return self._record(door, door.id, "unlock", Outcome(False, "dry_run", door.id, "dry run: no hardware command sent"), **context)
            adapter = self.adapters.adapter(door)
            try:
                self._call(adapter.unlock, seconds)
            except TimeoutError as error:
                self._secure_after_failure(door)
                return self._record(door, door.id, "unlock", Outcome(False, "timeout", door.id, str(error)), **context)
            except Exception as error:
                self._secure_after_failure(door)
                return self._record(door, door.id, "unlock", Outcome(False, "failed", door.id, str(error)), **context)
            due = self.clock() + seconds
            self.store.set_state(door.id, UNLOCKED, due, self._now())
            self.scheduler.schedule(f"relock:{door.id}", seconds, lambda: self._relock(door.id))
            state = self._inputs.setdefault(door.id, _InputState())
            state.grant_until = max(state.grant_until, due + 2)
            self._event(door, "unlocked", trigger=trigger, actor=actor, person_id=person_id,
                        facial_event_id=facial_event_id, duration_seconds=seconds)
            return self._record(door, door.id, "unlock", Outcome(True, "unlocked", door.id, state=UNLOCKED, relock_at=_utc(due)), **context)
        finally:
            door_lock.release()

    def _secure_after_failure(self, door: Door) -> None:
        """After a failed/timed-out unlock the real state is uncertain: try to
        secure it (best effort; the command itself is already a failure)."""
        try:
            self._call(self.adapters.adapter(door).lock)
            self.store.set_state(door.id, LOCKED, None, self._now())
        except Exception:
            self.store.set_state(door.id, UNKNOWN, None, self._now())

    def lock(self, door_id: str, *, reason: str = "", actor: str = "", trigger: str = "manual") -> Outcome:
        context = {"trigger_type": trigger, "actor": actor, "reason": reason}
        door = self.store.door(door_id)
        if door is None:
            return self._record(None, door_id, "lock", Outcome(False, "denied", door_id, "door is not configured"), **context)
        self.scheduler.cancel(f"relock:{door.id}")
        with self._door_lock(door.id):
            if door.dry_run:
                self.store.set_state(door.id, LOCKED, None, self._now())
                return self._record(door, door.id, "lock", Outcome(True, "dry_run", door.id, "dry run: no hardware command sent", state=LOCKED), **context)
            adapter = self.adapters.adapter(door)
            last_error = None
            for attempt in range(self.lock_retries):
                try:
                    self._call(adapter.lock)
                    self.store.set_state(door.id, LOCKED, None, self._now())
                    return self._record(door, door.id, "lock", Outcome(True, "locked", door.id, state=LOCKED), **context)
                except Exception as error:
                    last_error = str(error)
                    if attempt + 1 < self.lock_retries:
                        time.sleep(self.retry_pause)
            self.store.set_state(door.id, UNKNOWN, self.store.state(door.id).get("relock_due_at"), self._now())
            self._event(door, "relock_failed" if trigger == "relock" else "lock_failed", error=last_error)
            return self._record(door, door.id, "lock", Outcome(False, "failed", door.id, last_error, state=UNKNOWN), **context)

    def _relock(self, door_id: str) -> Outcome:
        return self.lock(door_id, reason="timed relock", actor="system", trigger="relock")

    # -- restart recovery
    def recover(self) -> list[Outcome]:
        """Called once at startup, before any other command: relay outputs
        forced off, unfinished unlocks re-locked (or re-scheduled if their
        time is not up yet)."""
        outcomes = []
        now = self.clock()
        for door in self.store.doors():
            if not door.enabled:
                continue
            stored = self.store.state(door.id)
            due = stored.get("relock_due_at")
            if door.kind in ("relay_strike", "relay_maglock") or (due and due <= now) or stored.get("state") == UNKNOWN and due:
                outcomes.append(self.lock(door.id, reason="startup recovery", actor="system", trigger="recovery"))
            elif due:
                self.scheduler.schedule(f"relock:{door.id}", due - now, lambda door_id=door.id: self._relock(door_id))
        return outcomes

    # -- relay door inputs
    def poll_inputs(self, door_id: str, held_open_seconds: float | None = None) -> list[str]:
        """Door-position / REX supervision for one door; returns the events
        raised. forced: opened while no unlock/REX grant was active. held
        open: open longer than held_open_seconds (once per opening)."""
        door = self.store.door(door_id)
        if door is None or not door.enabled:
            return []
        state = self._inputs.setdefault(door.id, _InputState())
        raised = []
        try:
            inputs = self._call(self.adapters.adapter(door).read_inputs)
            online = True
        except Exception as error:
            inputs, online = {}, False
            if state.online is not False:
                self._event(door, "controller_offline", error=str(error))
                raised.append("controller_offline")
        if online and state.online is False:
            self._event(door, "controller_online")
            raised.append("controller_online")
        state.online = online
        if not online:
            return raised
        now = self.clock()
        if inputs.get("rex"):
            if now >= state.grant_until:
                self._event(door, "request_to_exit")
                raised.append("request_to_exit")
            state.grant_until = max(state.grant_until, now + float(door.config.get("rex_grant_seconds", 10)))
        door_open = inputs.get("door_open")
        if door_open is None:
            return raised
        held_limit = float(held_open_seconds or door.config.get("held_open_seconds", DEFAULT_HELD_OPEN_SECONDS))
        if door_open and state.open_since is None:
            state.open_since = now
            state.held_reported = False
            if now > state.grant_until:
                self._event(door, "door_forced")
                raised.append("door_forced")
            else:
                self._event(door, "door_opened")
                raised.append("door_opened")
        elif door_open and not state.held_reported and now - state.open_since >= held_limit:
            state.held_reported = True
            self._event(door, "door_held_open", seconds=round(now - state.open_since))
            raised.append("door_held_open")
        elif not door_open and state.open_since is not None:
            state.open_since = None
            self._event(door, "door_closed")
            raised.append("door_closed")
        return raised


# ------------------------------------------------------------------ process singleton

_service: AccessControlService | None = None
_service_lock = threading.Lock()


def get_service() -> AccessControlService | None:
    """The appliance's service (None when disabled). Built lazily on the
    partner_db connection; main.py wires the event sink and runs recover()
    and the input poller at startup."""
    global _service
    if not SERVICE_ENABLED:
        return None
    with _service_lock:
        if _service is None:
            from partner_db import connection
            _service = AccessControlService(store=DoorStore(connection), adapters=AdapterFactory())
        return _service


def set_service(service: AccessControlService | None) -> None:
    """Tests and startup wiring."""
    global _service
    with _service_lock:
        _service = service


def new_door_id() -> str:
    return "door_" + uuid.uuid4().hex[:12]


INPUT_POLL_SECONDS = max(0.5, float(os.environ.get("ANYAICAM_ACCESS_INPUT_POLL_SECONDS", "1.0")))


async def edge_worker(event_sink=None, log=print) -> None:
    """Edge appliance background task: restart recovery FIRST (relay
    outputs off, unfinished unlocks re-locked), then door-position/REX
    supervision for relay doors. Housekeeping failures are logged, never
    raised into the VMS."""
    import asyncio
    service = get_service()
    if service is None:
        return
    if event_sink is not None:
        service.event_sink = event_sink
    try:
        outcomes = await asyncio.to_thread(service.recover)
        failed = [o.door_id for o in outcomes if not o.ok]
        if outcomes:
            log(f"access_control: startup recovery secured {len(outcomes) - len(failed)} door(s)"
                + (f", FAILED for {failed}" if failed else ""))
    except Exception as error:
        log(f"access_control: startup recovery failed: {error}")
    while True:
        try:
            doors = await asyncio.to_thread(service.list_doors)
            for door in doors:
                if door.enabled and door.kind in ("relay_strike", "relay_maglock", "mock") and (
                        door.config.get("dps_input") is not None or door.config.get("rex_input") is not None or door.kind == "mock"):
                    await asyncio.to_thread(service.poll_inputs, door.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log(f"access_control: input poll failed: {error}")
        await asyncio.sleep(INPUT_POLL_SECONDS)
