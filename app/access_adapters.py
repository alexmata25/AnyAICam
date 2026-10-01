"""Door hardware adapters for the AnyAiCam access-control service (2026-09-25).

access_control.AccessControlService is the only caller. It speaks to every
door through DoorAdapter, so facial recognition, the Unlock button, AAC
Voice Call and AACO never know whether a door is a residential Z-Wave lock
(access_zwave.ZWaveLockAdapter), a commercial strike/maglock behind a
dry-contact relay (RelayDoorAdapter below) or the simulator
(MockDoorAdapter).

Adapter contract (every method may raise AdapterError; the service turns
any exception or timeout into a fail-secure outcome):
  health()          -> {"online": bool, "controller_online": bool, ...}
  get_state()       -> "locked" | "unlocked" | "unknown" | "jammed"
  unlock(seconds)   -> None   release now and return
  lock()            -> None   secure now
  read_inputs()     -> {"door_open": bool|None, "rex": bool|None}
Every door type is re-locked the same way: unlock() releases and returns at
once, and the service's persisted, restart-safe relock timer calls lock()
when the configured time is up.

Lock power never flows through the appliance: a relay adapter only opens or
closes a dry contact on a separate access-control power supply/controller.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass, field

LOCKED, UNLOCKED, UNKNOWN, JAMMED = "locked", "unlocked", "unknown", "jammed"
KNOWN_STATES = (LOCKED, UNLOCKED)


class AdapterError(Exception):
    """Hardware could not be reached, or refused/failed the command."""


class DoorAdapter:
    kind = "abstract"

    def health(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def get_state(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def unlock(self, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def lock(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read_inputs(self) -> dict:
        return {"door_open": None, "rex": None}


# ------------------------------------------------------------------ simulator

@dataclass
class MockDoorAdapter(DoorAdapter):
    """Simulator for development and tests: a lock or a relay door with no
    hardware at all. Every failure mode the service must handle can be
    switched on: controller missing, device offline, command hang
    (timeout), jammed or unknown state, refused command, door open/REX."""
    kind: str = "mock"
    state: str = LOCKED
    controller_online: bool = True
    device_online: bool = True
    hang_seconds: float = 0.0          # simulate a command that never answers in time
    refuse: bool = False               # device answers but refuses the command
    stick_state_after_unlock: str | None = None  # e.g. JAMMED
    door_open: bool | None = False
    rex: bool | None = False
    calls: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _check(self):
        if not self.controller_online:
            raise AdapterError("controller not connected")
        if not self.device_online:
            raise AdapterError("device offline")

    def health(self) -> dict:
        return {"online": self.controller_online and self.device_online,
                "controller_online": self.controller_online, "device_online": self.device_online, "adapter": "mock"}

    def get_state(self) -> str:
        self._check()
        return self.state

    def unlock(self, seconds: float) -> None:
        with self._lock:
            self.calls.append(("unlock", seconds))
        if self.hang_seconds:
            time.sleep(self.hang_seconds)
        self._check()
        if self.refuse:
            raise AdapterError("device refused the command")
        self.state = self.stick_state_after_unlock or UNLOCKED

    def lock(self) -> None:
        with self._lock:
            self.calls.append(("lock", None))
        if self.hang_seconds:
            time.sleep(self.hang_seconds)
        self._check()
        if self.refuse:
            raise AdapterError("device refused the command")
        self.state = LOCKED

    def read_inputs(self) -> dict:
        self._check()
        return {"door_open": self.door_open, "rex": self.rex}


# ------------------------------------------------------------------ relay drivers

class RelayDriver:
    """One relay/IO module. Channels and inputs are numbered as the module
    numbers them; the door configuration says which are used."""
    def open(self) -> None: ...
    def close(self) -> None: ...

    def set_output(self, channel: int, energized: bool) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read_output(self, channel: int) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def read_input(self, index: int) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def health(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class NumatoUsbRelayDriver(RelayDriver):
    """Numato Lab USB relay/GPIO modules (the AIC-RELAY-NUMATO-3CH SKU and
    its siblings): a USB CDC serial device answering plain-text commands
    ("relay on 0", "relay read 0", "gpio read 1", "ver"). The serial path
    is configuration (prefer /dev/serial/by-id/...), never assumed.
    `port_factory` exists for tests; production opens pyserial lazily."""

    def __init__(self, path: str, *, timeout: float = 1.0, port_factory=None):
        if not path:
            raise AdapterError("relay serial path is not configured")
        self.path = path
        self.timeout = timeout
        self._port_factory = port_factory
        self._port = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._port is not None:
            return
        if self._port_factory is not None:
            self._port = self._port_factory(self.path)
            return
        try:
            import serial  # pyserial, imported lazily: the VMS runs without it
        except ImportError as error:
            raise AdapterError("pyserial is not installed") from error
        try:
            self._port = serial.Serial(self.path, 19200, timeout=self.timeout)
        except Exception as error:
            raise AdapterError(f"relay module not reachable at {self.path}: {error}") from error

    def close(self) -> None:
        with self._lock:
            if self._port is not None:
                try:
                    self._port.close()
                finally:
                    self._port = None

    def _command(self, text: str) -> str:
        with self._lock:
            try:
                self.open()
                self._port.reset_input_buffer()
                self._port.write((text + "\r").encode("ascii"))
                raw = self._port.read_until(b">", 256).decode("ascii", "replace")
            except AdapterError:
                raise
            except Exception as error:
                self._port = None  # force a clean reopen next time
                raise AdapterError(f"relay module I/O failed: {error}") from error
        if not raw.endswith(">"):
            self._port = None
            raise AdapterError("relay module did not answer in time")
        # The module echoes the command, then the answer, then its ">" prompt.
        lines = [line.strip() for line in raw[:-1].replace("\n", "\r").split("\r") if line.strip()]
        return lines[-1] if len(lines) > 1 else ""

    def set_output(self, channel: int, energized: bool) -> None:
        self._command(f"relay {'on' if energized else 'off'} {int(channel)}")

    def read_output(self, channel: int) -> bool:
        answer = self._command(f"relay read {int(channel)}").lower()
        if answer not in ("on", "off"):
            raise AdapterError(f"unexpected relay read answer {answer!r}")
        return answer == "on"

    def read_input(self, index: int) -> bool:
        answer = self._command(f"gpio read {int(index)}")
        if answer not in ("0", "1"):
            raise AdapterError(f"unexpected gpio read answer {answer!r}")
        return answer == "1"

    def health(self) -> dict:
        version = self._command("ver")
        return {"online": bool(version), "controller_online": bool(version), "firmware": version, "path": self.path}


def discover_serial_devices(pattern_hints: tuple[str, ...] = (), by_id_glob: str = "/dev/serial/by-id/*") -> list[dict]:
    """Stable serial device paths on a Linux appliance (/dev/serial/by-id
    survives re-plugging; /dev/ttyUSB0/ttyACM0 numbering does not).
    Read-only listing; hints only rank results, nothing is assumed."""
    found = []
    for path in sorted(glob.glob(by_id_glob)):
        name = os.path.basename(path)
        try:
            target = os.path.realpath(path)
        except OSError:
            target = None
        found.append({"path": path, "name": name, "device": target,
                      "hinted": any(hint.lower() in name.lower() for hint in pattern_hints)})
    return sorted(found, key=lambda item: (not item["hinted"], item["name"]))


# ------------------------------------------------------------------ relay door

LOCK_TYPES = ("strike", "maglock")


class RelayDoorAdapter(DoorAdapter):
    """A commercial door released through one dry-contact relay channel.

    The relay is only a switch in the access-control power supply's
    circuit; lock power never passes through the appliance. Idle (relay
    de-energized) is always "locked"; unlock() energizes the relay and the
    service's relock timer de-energizes it after the configured pulse. If
    the process dies mid-pulse, the service's restart recovery forces the
    output off before anything else runs.

      strike  -- fail-secure strike, typically on the relay's NO contact:
                 energizing releases it.
      maglock -- maglock power runs through the relay's NC contact:
                 energizing cuts power and releases it. A maglock MUST also
                 be released by the fire-alarm interface and a REX device
                 wired directly at the access-control power supply; that
                 life-safety release never depends on this software, the
                 network, Z-Wave or the VMS, and door configuration refuses
                 a maglock until the installer confirms it.

    Door-position (DPS) and request-to-exit (REX) inputs are optional;
    without a DPS the door state is reported from the relay alone and
    forced/held-open detection is unavailable (health says so).
    """
    kind = "relay"

    def __init__(self, driver: RelayDriver, *, channel: int, lock_type: str, dps_input: int | None = None,
                 dps_closed_value: bool = True, rex_input: int | None = None, rex_active_value: bool = True):
        if lock_type not in LOCK_TYPES:
            raise AdapterError(f"lock_type must be one of {LOCK_TYPES}")
        self.driver = driver
        self.channel = int(channel)
        self.lock_type = lock_type
        self.dps_input = dps_input
        self.dps_closed_value = dps_closed_value
        self.rex_input = rex_input
        self.rex_active_value = rex_active_value

    def health(self) -> dict:
        info = dict(self.driver.health())
        info.update(lock_type=self.lock_type, has_door_position=self.dps_input is not None,
                    has_rex=self.rex_input is not None, adapter="relay")
        return info

    def get_state(self) -> str:
        return UNLOCKED if self.driver.read_output(self.channel) else LOCKED

    def unlock(self, seconds: float) -> None:
        self.driver.set_output(self.channel, True)

    def lock(self) -> None:
        self.driver.set_output(self.channel, False)

    def read_inputs(self) -> dict:
        door_open = None
        rex = None
        if self.dps_input is not None:
            door_open = self.driver.read_input(self.dps_input) != self.dps_closed_value
        if self.rex_input is not None:
            rex = self.driver.read_input(self.rex_input) == self.rex_active_value
        return {"door_open": door_open, "rex": rex}


class MockRelayDriver(RelayDriver):
    """In-memory relay/IO module for tests and the simulator."""
    def __init__(self, channels: int = 3, inputs: int = 4):
        self.outputs = [False] * channels
        self.inputs = [False] * inputs
        self.online = True
        self.history: list[tuple[int, bool]] = []

    def _check(self):
        if not self.online:
            raise AdapterError("relay module offline")

    def set_output(self, channel, energized):
        self._check()
        self.outputs[channel] = energized
        self.history.append((channel, energized))

    def read_output(self, channel):
        self._check()
        return self.outputs[channel]

    def read_input(self, index):
        self._check()
        return self.inputs[index]

    def health(self):
        return {"online": self.online, "controller_online": self.online, "firmware": "mock"}
