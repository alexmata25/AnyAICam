"""R-REC-1 / R-REC-1b (Linux 1.2.7.1): event-only recording on the edge VMS.
Green U-R1 / U-R2 / U-R3 / U-R5 plus Red design G (event-buffer janitor).

- U-R1: _local_recording_settings() returns mode 'event' for a stored None,
  '', 'continuous', unknown or corrupt value, for a missing row and when the
  database cannot be read; the coercion is logged once per camera/value.
- Strategy: the recording supervisor only ever starts the event buffer;
  start_recording() (continuous 300 s segments) is unreachable from it.
- U-R2: the camera-slot placeholders created by partner onboarding and by
  self-service appliance provisioning persist 'event'.
- U-R3: a provisioning request / partner quote cannot select 'continuous'
  (400) and no customer- or partner-facing page offers it.
- U-R5: a recording-mode change reaches the RUNNING recorder within
  N = 10 s of the edge DB change (main.RECORDING_SWITCH_BOUND_SECONDS):
  at most RECORDING_STRATEGY_CHECK_SECONDS (4 s) until the supervisor
  re-reads the mode -- immediately (<= RECORDING_POLL_SECONDS, 1 s) when the
  edge sync notifies it -- plus at most RECORDING_GRACEFUL_STOP_SECONDS (5 s)
  for ffmpeg to close its segment after SIGTERM before it is killed, then an
  immediate restart: no reconnect count, no 10 s backoff. The cloud->edge
  sync interval (<= 60 s) is separate and not counted against N.
- G: every supervisor slot -- including a camera added after boot -- gets its
  own event_buffer_janitor whether MOTION_DETECTION_ENABLED is on or off, and
  the buffer stays bounded (age cap; stale in-flight windows cannot pin
  segments beyond the hard ceiling). There is no byte cap: bytes = bitrate x
  the age cap.
"""
import asyncio
import inspect
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
import partner_workspace
from database_backend import override_target
from partner_db import connection, initialize_database

ROOT = Path(__file__).resolve().parents[2]


# ============================================================ U-R1: the reader


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "r_rec_1_edge.db"


def _seed_cameras(db_path, modes):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p-1','P','2026-01-01')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','p-1','C','c@example.test','active','2026-01-01')")
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S','2026-01-01')")
            for number, mode in enumerate(modes, start=1):
                db.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,local_recording_mode,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (f"cam-{number}", "cust-1", "site-1", f"Camera {number}", "configured", number, mode, "2026-01-01"))


STORED_VALUES = [None, "", "continuous", "CONTINUOUS", "Event", "sometimes", '{"mode": "contin', "event"]


def test_local_recording_mode_constant_is_event():
    assert main.LOCAL_RECORDING_MODE == "event"


@pytest.mark.parametrize("stored", STORED_VALUES)
def test_reader_returns_event_for_every_stored_value(db_path, stored):
    _seed_cameras(db_path, [stored])
    with override_target(sqlite_path=str(db_path)):
        assert main._local_recording_settings(1)["mode"] == "event"


def test_reader_returns_event_for_a_camera_without_a_row(db_path):
    """The legacy local add / installer camera phase: a camera known only to
    the local camera map has no row at all."""
    _seed_cameras(db_path, [])
    with override_target(sqlite_path=str(db_path)):
        assert main._local_recording_settings(42)["mode"] == "event"


def test_reader_returns_event_when_the_database_cannot_be_read(monkeypatch):
    import partner_db

    def broken():
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(partner_db, "connection", broken)
    settings = main._local_recording_settings(3)
    assert settings["mode"] == "event"
    assert settings["pre_roll_seconds"] > 0


def test_reader_logs_a_coerced_value_once(db_path, monkeypatch):
    _seed_cameras(db_path, ["continuous"])
    logged = []
    monkeypatch.setattr(main, "structured_log", lambda event, level="info", **fields: logged.append((event, fields)))
    monkeypatch.setattr(main, "_local_recording_mode_coerced", set())
    with override_target(sqlite_path=str(db_path)):
        for _ in range(3):
            main._local_recording_settings(1)
    coerced = [fields for event, fields in logged if event == "recording.local_mode_coerced_to_event"]
    assert len(coerced) == 1 and coerced[0]["camera_number"] == 1


# ============================================================ strategy: event buffer only


@pytest.mark.parametrize("strategy", ["event", "continuous", None, "garbage"])
def test_the_only_recording_starter_is_the_event_buffer(strategy):
    assert main._recording_starter_for(strategy) is main.start_event_recording_buffer


def test_the_supervisor_cannot_reach_start_recording():
    """Red decision 2: start_recording() stays in the code but is unreachable."""
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    supervisor = source.split("async def process_supervisor", 1)[1].split("\nasync def ", 1)[0]
    assert re.search(r"\bstart_recording\b", supervisor) is None
    assert re.search(r"\bstart_recording\b", inspect.getsource(main._recording_starter_for)) is None
    import ast
    callers = [node.lineno for node in ast.walk(ast.parse(source))
               if isinstance(node, (ast.Call,)) and getattr(node.func, "id", None) == "start_recording"]
    referenced = [node.lineno for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Name) and node.id == "start_recording"]
    assert callers == [] and referenced == [], "start_recording() must have no caller or reference"


class _FakeProcess:
    def __init__(self, *, exits_on_terminate=True):
        self.stderr = None
        self.returncode = None
        self.exits_on_terminate = exits_on_terminate
        self.terminated_at = None
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated_at = time.monotonic()
        if self.exits_on_terminate:
            self.returncode = 255

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 3600)
        while self.returncode is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            time.sleep(0.01)
        return self.returncode


@pytest.fixture()
def supervisor_env(monkeypatch):
    """One recording supervisor for camera 5 with fake recorders; returns the
    list of (monotonic time, process) starts."""
    starts = []

    def fake_event_buffer(camera_number):
        process = _FakeProcess(exits_on_terminate=supervisor_env_options["exits_on_terminate"])
        starts.append((time.monotonic(), process))
        return process

    supervisor_env_options = {"exits_on_terminate": True}

    def forbidden_continuous(camera_number):
        raise AssertionError("start_recording() must never be started")

    monkeypatch.setattr(main, "start_event_recording_buffer", fake_event_buffer)
    monkeypatch.setattr(main, "start_recording", forbidden_continuous)
    monkeypatch.setattr(main, "camera_process_state", {5: {}})
    monkeypatch.setattr(main, "camera_reconnect_counts", {5: 0})
    monkeypatch.setattr(main, "ffmpeg_processes", [])
    monkeypatch.setattr(main, "_recording_strategy_wake", set())
    return SimpleNamespace(starts=starts, options=supervisor_env_options)


def test_a_null_mode_camera_starts_the_event_buffer_not_continuous(db_path, supervisor_env):
    _seed_cameras(db_path, [None, None, None, None, None])

    async def scenario():
        with override_target(sqlite_path=str(db_path)):
            task = asyncio.create_task(main.process_supervisor(5, "recording", stagger_index=0))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
    assert len(supervisor_env.starts) == 1


# ============================================================ U-R5: live switch, N = 10 s


def test_the_live_switch_bound_is_ten_seconds():
    assert main.RECORDING_SWITCH_BOUND_SECONDS == 10
    assert (main.RECORDING_STRATEGY_CHECK_SECONDS + main.RECORDING_POLL_SECONDS
            + main.RECORDING_GRACEFUL_STOP_SECONDS) <= main.RECORDING_SWITCH_BOUND_SECONDS


def _run_switch(monkeypatch, supervisor_env, *, notify, timeout):
    """Starts the recorder with the mode the reader reports ('legacy'), then
    changes the edge DB view to 'event' and measures how long the RUNNING
    recorder takes to be stopped and restarted."""
    current = {"mode": "legacy-continuous"}

    def reader(camera_number):
        return {"mode": current["mode"], "pre_roll_seconds": 5, "post_roll_seconds": 5,
                "merge_gap_seconds": 8, "max_event_seconds": 300}

    monkeypatch.setattr(main, "_local_recording_settings", reader)
    result = {}

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(5, "recording", stagger_index=0))
        while not supervisor_env.starts:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        changed_at = time.monotonic()
        current["mode"] = "event"                      # the edge DB change
        if notify:
            main.request_recording_strategy_check([5])  # what the edge sync worker does
        while len(supervisor_env.starts) < 2 and time.monotonic() - changed_at < timeout:
            await asyncio.sleep(0.02)
        result["elapsed"] = time.monotonic() - changed_at
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    return result["elapsed"]


def test_a_notified_mode_change_switches_the_running_recorder_promptly(monkeypatch, supervisor_env):
    elapsed = _run_switch(monkeypatch, supervisor_env, notify=True, timeout=12)
    assert len(supervisor_env.starts) == 2, "the running recorder was not restarted"
    first = supervisor_env.starts[0][1]
    assert first.terminated_at is not None and not first.killed       # graceful stop
    assert elapsed <= main.RECORDING_POLL_SECONDS + 1.0
    assert main.camera_reconnect_counts[5] == 0                        # not counted as a failure
    assert ("retrying" not in main.camera_process_state[5].values())


def test_an_unnotified_mode_change_switches_within_n_seconds(monkeypatch, supervisor_env):
    elapsed = _run_switch(monkeypatch, supervisor_env, notify=False, timeout=12)
    assert len(supervisor_env.starts) == 2
    assert elapsed <= main.RECORDING_SWITCH_BOUND_SECONDS
    assert main.camera_reconnect_counts[5] == 0


def test_a_recorder_ignoring_sigterm_is_killed_and_the_switch_stays_within_n(monkeypatch, supervisor_env):
    supervisor_env.options["exits_on_terminate"] = False
    elapsed = _run_switch(monkeypatch, supervisor_env, notify=True, timeout=12)
    assert len(supervisor_env.starts) == 2
    assert supervisor_env.starts[0][1].killed
    assert elapsed <= main.RECORDING_SWITCH_BOUND_SECONDS
    assert main.camera_reconnect_counts[5] == 0


def test_an_unchanged_mode_never_restarts_the_recorder(monkeypatch, supervisor_env):
    monkeypatch.setattr(main, "RECORDING_STRATEGY_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(main, "RECORDING_POLL_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(5, "recording", stagger_index=0))
        await asyncio.sleep(0.2)
        main.request_recording_strategy_check([5])
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert len(supervisor_env.starts) == 1
    assert supervisor_env.starts[0][1].terminated_at is None


def test_the_edge_sync_notification_reaches_the_supervisor():
    import edge_camera_sync
    assert main.request_recording_strategy_check in edge_camera_sync._recording_mode_listeners


# ============================================================ G: a janitor for every slot


@pytest.mark.parametrize("motion_enabled", [True, False])
def test_every_supervisor_slot_gets_a_janitor_including_a_camera_added_after_boot(monkeypatch, motion_enabled):
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", motion_enabled)
    monkeypatch.setattr(main, "_supervised_camera_numbers", set())
    started = []

    async def fake_supervisor(camera_number, mode, *, stagger_index=None):
        started.append(("supervisor", camera_number, mode))

    async def fake_janitor(camera_number):
        started.append(("janitor", camera_number))

    monkeypatch.setattr(main, "process_supervisor", fake_supervisor)
    monkeypatch.setattr(main, "event_buffer_janitor", fake_janitor)

    async def scenario():
        tasks = []
        main.start_camera_supervisors([1, 2], tasks)        # boot slots
        main.start_camera_supervisors([1, 2, 9], tasks)     # camera 9 provisioned after boot (watcher path)
        await asyncio.gather(*tasks)
        return tasks

    tasks = asyncio.run(scenario())
    assert sorted(n for kind, n, *_ in started if kind == "janitor") == [1, 2, 9]
    assert len(tasks) == 9  # live + recording + janitor per slot, each cancelled at shutdown


def test_lifespan_no_longer_starts_boot_only_janitors():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "event_buffer_janitor_tasks" not in source
    assert "event_buffer_janitor(camera_number)" in inspect.getsource(main.start_camera_supervisors)


def _make_buffer(folder: Path, camera_number: int, now: datetime, count: int) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for index in range(count):
        start = now - timedelta(seconds=30 * (index + 1))
        path = folder / f"buf{camera_number}_{start:%Y-%m-%d_%H-%M-%S}.mkv"
        path.write_bytes(b"x" * 1024)
        stamp = time.time() - 30 * index - 31
        os.utime(path, (stamp, stamp))
        files.append(path)
    return files


def _one_janitor_pass(camera_number):
    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def one_pass_sleep(seconds):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    async def scenario():
        try:
            await main.event_buffer_janitor(camera_number)
        except asyncio.CancelledError:
            pass

    original = main.asyncio.sleep
    main.asyncio.sleep = one_pass_sleep
    try:
        asyncio.run(scenario())
    finally:
        main.asyncio.sleep = original


@pytest.mark.parametrize("motion_enabled", [True, False])
def test_a_post_boot_cameras_buffer_stays_bounded_by_the_age_cap(tmp_path, monkeypatch, motion_enabled):
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", motion_enabled)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "_open_event_recordings", {})
    monkeypatch.setattr(main.ai_activities, "in_flight_windows", lambda camera_number, lead_seconds=0: [])
    now = datetime.now()
    folder = tmp_path / "camera9" / main.EVENT_BUFFER_SUBFOLDER_NAME
    _make_buffer(folder, 9, now, 120)  # one hour of 30 s segments
    _one_janitor_pass(9)
    remaining = sorted(folder.glob("*.mkv"))
    cap = main.event_buffer_age_cap_seconds(main._local_recording_settings(9))
    assert cap == 80  # defaults: pre-roll 5 + margin 10 + detection lookback 60 + post-roll 5
    assert 0 < len(remaining) <= cap // main.EVENT_BUFFER_SEGMENT_SECONDS + 1
    for path in remaining:
        end = main._buffer_segment_start(path, 9) + timedelta(seconds=main.EVENT_BUFFER_SEGMENT_SECONDS)
        assert (now - end).total_seconds() <= cap + 1


def test_a_stale_in_flight_window_cannot_pin_the_buffer_beyond_the_hard_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    now = datetime.now()
    monkeypatch.setattr(main, "_open_event_recordings", {9: {"start": now - timedelta(hours=2), "end": now}})
    monkeypatch.setattr(main.ai_activities, "in_flight_windows", lambda camera_number, lead_seconds=0: [])
    folder = tmp_path / "camera9" / main.EVENT_BUFFER_SUBFOLDER_NAME
    _make_buffer(folder, 9, now, 120)
    _one_janitor_pass(9)
    ceiling = main.event_buffer_hard_max_age_seconds(main._local_recording_settings(9))
    assert ceiling == 440  # 80 + max event 300 + two segments
    remaining = sorted(folder.glob("*.mkv"))
    assert remaining
    for path in remaining:
        end = main._buffer_segment_start(path, 9) + timedelta(seconds=main.EVENT_BUFFER_SEGMENT_SECONDS)
        assert (now - end).total_seconds() <= ceiling + 1


# ============================================================ U-R2: placeholder rows


def _route(path, method="POST"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request():
    return SimpleNamespace(headers={}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


def test_partner_onboarding_placeholders_persist_event(db_path, monkeypatch):
    import pricing_config
    onboard_customer = _route("/api/partner/customers/onboard")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('anyaicam-primary','AnyAiCam','2026-01-01')")
        conn.commit()
        monkeypatch.setattr(partner_workspace, "partner_identity",
                            lambda request: {"role": "partner_owner", "partner_id": "anyaicam-primary", "email": "op@example.test"})
        config = pricing_config.load_pricing()
        config["partner"]["pricing_mode"] = "percentage"
        config["partner"]["percentage_discount"] = 20
        monkeypatch.setattr(pricing_config, "load_pricing", lambda: config)
        onboard_customer(_fake_request(), {
            "name": "New", "company": "New Co", "email": "new-owner@example.test", "phone": "555-0100",
            "status": "trial", "sites": [{"name": "HQ"}], "appliance_type": "AnyAiCam mini PC", "deployment_mode": "local",
            "pricing": {"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 3, "addons": []}})
        conn = sqlite3.connect(db_path)
        modes = [r[0] for r in conn.execute("SELECT local_recording_mode FROM cameras")]
    assert modes == ["event"] * 3


def test_self_service_provisioning_placeholders_persist_event(db_path, tmp_path, monkeypatch):
    import provisioning_service
    from provisioning_service import MockProvisioningBackend, reset_provisioning_backend_for_tests
    reset_provisioning_backend_for_tests()
    monkeypatch.setattr(provisioning_service, "_backend_instance", MockProvisioningBackend(path=tmp_path / "mock.json"))
    provision = _route("/api/customer/appliances/provision")
    try:
        with override_target(sqlite_path=str(db_path)):
            initialize_database()
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P','2026-01-01')")
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','owner@example.test','active','2026-01-01')")
            conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,status,fulfillment_status,created_at,updated_at) "
                         "VALUES('hw-1','cust-1','anyaicam-starter','AnyAiCam Starter','price_test_hw',1,50,'paid','unfulfilled','2026-01-01','2026-01-01')")
            conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
                         "VALUES('ent-1','cust-1','camera_slots_local',2,'active','2026-01-01','2026-01-01')")
            conn.commit()
            monkeypatch.setattr(partner_workspace, "partner_identity",
                                lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"})
            assert provision(_fake_request(), {"site_name": "HQ"})["status"] == "provisioned"
            conn = sqlite3.connect(db_path)
            modes = [r[0] for r in conn.execute("SELECT local_recording_mode FROM cameras")]
    finally:
        reset_provisioning_backend_for_tests()
    assert modes == ["event"] * 2


# ============================================================ U-R3: requests, quotes, pages


def _seed_appliance(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,company,email,status,created_at) VALUES('cust-1','partner-1','C','C','c@example.test','active','2026-01-01')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-cust-1','cust-1','Main','2026-01-01')")
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,last_check_in,created_at) "
                     "VALUES('appl-1','cust-1','site-cust-1','AIC-TEST1','activated','online','2026-01-01T00:00:10','2026-01-01')")
        conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,last_used_at,created_at) VALUES('cred-1','appl-1','x','2026-01-01T00:00:05','2026-01-01')")
        conn.commit()


@pytest.fixture()
def licensed(monkeypatch):
    import customer_entitlements
    monkeypatch.setattr(customer_entitlements, "total_camera_slots", lambda customer_id: 64)
    # Provisioning now uses usable_camera_capacity(), which also requires a
    # software entitlement. This fixture isolates the recording-mode policy
    # under test; without an explicit licensed-capacity stub the endpoint
    # correctly returns its capacity-refusal JSONResponse before creating a
    # job, and the test would inspect the wrong response shape.
    monkeypatch.setattr(customer_entitlements, "usable_camera_capacity", lambda customer_id: 64)


@pytest.mark.parametrize("mode,expected", [(None, "motion"), ("motion", "motion"), ("disabled", "disabled"),
                                           ("continuous", 400), ("Continuous", 400), ("anything", 400)])
def test_a_provisioning_request_cannot_select_continuous(db_path, monkeypatch, licensed, mode, expected):
    request_camera_provisioning = _route("/api/customer/cameras/provision")
    monkeypatch.delenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", raising=False)
    _seed_appliance(db_path)
    payload = {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door"}
    if mode is not None:
        payload["recording_mode"] = mode
    with override_target(sqlite_path=str(db_path)):
        monkeypatch.setattr(partner_workspace, "partner_identity",
                            lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "c@example.test"})
        if expected == 400:
            with pytest.raises(Exception) as excinfo:
                request_camera_provisioning(_fake_request(), payload)
            assert getattr(excinfo.value, "status_code", None) == 400
            stored = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM camera_provisioning_requests").fetchone()[0]
            assert stored == 0
        else:
            job = request_camera_provisioning(_fake_request(), payload)
            stored = sqlite3.connect(db_path).execute("SELECT recording_mode FROM camera_provisioning_requests WHERE id=?", (job["job_id"],)).fetchone()[0]
            assert stored == expected


@pytest.mark.parametrize("value", ["continuous", "CONTINUOUS", "24/7"])
def test_a_partner_quote_cannot_select_continuous(value):
    with pytest.raises(Exception) as excinfo:
        main._quote_recording_mode(value)
    assert getattr(excinfo.value, "status_code", None) == 400


@pytest.mark.parametrize("value", [None, "", "motion"])
def test_a_partner_quote_defaults_to_motion(value):
    assert main._quote_recording_mode(value) == "motion"


def test_both_quote_routes_validate_the_recording_mode():
    assert "_quote_recording_mode(payload.recording_mode)" in inspect.getsource(main.create_partner_quote)
    assert "_quote_recording_mode(payload.recording_mode)" in inspect.getsource(main.update_partner_quote)


def test_a_legacy_continuous_quote_is_priced_as_motion():
    legacy = main.partner_quote_price({"resolution": "4mp", "recording_mode": "continuous", "retention_days": 14, "camera_quantity": 1})
    motion = main.partner_quote_price({"resolution": "4mp", "recording_mode": "motion", "retention_days": 14, "camera_quantity": 1})
    assert legacy == motion


CONTINUOUS_OFFER = re.compile(r"<option[^>]*>[^<]*(continuous|24/7)[^<]*</option>|value=[\"']continuous[\"']|continuous[\"']?\s*:\s*\{", re.IGNORECASE)


@pytest.mark.parametrize("relative", [
    "app/main.py", "app/pricing_portal.py", "app/partner_portal.py", "app/partner_workspace.py", "app/business_portal.py",
])
def test_no_ui_source_offers_continuous_recording(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert CONTINUOUS_OFFER.search(text) is None, CONTINUOUS_OFFER.search(text).group(0)


def test_status_and_dashboard_never_label_a_camera_continuous():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '"continuous recording"' not in (ROOT / "app" / "partner_workspace.py").read_text(encoding="utf-8")
    assert '"recording_mode": "event" if event_mode else "continuous"' not in source
    assert '"Event" if row["local_recording_mode"] == "event" else "Continuous"' not in source
    assert '_dashboard_recording_stat = "Continuous"' not in source
