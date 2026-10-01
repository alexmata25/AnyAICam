"""Access-control hardware integration (2026-09-25): service, fail-secure
rules, adapters (simulator, relay/Numato, Z-Wave JS), face-recognition hook,
API authorization and audit. No hardware: the Z-Wave JS server, the relay
module and the serial port are fakes that speak the real protocols."""
import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import access_adapters as aa
import access_control as ac
import access_zwave as zw
from database_backend import override_target
from partner_db import connection, initialize_database


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def schedule(self, key, delay, fn):
        self.jobs[key] = (delay, fn)

    def cancel(self, key):
        self.jobs.pop(key, None)

    def fire(self, key):
        delay, fn = self.jobs.pop(key)
        return fn()


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def db(tmp_path):
    with override_target(sqlite_path=tmp_path / "access.db"):
        initialize_database()
        conn = sqlite3.connect(tmp_path / "access.db")
        conn.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real','2026-01-01')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,camera_number,status,created_at) VALUES('cam-front','cust-1','site-1','Front Door',1,'configured','2026-01-01')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,camera_number,status,created_at) VALUES('cam-back','cust-1','site-1','Back Door',2,'configured','2026-01-01')")
        conn.commit()
        conn.close()
        yield tmp_path / "access.db"


def make_service(**kwargs):
    mocks = kwargs.pop("mock_adapters", {})
    factory = kwargs.pop("factory", None) or ac.AdapterFactory(mock_adapters=mocks, zwave_client=kwargs.pop("zwave_client", None),
                                                                  relay_driver_factory=kwargs.pop("relay_driver_factory", None))
    return ac.AccessControlService(store=ac.DoorStore(connection), adapters=factory, clock=kwargs.pop("clock", Clock()),
                                   scheduler=kwargs.pop("scheduler", FakeScheduler()), command_timeout=kwargs.pop("command_timeout", 0.5),
                                   retry_pause=0.0, **kwargs)


def mock_door(service, door_id="door-front", camera_id="cam-front", **overrides):
    door = ac.Door(id=door_id, customer_id="cust-1", name=overrides.pop("name", "Front Door"), kind="mock", config={},
                   camera_id=camera_id, unlock_seconds=overrides.pop("unlock_seconds", 5), dry_run=overrides.pop("dry_run", False), **overrides)
    service.configure_door(door)
    return door


def commands(door_id):
    with connection() as conn:
        return [dict(r) for r in conn.execute("SELECT command,trigger_type,result,actor,detail FROM access_door_commands WHERE door_id=? ORDER BY rowid", (door_id,))]


def events(door_id):
    with connection() as conn:
        return [r["event_type"] for r in conn.execute("SELECT event_type FROM access_door_events WHERE door_id=? ORDER BY rowid", (door_id,))]


# ----------------------------------------------------------------- core service / simulator

def test_successful_unlock_schedules_relock_and_is_logged(db):
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    outcome = service.unlock("door-front", reason="test", actor="owner@example.test")
    assert outcome.ok and outcome.result == "unlocked" and outcome.state == "unlocked" and outcome.relock_at
    assert adapter.calls == [("unlock", 5)] and adapter.state == "unlocked"
    assert "relock:door-front" in service.scheduler.jobs and service.scheduler.jobs["relock:door-front"][0] == 5
    assert commands("door-front") == [{"command": "unlock", "trigger_type": "manual", "result": "unlocked", "actor": "owner@example.test", "detail": None}]
    assert events("door-front") == ["unlocked"]


def test_timed_relock_locks_and_clears_the_pending_state(db):
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service, unlock_seconds=8)
    service.unlock("door-front")
    service.scheduler.fire("relock:door-front")
    assert adapter.state == "locked" and adapter.calls[-1] == ("lock", None)
    assert service.store.state("door-front") ["relock_due_at"] is None
    assert commands("door-front")[-1]["trigger_type"] == "relock" and commands("door-front")[-1]["result"] == "locked"


def test_controller_missing_means_no_unlock(db):
    adapter = aa.MockDoorAdapter(controller_online=False)
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    outcome = service.unlock("door-front")
    assert (outcome.ok, outcome.result) == (False, "offline") and adapter.calls == []
    assert events("door-front") == ["offline"]


def test_device_offline_means_no_unlock(db):
    adapter = aa.MockDoorAdapter(device_online=False)
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    assert service.unlock("door-front").result == "offline" and adapter.calls == []


def test_duplicate_unlock_is_refused_not_queued(db):
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    assert service.unlock("door-front").ok
    second = service.unlock("door-front")
    assert (second.ok, second.result) == (False, "duplicate") and adapter.calls == [("unlock", 5)]


def test_concurrent_commands_for_one_door_never_overlap(db):
    adapter = aa.MockDoorAdapter(hang_seconds=0.3)
    service = make_service(mock_adapters={"door-front": adapter}, command_timeout=2)
    mock_door(service)
    results = []
    import contextvars
    def attempt():
        results.append(service.unlock("door-front").result)
    # Threads don't inherit the test's override_target DB context; copy it in.
    threads = [threading.Thread(target=contextvars.copy_context().run, args=(attempt,)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["duplicate", "duplicate", "unlocked"]
    assert [c for c in adapter.calls if c[0] == "unlock"] == [("unlock", 5)]


def test_command_timeout_fails_secure(db):
    adapter = aa.MockDoorAdapter(hang_seconds=1.0)
    service = make_service(mock_adapters={"door-front": adapter}, command_timeout=0.2)
    mock_door(service)
    outcome = service.unlock("door-front")
    assert (outcome.ok, outcome.result) == (False, "timeout")
    assert "relock:door-front" not in service.scheduler.jobs
    time.sleep(1.2)  # let the hung calls finish
    assert ("lock", None) in adapter.calls  # a securing attempt followed the timeout


def test_refused_or_failed_command_is_a_failure_and_secures(db):
    adapter = aa.MockDoorAdapter(refuse=True)
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    outcome = service.unlock("door-front")
    assert (outcome.ok, outcome.result) == (False, "failed") and "refused" in outcome.detail


def test_automatic_unlock_refuses_unknown_or_jammed_state_manual_is_the_humans_call(db):
    for state in ("unknown", "jammed"):
        adapter = aa.MockDoorAdapter(state=state)
        service = make_service(mock_adapters={"door-front": adapter})
        mock_door(service)
        for trigger in ("automatic", "aaco", "aac_voice_call"):
            auto = service.unlock("door-front", trigger=trigger)
            assert (auto.ok, auto.result) == (False, "uncertain_state") and adapter.calls == [], trigger
        service.store.delete("door-front")


def test_dry_run_door_never_touches_hardware(db):
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service, dry_run=True)
    outcome = service.unlock("door-front")
    assert (outcome.ok, outcome.result) == (False, "dry_run") and adapter.calls == []


def test_unknown_or_disabled_door_is_denied_and_logged(db):
    service = make_service()
    assert service.unlock("nope").result == "denied"
    assert commands("nope")[0]["result"] == "denied"
    mock_door(service, enabled=False)
    assert service.unlock("door-front").result == "denied"


def test_lock_retries_then_raises_relock_failed(db):
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    service.unlock("door-front")
    adapter.refuse = True
    outcome = service.scheduler.fire("relock:door-front")
    assert (outcome.ok, outcome.result) == (False, "failed")
    assert len([c for c in adapter.calls if c[0] == "lock"]) == service.lock_retries
    assert "relock_failed" in events("door-front") and service.store.state("door-front")["state"] == "unknown"


def test_restart_recovery_relocks_overdue_and_reschedules_pending(db):
    clock = Clock()
    a1, a2 = aa.MockDoorAdapter(), aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": a1, "door-back": a2}, clock=clock)
    mock_door(service)
    mock_door(service, door_id="door-back", camera_id="cam-back", name="Back Door", unlock_seconds=60)
    service.unlock("door-front")
    service.unlock("door-back")
    clock.t += 10  # the process died; front's relock (5 s) is overdue, back's (60 s) is not
    fresh = make_service(mock_adapters={"door-front": a1, "door-back": a2}, clock=clock)
    outcomes = fresh.recover()
    assert [o.door_id for o in outcomes] == ["door-front"] and a1.state == "locked"
    assert a2.state == "unlocked" and round(fresh.scheduler.jobs["relock:door-back"][0]) == 50


def test_multiple_doors_are_independent(db):
    a1, a2 = aa.MockDoorAdapter(), aa.MockDoorAdapter(device_online=False)
    service = make_service(mock_adapters={"door-front": a1, "door-back": a2})
    mock_door(service)
    mock_door(service, door_id="door-back", camera_id="cam-back", name="Back Door")
    assert service.unlock("door-front").ok
    assert service.unlock("door-back").result == "offline"
    assert service.get_status("door-front")["state"] == "unlocked" and service.get_status("door-back")["online"] is False
    assert service.door_for_camera("cam-back").id == "door-back"


# ----------------------------------------------------------------- relay doors

def relay_service(driver, **door_config):
    service = make_service(relay_driver_factory=lambda cfg: driver)
    config = {"driver": "mock", "channel": 1, **door_config}
    kind = "relay_maglock" if config.pop("maglock", False) else "relay_strike"
    service.configure_door(ac.Door(id="door-shop", customer_id="cust-1", name="Shop", kind=kind, config=config,
                                   camera_id="cam-back", unlock_seconds=3))
    return service


def test_relay_strike_pulse_energizes_then_relock_de_energizes(db):
    driver = aa.MockRelayDriver()
    service = relay_service(driver)
    assert service.unlock("door-shop").ok and driver.outputs[1] is True
    service.scheduler.fire("relock:door-shop")
    assert driver.outputs[1] is False and driver.history == [(1, True), (1, False)]


def test_relay_module_offline_means_no_unlock(db):
    driver = aa.MockRelayDriver()
    driver.online = False
    service = relay_service(driver)
    assert service.unlock("door-shop").result == "offline" and driver.history == []


def test_relay_doors_are_forced_off_on_restart(db):
    driver = aa.MockRelayDriver()
    driver.outputs[1] = True  # left energized by a crash
    service = relay_service(driver)
    service.recover()
    assert driver.outputs[1] is False


def test_maglock_requires_confirmed_independent_life_safety_release(db):
    service = make_service(relay_driver_factory=lambda cfg: aa.MockRelayDriver())
    door = ac.Door(id="door-mag", customer_id="cust-1", name="Lobby", kind="relay_maglock",
                   config={"driver": "mock", "channel": 0})
    with pytest.raises(ValueError, match="fire-alarm"):
        service.configure_door(door)
    door.config["life_safety_confirmed"] = True
    service.configure_door(door)
    assert service.health("door-mag")["lock_type"] == "maglock"


def test_forced_door_held_open_and_request_to_exit(db):
    clock = Clock()
    driver = aa.MockRelayDriver()
    service = relay_service(driver, dps_input=0, rex_input=1, held_open_seconds=20)
    service.clock = clock
    driver.inputs[0] = True  # DPS: closed
    assert service.poll_inputs("door-shop") == []
    driver.inputs[0] = False  # opened with no unlock/REX: forced
    assert service.poll_inputs("door-shop") == ["door_forced"]
    clock.t += 25
    assert service.poll_inputs("door-shop") == ["door_held_open"]
    clock.t += 5
    assert service.poll_inputs("door-shop") == []  # held-open raised once per opening
    driver.inputs[0] = True
    assert service.poll_inputs("door-shop") == ["door_closed"]
    driver.inputs[1] = True  # REX pressed, then door opens: not forced
    assert service.poll_inputs("door-shop") == ["request_to_exit"]
    driver.inputs[1] = False
    driver.inputs[0] = False
    assert service.poll_inputs("door-shop") == ["door_opened"]
    driver.inputs[0] = True
    service.poll_inputs("door-shop")
    clock.t += 60
    assert service.unlock("door-shop").ok  # an authorized unlock grants the next opening
    driver.inputs[0] = False
    assert service.poll_inputs("door-shop") == ["door_opened"]
    driver.online = False
    assert service.poll_inputs("door-shop") == ["controller_offline"]
    assert service.poll_inputs("door-shop") == []  # raised once, not every poll
    driver.online = True
    assert "controller_online" in service.poll_inputs("door-shop")


class FakeSerial:
    """Numato USB relay firmware: echoes the command, answers, then '>'."""
    def __init__(self):
        self.relays = {0: False, 1: False, 2: False}
        self.gpio = {0: True, 1: False}
        self.written = []
        self._out = b""

    def reset_input_buffer(self):
        self._out = b""

    def write(self, data):
        text = data.decode().strip()
        self.written.append(text)
        parts = text.split()
        answer = ""
        if parts[:1] == ["ver"]:
            answer = "00000008"
        elif parts[:2] == ["relay", "on"]:
            self.relays[int(parts[2])] = True
        elif parts[:2] == ["relay", "off"]:
            self.relays[int(parts[2])] = False
        elif parts[:2] == ["relay", "read"]:
            answer = "on" if self.relays[int(parts[2])] else "off"
        elif parts[:2] == ["gpio", "read"]:
            answer = "1" if self.gpio[int(parts[2])] else "0"
        self._out = (text + "\n\r" + (answer + "\n\r" if answer else "") + ">").encode()

    def read_until(self, terminator, size):
        out, self._out = self._out, b""
        return out

    def close(self):
        pass


def test_numato_driver_speaks_the_module_protocol():
    port = FakeSerial()
    driver = aa.NumatoUsbRelayDriver("/dev/serial/by-id/usb-Numato_Lab_3_Channel_USB_Relay_Module-if00", port_factory=lambda path: port)
    assert driver.health()["firmware"] == "00000008"
    driver.set_output(2, True)
    assert driver.read_output(2) is True and port.written[-2:] == ["relay on 2", "relay read 2"]
    assert driver.read_input(0) is True and driver.read_input(1) is False
    with pytest.raises(aa.AdapterError):
        aa.NumatoUsbRelayDriver("")


def test_serial_discovery_prefers_by_id_paths_and_ranks_hints(tmp_path):
    for name in ("usb-FTDI_Serial-if00", "usb-0658_0200-if00", "usb-Numato_Lab_Relay-if00"):
        (tmp_path / name).write_text("")
    found = zw.discover_zsticks(by_id_glob=str(tmp_path / "*"))
    assert found["candidates"][0]["name"] == "usb-0658_0200-if00" and found["configured_path"] in (None, zw.ZWAVE_SERIAL_PATH or None)
    relays = aa.discover_serial_devices(("Numato",), by_id_glob=str(tmp_path / "*"))
    assert relays[0]["name"] == "usb-Numato_Lab_Relay-if00"


# ----------------------------------------------------------------- Z-Wave JS

LOCK_NODE = {"nodeId": 5, "status": 4, "isSecure": True, "highestSecurityClass": 2, "manufacturerId": 144,
             "productType": 3, "productId": 1, "label": "SmartCode 916", "deviceConfig": {"manufacturer": "Kwikset"},
             "commandClasses": [{"id": 98}], "currentMode": 255}


class FakeZWaveJsServer:
    """Plays zwave-js-server's WebSocket protocol: version greeting,
    set_api_schema, start_listening, node.* and controller.* commands."""
    def __init__(self, nodes=None, mode=255, silent=None):
        self.inbox = queue.Queue()
        self.sent = []
        self.nodes = nodes if nodes is not None else [dict(LOCK_NODE)]
        self.mode = mode
        self.silent = set(silent or ())
        self.inbox.put(json.dumps({"type": "version", "driverVersion": "13.0.0", "serverVersion": "1.40.0",
                                   "homeId": 3735928559, "minSchemaVersion": 0, "maxSchemaVersion": 38}))

    def emit(self, event):
        self.inbox.put(json.dumps({"type": "event", "event": event}))

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        command = message["command"]
        if command in self.silent:
            return
        result = {}
        if command == "start_listening":
            result = {"state": {"controller": {"homeId": 3735928559}, "nodes": self.nodes}}
        elif command == "node.poll_value":
            result = {"value": self.mode}
        elif command == "node.set_value":
            self.mode = message["value"]
            result = {"result": {"status": 255}}
        self.inbox.put(json.dumps({"type": "result", "messageId": message["messageId"], "success": True, "result": result}))

    def recv(self, timeout):
        try:
            return self.inbox.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None

    def close(self):
        pass


def zwave_service(server, **identity):
    client = zw.ZWaveJsClient("ws://test", transport_factory=lambda: server, timeout=0.5)
    service = make_service(zwave_client=client)
    service.configure_door(ac.Door(id="door-home", customer_id="cust-1", name="Front Deadbolt", kind="zwave_lock",
                                   config={"node_id": 5, "identity": identity or {"manufacturer_id": 144, "product_type": 3, "product_id": 1}},
                                   camera_id="cam-front", unlock_seconds=10))
    return service, client


def test_zwave_lock_unlock_status_and_relock(db):
    server = FakeZWaveJsServer()
    service, client = zwave_service(server)
    status = service.get_status("door-home")
    assert (status["online"], status["state"]) == (True, "locked")
    assert service.unlock("door-home", trigger="automatic").ok
    set_values = [m for m in server.sent if m["command"] == "node.set_value"]
    assert set_values[-1]["value"] == 0 and set_values[-1]["valueId"] == {"commandClass": 98, "endpoint": 0, "property": "targetMode"}
    assert server.sent[0]["command"] == "set_api_schema" and server.sent[0]["schemaVersion"] == 35
    service.scheduler.fire("relock:door-home")
    assert [m for m in server.sent if m["command"] == "node.set_value"][-1]["value"] == 255
    client.close()


def test_zwave_dead_node_insecure_or_swapped_device_is_offline(db):
    for node_patch in ({"status": 3}, {"isSecure": False}, {"productId": 99}):
        server = FakeZWaveJsServer(nodes=[{**LOCK_NODE, **node_patch}])
        service, client = zwave_service(server)
        assert service.unlock("door-home").result == "offline", node_patch
        assert not [m for m in server.sent if m["command"] == "node.set_value"]
        client.close()
        service.store.delete("door-home")


def test_zwave_server_missing_means_offline(db):
    def unreachable():
        raise aa.AdapterError("Z-Wave JS server not reachable")
    client = zw.ZWaveJsClient("ws://test", transport_factory=unreachable, timeout=0.2)
    service = make_service(zwave_client=client)
    service.configure_door(ac.Door(id="door-home", customer_id="cust-1", name="Deadbolt", kind="zwave_lock", config={"node_id": 5}))
    outcome = service.unlock("door-home")
    assert outcome.result == "offline" and "not reachable" in outcome.detail


def test_zwave_jammed_notification_blocks_automatic_unlock(db):
    server = FakeZWaveJsServer()
    service, client = zwave_service(server)
    service.get_status("door-home")
    server.emit({"source": "node", "event": "value updated", "nodeId": 5, "args": {"commandClass": 113, "newValue": 11}})
    time.sleep(0.2)
    assert service.get_status("door-home")["state"] == "jammed"
    assert service.unlock("door-home", trigger="automatic").result == "uncertain_state"
    client.close()


def test_zwave_command_timeout_is_a_failure(db):
    server = FakeZWaveJsServer(silent={"node.set_value"})
    service, client = zwave_service(server)
    outcome = service.unlock("door-home")
    assert not outcome.ok and outcome.result in ("failed", "timeout")
    client.close()


def test_zwave_secure_inclusion_with_s2_pin_and_exclusion():
    server = FakeZWaveJsServer(nodes=[])
    client = zw.ZWaveJsClient("ws://test", transport_factory=lambda: server, timeout=0.5)
    enrollment = zw.ZWaveEnrollment(client)
    assert enrollment.start_inclusion()["state"] == "including"
    assert [m for m in server.sent if m["command"] == "controller.begin_inclusion"][0]["options"] == {"strategy": 0}
    server.emit({"source": "controller", "event": "grant security classes", "requested": {"securityClasses": [2], "clientSideAuth": False}})
    server.emit({"source": "controller", "event": "validate dsk and enter pin", "dsk": "-12345-67890"})
    deadline = time.time() + 2
    while enrollment.state != "awaiting_pin" and time.time() < deadline:
        time.sleep(0.02)
    assert enrollment.state == "awaiting_pin" and enrollment.status()["dsk_hint"] == "-12345-67890"
    with pytest.raises(aa.AdapterError):
        enrollment.submit_pin("12")
    enrollment.submit_pin("48151")
    assert [m for m in server.sent if m["command"] == "controller.validate_dsk_and_enter_pin"][0]["pin"] == "48151"
    assert [m for m in server.sent if m["command"] == "controller.grant_security_classes"][0]["inclusionGrant"]["securityClasses"] == [2]
    server.emit({"source": "controller", "event": "node added", "node": {"nodeId": 7}})
    server.emit({"source": "node", "event": "interview completed", "nodeId": 7, "node": {**LOCK_NODE, "nodeId": 7}})
    deadline = time.time() + 2
    while enrollment.state != "included" and time.time() < deadline:
        time.sleep(0.02)
    assert enrollment.state == "included" and enrollment.status()["node"]["is_lock"] is True
    assert enrollment.start_exclusion()["state"] == "excluding"
    server.emit({"source": "controller", "event": "node removed", "node": {"nodeId": 7}})
    deadline = time.time() + 2
    while enrollment.state != "excluded" and time.time() < deadline:
        time.sleep(0.02)
    assert enrollment.state == "excluded"
    client.close()


def test_an_insecurely_included_lock_is_rejected():
    server = FakeZWaveJsServer(nodes=[])
    client = zw.ZWaveJsClient("ws://test", transport_factory=lambda: server, timeout=0.5)
    enrollment = zw.ZWaveEnrollment(client)
    enrollment.start_inclusion()
    server.emit({"source": "controller", "event": "node added", "node": {"nodeId": 8}})
    server.emit({"source": "node", "event": "interview completed", "nodeId": 8, "node": {**LOCK_NODE, "nodeId": 8, "isSecure": False, "highestSecurityClass": None}})
    deadline = time.time() + 2
    while enrollment.state not in ("insecure", "included") and time.time() < deadline:
        time.sleep(0.02)
    assert enrollment.state == "insecure" and "will not be used" in enrollment.status()["error"]
    client.close()


def test_multiple_adapter_types_in_one_service(db):
    server = FakeZWaveJsServer()
    client = zw.ZWaveJsClient("ws://test", transport_factory=lambda: server, timeout=0.5)
    driver = aa.MockRelayDriver()
    sim = aa.MockDoorAdapter()
    service = make_service(zwave_client=client, relay_driver_factory=lambda cfg: driver, mock_adapters={"door-sim": sim})
    service.configure_door(ac.Door(id="door-home", customer_id="cust-1", name="Deadbolt", kind="zwave_lock", config={"node_id": 5}, camera_id="cam-front"))
    service.configure_door(ac.Door(id="door-shop", customer_id="cust-1", name="Shop", kind="relay_strike", config={"driver": "mock", "channel": 2}, camera_id="cam-back"))
    service.configure_door(ac.Door(id="door-sim", customer_id="cust-1", name="Sim", kind="mock", config={}))
    assert all(service.unlock(door_id).ok for door_id in ("door-home", "door-shop", "door-sim"))
    assert driver.outputs[2] is True and sim.state == "unlocked" and server.mode == 0
    client.close()


# ----------------------------------------------------------------- face recognition hook + legacy path

def _rule(conn, **overrides):
    conn.execute("INSERT OR IGNORE INTO facial_people(id,customer_id,display_name,status,created_at,updated_at) "
                 "VALUES('person-anna','cust-1','Anna','active','2026-01-01','2026-01-01')")
    row = {"id": "rule-1", "customer_id": "cust-1", "camera_id": "cam-front", "name": "Family", "trigger_type": "specific_person",
           "person_id": "person-anna", "min_confidence": 0.8, "relay_channel": 1, "pulse_ms": 4000, "cooldown_seconds": 0,
           "dry_run": 0, "enabled": 1, "created_at": "2026-01-01", "updated_at": "2026-01-01", **overrides}
    cols = ",".join(row)
    conn.execute(f"INSERT INTO facial_rules({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))


def _face(service, *, person_id="person-anna", confidence=0.93, now="12:00", match_state="known"):
    import door_access
    import facial_events
    import relay_control
    ac.set_service(service)
    base = relay_control.MockRelayProvider()
    camera = {"id": "cam-front", "name": "Front Door", "door_relay_channel": None, "door_relay_pulse_ms": None, "door_access_enabled": 1}
    with connection() as conn:
        outcomes = facial_events.evaluate_access_rules(
            conn, customer_id="cust-1", camera_id="cam-front", match_state=match_state, confidence=confidence,
            matched_person_id=person_id, matched_watchlist_id=None,
            relay_provider=door_access.CameraDoorProvider(camera, base, person_id=person_id),
            detection_event_id="det-1", current_time=now)
    return outcomes, base


@pytest.fixture()
def face_env(db):
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    yield service, adapter
    ac.set_service(None)


def test_face_authorized_person_unlocks_through_the_service(face_env):
    service, adapter = face_env
    with connection() as conn:
        _rule(conn)
    outcomes, base = _face(service)
    assert outcomes and outcomes[0]["activated"] is True and adapter.calls == [("unlock", 4)]
    assert base.calls == []  # never the legacy provider for a service door
    row = commands("door-front")[0]
    assert (row["trigger_type"], row["actor"], row["result"]) == ("automatic", "facial_recognition", "unlocked")


def test_face_unauthorized_low_confidence_and_outside_schedule_never_unlock(face_env):
    service, adapter = face_env
    with connection() as conn:
        _rule(conn, schedule_start="08:00", schedule_end="18:00")
    for kwargs in ({"person_id": "someone-else"}, {"confidence": 0.5}, {"now": "23:30"}):
        outcomes, _ = _face(service, **kwargs)
        assert not any(o["activated"] for o in outcomes), kwargs
    assert adapter.calls == []


def test_face_dry_run_rule_never_reaches_hardware(face_env):
    service, adapter = face_env
    with connection() as conn:
        _rule(conn, dry_run=1)
    outcomes, _ = _face(service)
    assert outcomes and not outcomes[0]["activated"] and adapter.calls == []


def test_face_with_uncertain_lock_state_is_refused(face_env):
    service, adapter = face_env
    adapter.state = "unknown"
    with connection() as conn:
        _rule(conn)
    outcomes, _ = _face(service)
    assert not outcomes[0]["activated"] and adapter.calls == []
    assert commands("door-front")[0]["result"] == "uncertain_state"


def test_a_door_not_configured_in_the_service_keeps_the_legacy_relay_path(db):
    import door_access
    import relay_control
    ac.set_service(make_service())
    try:
        relay_control.reset_provider()
        result = door_access.trigger_door({"id": "cam-back", "door_relay_channel": 2, "door_relay_pulse_ms": 1500},
                                          reason="manual_unlock:cam-back", actor="o@example.test", trigger_type="manual")
        assert result.activated is True and result.channel == 2
        assert relay_control.get_provider().calls[-1].channel == 2
    finally:
        ac.set_service(None)
        relay_control.reset_provider()


# ----------------------------------------------------------------- API: authorization + audit
# Same pattern as test_door_access.py: call the registered endpoints directly
# (no TestClient, so the app lifespan and its workers never start).

def _route(path, method):
    import main
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set()):
            return r.endpoint
    raise AssertionError(f"no route for {method} {path}")


def _request():
    from types import SimpleNamespace
    return SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"),
                           url=SimpleNamespace(scheme="https", netloc="portal.example.test"))


OWNER = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"}
VIEWER = {"role": "customer_viewer", "customer_id": "cust-1", "email": "viewer@example.test"}
OTHER = {"role": "customer_owner", "customer_id": "cust-2", "email": "other@example.test"}


@pytest.fixture()
def api(db, monkeypatch):
    import access_control_api
    adapter = aa.MockDoorAdapter()
    service = make_service(mock_adapters={"door-front": adapter})
    mock_door(service)
    ac.set_service(service)
    who = {}
    monkeypatch.setattr(access_control_api, "partner_identity", lambda request: who.get("current"))
    monkeypatch.setattr(access_control_api, "audit", lambda *a, **k: None)
    with connection() as conn:
        for uid, email, role in (("u-viewer", "viewer@example.test", "customer_viewer"), ("u-owner", "owner@example.test", "customer_owner")):
            conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) "
                         "VALUES(?,?,?,?,?,'x',1,'cust-1','2026-01-01','active')", (uid, "p1", email, email, role))
    yield who, adapter
    ac.set_service(None)


def _status(fn, *args):
    from fastapi import HTTPException
    try:
        return 200, fn(_request(), *args)
    except HTTPException as error:
        return error.status_code, error.detail


def _door_audit():
    with connection() as conn:
        return [tuple(r) for r in conn.execute("SELECT actor_email,authorization_result,relay_result,success FROM door_access_events ORDER BY rowid")]


def test_api_owner_unlock_and_viewer_needs_can_unlock(api):
    who, adapter = api
    unlock = _route("/api/customer/access/doors/{door_id}/unlock", "POST")
    lock = _route("/api/customer/access/doors/{door_id}/lock", "POST")
    who["current"] = VIEWER
    assert _status(unlock, "door-front", {})[0] == 403 and adapter.calls == []
    who["current"] = OTHER
    assert _status(unlock, "door-front", {})[0] == 404  # another customer's door does not exist for them
    who["current"] = None
    assert _status(unlock, "door-front", {})[0] == 403
    who["current"] = OWNER
    assert _status(unlock, "door-front", {"duration_seconds": -1})[0] == 400
    code, body = _status(unlock, "door-front", {"duration_seconds": 3})
    assert code == 200 and body["result"] == "unlocked" and adapter.calls == [("unlock", 3)]
    code, body = _status(unlock, "door-front", {})
    assert code == 409 and body["result"] == "duplicate"
    with connection() as conn:
        conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_unlock) VALUES('u-viewer','cam-front',1)")
    who["current"] = VIEWER
    assert _status(lock, "door-front")[0] == 200 and adapter.state == "locked"
    audit_rows = _door_audit()
    assert audit_rows[0] == ("viewer@example.test", "denied", "skipped", 0)
    assert ("owner@example.test", "authorized", "activated", 1) in audit_rows
    assert ("owner@example.test", "authorized", "duplicate", 0) in audit_rows


def test_api_offline_door_returns_503_and_nothing_fires(api):
    who, adapter = api
    adapter.controller_online = False
    who["current"] = OWNER
    code, body = _status(_route("/api/customer/access/doors/{door_id}/unlock", "POST"), "door-front", {})
    assert code == 503 and body["result"] == "offline" and adapter.calls == []


def test_api_configuration_is_owner_only_and_new_doors_start_in_dry_run(api):
    who, _ = api
    save = _route("/api/customer/access/doors", "POST")
    inclusion = _route("/api/customer/access/zwave/inclusion", "POST")
    listing = _route("/api/customer/access/doors", "GET")
    who["current"] = VIEWER
    assert _status(save, {"name": "X", "kind": "mock"})[0] == 403
    assert _status(inclusion, {"action": "start"})[0] == 403
    who["current"] = OWNER
    code, body = _status(save, {"name": "Garage", "kind": "mock", "camera_id": "cam-back"})
    assert code == 200 and body["status"]["dry_run"] is True
    code, detail = _status(save, {"name": "Lobby", "kind": "relay_maglock", "config": {"driver": "mock", "channel": 0}})
    assert code == 400 and "fire-alarm" in detail
    assert _status(save, {"name": "Z", "kind": "mock", "camera_id": "someone-elses"})[0] == 404
    assert {d["door_id"] for d in _status(listing)[1]["doors"]} == {"door-front", body["door_id"]}
