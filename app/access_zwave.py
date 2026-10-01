"""Residential Z-Wave locks for the AnyAiCam access-control service (2026-09-25).

Architecture:  AnyAiCam VMS  --ws-->  zwave-js-server  --serial-->  Aeotec
Z-Stick 7 (or any Z-Wave JS supported 700/800 controller)  --radio-->
Z-Wave deadbolt (Kwikset SmartCode 914/916 Z-Wave, Yale Assure/Conexis
Z-Wave, Schlage BE469/Connect Z-Wave, ...). Wi-Fi/Bluetooth-only locks
(e.g. Kwikset SmartCode 270) are not Z-Wave and cannot be used.

Z-Wave JS (the maintained open-source Z-Wave stack) owns the serial
controller, security keys, network and device interviews; it runs as its
own container with the stick's /dev/serial/by-id/... path mapped in
(deploy/zwave/). This module only speaks its documented WebSocket API, so
the serial device path is configuration, never assumed (see
discover_zsticks()).

Security: a lock is only accepted when it is included securely (S2 Access
Control, or S0 for older locks that predate S2). An insecurely included
lock is flagged and never commanded. S2 inclusion asks for the 5-digit
PIN printed on the lock/its manual; the PIN is passed straight to Z-Wave
JS and never stored.

Lock state comes from the Door Lock command class (98): currentMode 255 =
secured, 0/1/16/17/32/33 = unsecured, 254 = unknown. A "lock jammed"
notification (Access Control, value 11) marks the door jammed. A node Z-Wave
JS reports "dead", a disconnected server, or an identity that no longer
matches what was enrolled (a different device now answering at that node
id) all make the door offline -- and the service never unlocks an offline
door.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import time

from access_adapters import JAMMED, LOCKED, UNKNOWN, UNLOCKED, AdapterError, DoorAdapter, discover_serial_devices

ZWAVE_JS_URL = os.environ.get("ANYAICAM_ZWAVE_JS_URL", "ws://127.0.0.1:3000").strip()
ZWAVE_SERIAL_PATH = os.environ.get("ANYAICAM_ZWAVE_SERIAL_PATH", "").strip()  # handed to the zwave-js-server container
SCHEMA_VERSION = 35
DOOR_LOCK_CC = 98
NOTIFICATION_CC = 113
TARGET_SECURED, TARGET_UNSECURED = 255, 0
UNSECURED_MODES = {0, 1, 16, 17, 32, 33}
LOCK_JAMMED_NOTIFICATION = 11
SET_VALUE_ACCEPTED = {1, 254, 255}  # Working, SuccessUnsupervised, Success
# Z-Wave JS SecurityClass: S2_Unauthenticated=0, S2_Authenticated=1, S2_AccessControl=2, S0_Legacy=7
S2_ACCESS_CONTROL, S0_LEGACY = 2, 7
ACCEPTABLE_LOCK_SECURITY = {S2_ACCESS_CONTROL, S0_LEGACY}
# Z-Stick/controller name hints for ranking /dev/serial/by-id entries (never required).
ZSTICK_HINTS = ("Z-Stick", "ZWA010", "Aeotec", "0658_0200", "Zooz", "Z-Wave", "zwave", "Silicon_Labs")


def discover_zsticks(by_id_glob: str = "/dev/serial/by-id/*") -> dict:
    devices = discover_serial_devices(ZSTICK_HINTS, by_id_glob=by_id_glob)
    configured = ZWAVE_SERIAL_PATH or None
    return {"configured_path": configured,
            "configured_path_present": bool(configured and os.path.exists(configured)),
            "candidates": devices}


# ------------------------------------------------------------------ transport

class WebSocketTransport:
    """Production transport (websockets' sync client, imported lazily)."""
    def __init__(self, url: str, open_timeout: float):
        try:
            from websockets.sync.client import connect
        except ImportError as error:
            raise AdapterError("the websockets package is not installed") from error
        try:
            self._ws = connect(url, open_timeout=open_timeout, max_size=None)
        except Exception as error:
            raise AdapterError(f"Z-Wave JS server not reachable at {url}: {error}") from error

    def send(self, text: str) -> None:
        self._ws.send(text)

    def recv(self, timeout: float) -> str | None:
        try:
            return self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class ZWaveJsClient:
    """One connection to zwave-js-server: request/response by messageId,
    a reader thread for events, and a cache of controller/node state."""

    def __init__(self, url: str = ZWAVE_JS_URL, *, transport_factory=None, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self._transport_factory = transport_factory or (lambda: WebSocketTransport(url, timeout))
        self._transport = None
        self._ids = itertools.count(1)
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader = None
        self.connected = False
        self.last_error: str | None = None
        self.server: dict = {}
        self.controller: dict = {}
        self.nodes: dict[int, dict] = {}
        self.jammed: set[int] = set()
        self.listeners: list = []

    # -- connection
    def connect(self) -> None:
        with self._lock:
            if self.connected:
                return
            self._transport = self._transport_factory()
            first = self._transport.recv(self.timeout)
            if not first:
                self._transport.close()
                raise AdapterError("Z-Wave JS server sent no version greeting")
            self.server = json.loads(first)
            self.connected = True
            self._reader = threading.Thread(target=self._read_loop, name="zwave-js-reader", daemon=True)
            self._reader.start()
        schema = min(SCHEMA_VERSION, int(self.server.get("maxSchemaVersion") or SCHEMA_VERSION))
        self.request("set_api_schema", schemaVersion=schema)
        state = self.request("start_listening").get("state") or {}
        self.controller = state.get("controller") or {}
        self.nodes = {int(node["nodeId"]): node for node in state.get("nodes") or [] if "nodeId" in node}

    def ensure_connected(self) -> None:
        if not self.connected:
            self.connect()

    def close(self) -> None:
        with self._lock:
            self.connected = False
            if self._transport is not None:
                self._transport.close()

    def _fail_all(self, reason: str) -> None:
        self.connected = False
        self.last_error = reason
        for waiter in list(self._pending.values()):
            waiter.put({"success": False, "errorCode": "disconnected", "message": reason})

    def _read_loop(self) -> None:
        while self.connected:
            try:
                raw = self._transport.recv(1.0)
            except Exception as error:
                self._fail_all(f"connection lost: {error}")
                return
            if raw is None:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("type") == "result":
                waiter = self._pending.pop(str(message.get("messageId")), None)
                if waiter:
                    waiter.put(message)
            elif message.get("type") == "event":
                self._on_event(message.get("event") or {})

    def request(self, command: str, **payload) -> dict:
        if not self.connected:
            raise AdapterError("Z-Wave JS server is not connected")
        message_id = str(next(self._ids))
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._pending[message_id] = waiter
        try:
            self._transport.send(json.dumps({"messageId": message_id, "command": command, **payload}))
        except Exception as error:
            self._pending.pop(message_id, None)
            self._fail_all(f"send failed: {error}")
            raise AdapterError(f"Z-Wave JS send failed: {error}") from error
        try:
            reply = waiter.get(timeout=self.timeout)
        except queue.Empty:
            self._pending.pop(message_id, None)
            raise AdapterError(f"Z-Wave JS did not answer {command} in {self.timeout:.0f}s")
        if not reply.get("success"):
            raise AdapterError(f"Z-Wave JS {command} failed: {reply.get('errorCode') or reply.get('message')}")
        return reply.get("result") or {}

    # -- events
    def _on_event(self, event: dict) -> None:
        source, name = event.get("source"), event.get("event")
        node_id = event.get("nodeId")
        if source == "node" and node_id is not None:
            node = self.nodes.setdefault(int(node_id), {"nodeId": int(node_id)})
            if name in ("dead", "alive", "sleep", "wake up"):
                node["status"] = {"dead": 3, "alive": 4, "sleep": 1, "wake up": 2}[name]
            elif name == "value updated":
                args = event.get("args") or {}
                if args.get("commandClass") == NOTIFICATION_CC and args.get("newValue") == LOCK_JAMMED_NOTIFICATION:
                    self.jammed.add(int(node_id))
                if args.get("commandClass") == DOOR_LOCK_CC and args.get("property") == "currentMode":
                    node["currentMode"] = args.get("newValue")
                    self.jammed.discard(int(node_id))
            elif name == "interview completed" and isinstance(event.get("node"), dict):
                node.update(event["node"])
        elif source == "controller" and name == "node added" and isinstance(event.get("node"), dict):
            self.nodes[int(event["node"]["nodeId"])] = event["node"]
        elif source == "controller" and name == "node removed" and isinstance(event.get("node"), dict):
            self.nodes.pop(int(event["node"].get("nodeId", -1)), None)
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                pass

    # -- helpers
    def home_id(self):
        return self.controller.get("homeId") or self.server.get("homeId")

    def health(self) -> dict:
        return {"controller_online": self.connected, "server_url": self.url, "home_id": self.home_id(),
                "driver_version": self.server.get("driverVersion"), "last_error": self.last_error}


NODE_STATUS = {0: "unknown", 1: "asleep", 2: "awake", 3: "dead", 4: "alive"}


def node_identity(node: dict) -> dict:
    return {"manufacturer_id": node.get("manufacturerId"), "product_type": node.get("productType"),
            "product_id": node.get("productId")}


def node_summary(node: dict) -> dict:
    config = node.get("deviceConfig") or {}
    classes = {cc.get("id") for cc in node.get("commandClasses") or [] if isinstance(cc, dict)}
    return {
        "node_id": node.get("nodeId"),
        "label": node.get("label") or config.get("label"),
        "manufacturer": config.get("manufacturer"),
        "description": config.get("description"),
        "status": NODE_STATUS.get(node.get("status"), "unknown"),
        "is_secure": node.get("isSecure"),
        "security_class": node.get("highestSecurityClass"),
        "is_lock": DOOR_LOCK_CC in classes or node.get("deviceClass", {}).get("generic", {}).get("label") == "Entry Control",
        **node_identity(node),
    }


# ------------------------------------------------------------------ inclusion / exclusion

class ZWaveEnrollment:
    """Secure inclusion and exclusion, driven by the owner from the UI.

    idle -> including -> (awaiting_grant) -> awaiting_pin -> interviewing
         -> included | insecure (rejected) | failed | stopped
    idle -> excluding -> excluded | stopped
    """

    def __init__(self, client: ZWaveJsClient):
        self.client = client
        self.state = "idle"
        self.detail: dict = {}
        client.listeners.append(self._on_event)

    def start_inclusion(self) -> dict:
        self.client.ensure_connected()
        self.detail = {}
        self.state = "including"
        # Strategy 0 (Default): S2 when the device supports it, S0 for older
        # locks. Never "Insecure" (2): a lock must be secure.
        self.client.request("controller.begin_inclusion", options={"strategy": 0})
        return self.status()

    def submit_pin(self, pin: str) -> dict:
        pin = str(pin or "").strip()
        if self.state != "awaiting_pin":
            raise AdapterError("no S2 PIN is being requested right now")
        if not (len(pin) == 5 and pin.isdigit()):
            raise AdapterError("the S2 PIN is the 5-digit number printed on the lock or its manual")
        self.client.request("controller.validate_dsk_and_enter_pin", pin=pin)
        self.state = "interviewing"
        return self.status()

    def stop(self) -> dict:
        if self.state in ("including", "awaiting_grant", "awaiting_pin"):
            self.client.request("controller.stop_inclusion")
        elif self.state == "excluding":
            self.client.request("controller.stop_exclusion")
        self.state = "stopped"
        return self.status()

    def start_exclusion(self) -> dict:
        self.client.ensure_connected()
        self.detail = {}
        self.state = "excluding"
        self.client.request("controller.begin_exclusion")
        return self.status()

    def status(self) -> dict:
        return {"state": self.state, **self.detail}

    def _on_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "grant security classes" and self.state == "including":
            requested = (event.get("requested") or {}).get("securityClasses") or []
            self.state = "awaiting_grant"
            try:
                self.client.request("controller.grant_security_classes",
                                    inclusionGrant={"securityClasses": requested, "clientSideAuth": False})
            except AdapterError as error:
                self.state, self.detail["error"] = "failed", str(error)
        elif name == "validate dsk and enter pin":
            self.state = "awaiting_pin"
            self.detail["dsk_hint"] = event.get("dsk")  # the printed DSK's visible part, for the owner to compare
        elif name == "node added" and self.state in ("including", "awaiting_grant", "awaiting_pin", "interviewing"):
            node = event.get("node") or {}
            self.detail["node_id"] = node.get("nodeId")
            self.state = "interviewing"
        elif name == "interview completed" and self.detail.get("node_id") == event.get("nodeId"):
            summary = node_summary(event.get("node") or self.client.nodes.get(int(event["nodeId"]), {}))
            self.detail["node"] = summary
            secure = summary["is_secure"] and summary["security_class"] in ACCEPTABLE_LOCK_SECURITY
            self.state = "included" if secure else "insecure"
            if not secure:
                self.detail["error"] = ("This lock joined without S2 Access Control or S0 security. It will not be used; "
                                        "exclude it and include it again securely.")
        elif name == "inclusion failed":
            self.state, self.detail["error"] = "failed", "inclusion failed"
        elif name == "node removed" and self.state == "excluding":
            self.detail["node_id"] = (event.get("node") or {}).get("nodeId")
            self.state = "excluded"


# ------------------------------------------------------------------ lock adapter

class ZWaveLockAdapter(DoorAdapter):
    kind = "zwave"

    def __init__(self, client: ZWaveJsClient, *, node_id: int, expected_identity: dict | None = None):
        self.client = client
        self.node_id = int(node_id)
        self.expected_identity = {k: v for k, v in (expected_identity or {}).items() if v is not None}

    def _node(self) -> dict:
        self.client.ensure_connected()
        node = self.client.nodes.get(self.node_id)
        if node is None:
            raise AdapterError(f"Z-Wave node {self.node_id} is not in the network")
        return node

    def _check_identity(self, node: dict) -> None:
        actual = node_identity(node)
        for key, value in self.expected_identity.items():
            if actual.get(key) != value:
                raise AdapterError(f"Z-Wave node {self.node_id} is not the enrolled lock ({key} changed)")

    def health(self) -> dict:
        info = {"adapter": "zwave", "node_id": self.node_id}
        try:
            node = self._node()  # connects first, so the controller status below is current
            info.update(self.client.health())
            self._check_identity(node)
            summary = node_summary(node)
            secure = bool(summary["is_secure"]) and summary["security_class"] in ACCEPTABLE_LOCK_SECURITY
            info.update(device_status=summary["status"], secure=secure, label=summary["label"])
            info["online"] = info["controller_online"] and summary["status"] != "dead" and secure
        except AdapterError as error:
            info.update(self.client.health())
            info.update(online=False, error=str(error))
        return info

    def get_state(self) -> str:
        node = self._node()
        self._check_identity(node)
        if self.node_id in self.client.jammed:
            return JAMMED
        result = self.client.request("node.poll_value", nodeId=self.node_id,
                                     valueId={"commandClass": DOOR_LOCK_CC, "endpoint": 0, "property": "currentMode"})
        mode = result.get("value", node.get("currentMode"))
        if mode == TARGET_SECURED:
            return LOCKED
        if mode in UNSECURED_MODES:
            return UNLOCKED
        return UNKNOWN

    def _set_mode(self, mode: int) -> None:
        node = self._node()
        self._check_identity(node)
        result = self.client.request("node.set_value", nodeId=self.node_id,
                                     valueId={"commandClass": DOOR_LOCK_CC, "endpoint": 0, "property": "targetMode"},
                                     value=mode)
        # Newer schemas answer {"result": {"status": SetValueStatus}}, older
        # ones {"success": bool}. SetValueStatus: 0 NoDeviceSupport,
        # 1 Working, 2 Fail, 3 EndpointNotFound, 4 NotImplemented,
        # 5 InvalidValue, 254 SuccessUnsupervised, 255 Success.
        status = (result.get("result") or {}).get("status") if isinstance(result.get("result"), dict) else None
        if status is not None and status not in SET_VALUE_ACCEPTED:
            raise AdapterError(f"lock refused the command (SetValueStatus {status})")
        if status is None and result.get("success") is False:
            raise AdapterError("lock refused the command")

    def unlock(self, seconds: float) -> None:
        self._set_mode(TARGET_UNSECURED)

    def lock(self) -> None:
        self._set_mode(TARGET_SECURED)
