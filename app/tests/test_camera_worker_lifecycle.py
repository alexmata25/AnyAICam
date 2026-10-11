"""Codex blocker (2026-10-11): a removed camera's motion reader kept running
forever (seen live on Ryzen 1.2.4 for removed camera 4) and kept its HLS
demand; a camera added later never got a motion task.

Now: motion / AI-detector tasks follow the camera map
(reconcile_camera_workers(), run at startup and by camera_supervisor_watcher),
the motion loop itself stops within one iteration when its camera is no
longer configured (ffmpeg terminated, HLS hold released, per-camera state
dropped), and a re-added camera gets a fresh task. Other cameras are
unaffected."""
import asyncio
import sqlite3

import numpy as np
import pytest

import analytics_frames
import live_hls_demand
import main
import main_frame_grab


def _motion_holds(number):
    return sum(1 for owner in live_hls_demand.state(number)["holds"] if owner.startswith("motion:"))


@pytest.fixture(autouse=True)
def _reset():
    live_hls_demand.configure(mode="on_demand", idle_seconds=60)
    for registry in main._camera_worker_tasks.values():
        registry.clear()
    main._camera_in_service_cache[:] = [-1e18, None]
    yield
    live_hls_demand.configure(mode="always")
    for registry in main._camera_worker_tasks.values():
        registry.clear()
    main._camera_in_service_cache[:] = [-1e18, None]


# ---------------------------------------------------------------- camera map

def test_current_camera_numbers_from_the_cameras_table(monkeypatch, tmp_path):
    db_path = tmp_path / "c.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cameras(id TEXT, camera_number INTEGER)")
    conn.commit()

    class Conn:
        def __enter__(self):
            self.db = sqlite3.connect(db_path)
            self.db.row_factory = sqlite3.Row
            return self.db

        def __exit__(self, *exc):
            self.db.close()

    import partner_db
    monkeypatch.setattr(partner_db, "connection", lambda: Conn())
    monkeypatch.setattr(main, "legacy_camera_numbers_in_use", lambda: [])
    assert main.current_camera_numbers() is None             # nothing ever provisioned: unknown, keep workers
    conn.execute("INSERT INTO cameras VALUES('a',1),('b',4)")
    conn.commit()
    assert main.current_camera_numbers() == frozenset({1, 4})
    conn.execute("UPDATE cameras SET camera_number=NULL WHERE id='b'")   # removed: number cleared, row kept
    conn.commit()
    assert main.current_camera_numbers() == frozenset({1})
    conn.execute("UPDATE cameras SET camera_number=NULL")
    conn.commit()
    assert main.current_camera_numbers() == frozenset()       # the last camera removed still counts

    def broken():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(partner_db, "connection", broken)
    assert main.current_camera_numbers() is None              # unknown: never retire on a DB hiccup


def test_camera_in_service_uses_a_short_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "current_camera_numbers", lambda: calls.append(1) or frozenset({2}))
    assert main.camera_in_service(2) and not main.camera_in_service(4)
    assert len(calls) == 1
    monkeypatch.setattr(main, "current_camera_numbers", lambda: None)
    main._camera_in_service_cache[:] = [-1e18, None]
    assert main.camera_in_service(4)                          # unknown map: in service


# ---------------------------------------------------------------- reconcile

def test_reconcile_starts_retires_and_restarts_workers(monkeypatch):
    started, cancelled, retired = [], [], []

    def factory(kind):
        async def worker(number):
            started.append((kind, number))
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append((kind, number))
                raise
        return worker

    monkeypatch.setattr(main, "motion_detector", factory("motion"))
    monkeypatch.setattr(main, "ai_person_detector", factory("ai"))
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", True)
    monkeypatch.setattr(main, "AI_PERSON_DETECTION_ENABLED", True)
    monkeypatch.setattr(main, "retire_camera_pipeline", lambda number: retired.append(number))

    async def scenario():
        result = main.reconcile_camera_workers([1, 4])
        await asyncio.sleep(0)
        assert result["started"] == {"motion": [1, 4], "ai": [1, 4]}
        first_motion_1 = main._camera_worker_tasks["motion"][1]
        result = main.reconcile_camera_workers([1])                 # camera 4 removed
        await asyncio.sleep(0)
        assert result["retired"] == [4] and retired == [4]
        assert ("motion", 4) in cancelled and ("ai", 4) in cancelled
        assert main._camera_worker_tasks["motion"][1] is first_motion_1   # other camera untouched
        assert ("motion", 1) not in cancelled
        assert main.reconcile_camera_workers(None) == {"started": {}, "retired": []}   # unknown map: no retire
        result = main.reconcile_camera_workers([1, 4, 9])           # 4 re-added, 9 added later
        await asyncio.sleep(0)
        assert result["started"] == {"motion": [4, 9], "ai": [4, 9]}
        assert started.count(("motion", 4)) == 2                    # a fresh task
        assert 9 in main.ai_detection_state and 9 in main.ai_person_last_event
        for task in main.camera_worker_tasks():
            task.cancel()
        await asyncio.gather(*main.camera_worker_tasks(), return_exceptions=True)

    asyncio.run(scenario())


def test_watcher_reconciles_workers_both_ways(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "CAMERA_NOT_CONFIGURED_POLL_SECONDS", 0)
    monkeypatch.setattr(main, "get_camera_numbers", lambda *a, **k: [1])
    monkeypatch.setattr(main, "current_camera_numbers", lambda: frozenset({1}))
    monkeypatch.setattr(main, "start_camera_supervisors", lambda numbers, tasks: [])

    def reconcile(numbers):
        seen.append(numbers)
        raise asyncio.CancelledError()            # stop the endless watcher after one pass

    monkeypatch.setattr(main, "reconcile_camera_workers", reconcile)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.camera_supervisor_watcher([]))
    assert seen == [[1]]


# ---------------------------------------------------------------- the motion loop itself

class _Stdout:
    def __init__(self):
        self.frames = 0

    async def readexactly(self, n):
        await asyncio.sleep(0.01)
        self.frames += 1
        return bytes(n)


class _Process:
    def __init__(self):
        self.stdout = _Stdout()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_removed_camera_motion_loop_ends_stops_ffmpeg_and_releases_hls(monkeypatch, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    for number in (3, 4):
        (hls / f"camera{number}.m3u8").write_text("#EXTM3U\n")
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main._motion_source_selector, "mode", "hls")   # holds the HLS encoder while reading
    monkeypatch.setattr(main, "CAMERA_IN_SERVICE_CACHE_SECONDS", 0.05)
    settings = main.EventSettingsModel(camera=3, enabled=False)
    monkeypatch.setattr(main, "get_event_settings", lambda camera_number: settings)
    processes = {}

    async def fake_exec(*cmd, **kwargs):
        number = 3 if "camera3.m3u8" in " ".join(cmd) else 4
        processes.setdefault(number, []).append(_Process())
        return processes[number][-1]

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    configured = {3, 4}
    monkeypatch.setattr(main, "camera_in_service", lambda number: number in configured)
    forgotten = []
    real_retire = main.retire_camera_pipeline
    monkeypatch.setattr(main, "retire_camera_pipeline", lambda number: (forgotten.append(number), real_retire(number)))

    async def scenario():
        tasks = {n: asyncio.create_task(main.motion_detector(n)) for n in (3, 4)}
        await asyncio.sleep(0.3)
        assert _motion_holds(4) == 1 and live_hls_demand.wanted(4)
        configured.discard(4)                                        # camera 4 removed
        await asyncio.wait_for(tasks[4], timeout=2)                  # ends within one check
        assert processes[4][-1].terminated                           # its ffmpeg was stopped
        assert live_hls_demand.state(4)["holds"] == [] and forgotten == [4]
        assert not tasks[3].done() and _motion_holds(3) == 1   # camera 3 unaffected
        assert not processes[3][-1].terminated
        tasks[3].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[3]

    asyncio.run(scenario())


def test_cancelled_motion_task_releases_its_hls_hold(monkeypatch, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "camera5.m3u8").write_text("#EXTM3U\n")
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main._motion_source_selector, "mode", "hls")
    monkeypatch.setattr(main, "get_event_settings", lambda n: main.EventSettingsModel(camera=5, enabled=False))
    process = _Process()

    async def fake_exec(*cmd, **kwargs):
        return process

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(main, "camera_in_service", lambda number: True)

    async def scenario():
        task = asyncio.create_task(main.motion_detector(5))
        await asyncio.sleep(0.2)
        assert _motion_holds(5) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.terminated and live_hls_demand.state(5)["holds"] == []

    asyncio.run(scenario())


# ---------------------------------------------------------------- per-camera state

def test_retire_drops_hls_demand_reader_grab_cache_and_selector_state(monkeypatch):
    live_hls_demand.touch(6, "http")
    live_hls_demand.hold(6, "motion")
    live_hls_demand.note_source_frame(6)
    main._motion_source_selector._failures[6] = 2
    main._motion_source_selector._last_kind[6] = "substream"
    stopped = []

    class Reader:
        def stop(self):
            stopped.append(6)

    main.ai_frames._readers[6] = Reader()
    main.main_frame_grabber._cache[6] = (0.0, "frame")
    main._ai_detection_cache[6] = (1, {})
    main.retire_camera_pipeline(6)
    assert live_hls_demand.state(6) == {"mode": "on_demand", "effective": "on_demand", "holds": [], "seconds_since_demand": None}
    assert not live_hls_demand.wanted(6)
    assert 6 not in main._motion_source_selector._failures and 6 not in main._motion_source_selector._last_kind
    assert stopped == [6] and 6 not in main.ai_frames._readers
    assert 6 not in main.main_frame_grabber._cache and 6 not in main._ai_detection_cache


def test_ai_frames_never_reads_or_demands_hls_for_a_removed_camera():
    stopped = []

    class Reader:
        kind, last_error = "main", None

        def stop(self):
            stopped.append(1)

    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="hls", start_housekeeping=False,
                                              reader_factory=lambda n: pytest.fail("no reader for a removed camera"),
                                              in_service=lambda n: n != 8)
    frames._readers[8] = Reader()
    result = frames.get_frame(8)
    assert result["ok"] is False and "not configured" in result["error"]    # not None: no HLS read
    assert stopped == [1] and 8 not in frames._readers
    assert frames.get_frame(2) is None                                     # configured camera: hls mode as before


def test_main_frame_grabber_forget():
    grabber = main_frame_grab.MainFrameGrabber(lambda n: None, enabled=True)
    grabber._cache[3] = (0.0, np.zeros((2, 2, 3)))
    grabber._failed_until[3] = 1e18
    grabber.forget(3)
    assert 3 not in grabber._cache and 3 not in grabber._failed_until


# ---------------------------------------------------------------- races (Codex re-review of 4473cf2)

def test_stale_yolo_result_is_never_delivered_after_retire_and_re_add(monkeypatch):
    """A detect_objects_frame() running while the camera is retired and
    re-added (new reader, seq restarting at 1) must not cache or deliver
    its stale result to the new reader's frame 1."""
    import types
    frame = np.zeros((360, 640, 3), np.uint8)
    reader_generation = {"value": 0}

    class Frames:
        mode = "main"

        def forget(self, camera_number):
            pass

        def get_frame(self, camera_number, wait=None):
            return {"ok": True, "frame": frame, "captured_at": None, "seq": 1,
                    "generation": reader_generation["value"], "source": "main", "error": None}

    monkeypatch.setattr(main, "ai_frames", Frames())
    monkeypatch.setattr(main, "YOLO", object())
    calls = []

    def box(x):
        tensor = lambda values: [types.SimpleNamespace(item=lambda v=values: v, tolist=lambda v=values: v)]
        return types.SimpleNamespace(cls=tensor(0), conf=tensor(0.9), xyxy=tensor([x, 10, x + 20, 60]))

    def predict(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            main.retire_camera_pipeline(7)        # retired (and re-added) while YOLO runs
            reader_generation["value"] = 1
        return [types.SimpleNamespace(names={0: "person"}, boxes=[box(100 if len(calls) == 1 else 300)])]

    monkeypatch.setattr(main, "get_yolo_model", lambda: types.SimpleNamespace(predict=predict))
    stale = main.detect_objects_frame(7)
    assert stale["ok"] is False                                  # not delivered
    assert 7 not in main._ai_detection_cache                     # not cached
    fresh = main.detect_objects_frame(7)                         # the new reader's seq 1
    assert fresh["ok"] and fresh["detections"][0]["x"] == 300 and len(calls) == 2


def test_get_frame_never_creates_a_reader_after_retirement():
    created = []

    class Reader:
        kind, last_error = "main", None

        def ensure_running(self):
            pass

        def latest(self, max_age=None, wait=0.0):
            return None

        def stop(self):
            pass

    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="main", start_housekeeping=False,
                                              reader_factory=lambda n: created.append(n) or Reader())
    generation = frames.generation(3)
    frames.forget(3)                                             # retired after the caller's service check
    assert frames._reader(3, generation) is None and created == []
    assert frames._reader(3, frames.generation(3)) is not None and created == [3]

    frames2 = analytics_frames.AnalyticsFrames(lambda n: None, mode="main", start_housekeeping=False)

    class Retiring(Reader):
        def latest(self, max_age=None, wait=0.0):
            frames2.forget(4)                                    # retired while waiting for the frame
            return (np.zeros((2, 2, 3), np.uint8), None, 1)

    frames2._custom_factory = lambda n: Retiring()
    assert frames2.get_frame(4, wait=0)["ok"] is False


def test_reader_thread_never_spawns_for_a_removed_camera():
    spawned = []
    reader = analytics_frames.FrameReader(5, lambda n: analytics_frames.SourceSpec("rtsp://x", 8, 6, "main"),
                                          popen=lambda *a, **k: spawned.append(1), log=lambda m: None,
                                          in_service=lambda n: False)
    reader.ensure_running()
    reader._thread.join(timeout=5)
    assert not reader._thread.is_alive() and spawned == []


def test_housekeeping_forgets_readers_of_removed_cameras():
    stopped = []

    class Reader:
        def stop(self):
            stopped.append(1)

        def housekeeping(self):
            pass

    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="main", start_housekeeping=False,
                                              in_service=lambda n: n != 2)
    frames._readers[2] = Reader()
    frames.housekeeping()
    assert stopped == [1] and 2 not in frames._readers and frames.generation(2) == 1


def test_reader_kills_an_ffmpeg_that_started_while_it_was_being_stopped():
    class Proc:
        stdout = None
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    procs = []
    holder = {}

    def popen(*a, **k):
        holder["reader"].stop()                                  # stop() races the spawn: nothing to kill yet
        procs.append(Proc())
        return procs[-1]

    reader = analytics_frames.FrameReader(6, lambda n: analytics_frames.SourceSpec("rtsp://x", 8, 6, "main"),
                                          popen=popen, log=lambda m: None)
    holder["reader"] = reader
    reader.ensure_running()
    reader._thread.join(timeout=5)
    assert procs and procs[0].returncode == -9 and reader._process is None


def test_old_motion_finalizer_cannot_release_the_replacement_hold():
    live_hls_demand.hold(4, "motion:old")
    live_hls_demand.hold(4, "motion:new")
    live_hls_demand.release(4, "motion:old")                     # the old task's finally
    assert live_hls_demand.state(4)["holds"] == ["motion:new"] and live_hls_demand.wanted(4)


def test_each_motion_task_holds_with_its_own_token(monkeypatch, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "camera4.m3u8").write_text("#EXTM3U\n")
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setattr(main._motion_source_selector, "mode", "hls")
    monkeypatch.setattr(main, "get_event_settings", lambda n: main.EventSettingsModel(camera=4, enabled=False))
    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", lambda *a, **k: _spawn())
    monkeypatch.setattr(main, "camera_in_service", lambda number: True)

    async def _make():
        return _Process()

    def _spawn():
        return _make()

    async def scenario():
        old = asyncio.create_task(main.motion_detector(4))
        new = asyncio.create_task(main.motion_detector(4))
        await asyncio.sleep(0.2)
        holds = live_hls_demand.state(4)["holds"]
        assert len(holds) == 2 and len(set(holds)) == 2
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old
        assert _motion_holds(4) == 1 and live_hls_demand.wanted(4)     # the new task still holds
        new.cancel()
        with pytest.raises(asyncio.CancelledError):
            await new
        assert _motion_holds(4) == 0

    asyncio.run(scenario())


def test_replacement_waits_until_the_cancelled_task_has_finished(monkeypatch):
    started = []

    async def slow_cleanup_worker(number):
        started.append(number)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await gate.wait()                                    # still cleaning up
            raise

    monkeypatch.setattr(main, "motion_detector", slow_cleanup_worker)
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", True)
    monkeypatch.setattr(main, "AI_PERSON_DETECTION_ENABLED", False)
    monkeypatch.setattr(main, "retire_camera_pipeline", lambda number: None)

    async def scenario():
        global gate
        gate = asyncio.Event()
        main.reconcile_camera_workers([4])
        await asyncio.sleep(0)
        main.reconcile_camera_workers([])                        # removed
        await asyncio.sleep(0)
        assert main.reconcile_camera_workers([4])["started"] == {}   # re-added: old task still cleaning up
        gate.set()
        await asyncio.sleep(0.05)
        assert main.reconcile_camera_workers([4])["started"] == {"motion": [4]}
        await asyncio.sleep(0)
        assert started == [4, 4]
        for task in main.camera_worker_tasks():
            task.cancel()
        await asyncio.gather(*main.camera_worker_tasks(), return_exceptions=True)

    asyncio.run(scenario())
    for registry in main._camera_worker_retiring.values():
        registry.clear()


def test_cancel_during_ffmpeg_spawn_leaves_no_process(monkeypatch):
    class Proc:
        returncode = None
        killed = False
        reaped = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.reaped = True
            return self.returncode

    proc = Proc()

    async def slow_exec(*cmd, **kwargs):
        await asyncio.sleep(0.2)                                 # ffmpeg still starting
        return proc

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", slow_exec)

    async def scenario():
        task = asyncio.create_task(main.spawn_process_cancellation_safe("ffmpeg"))
        await asyncio.sleep(0.05)
        task.cancel()                                            # cancelled during the spawn
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed and proc.reaped                       # it started anyway: killed and reaped

    asyncio.run(scenario())


def test_main_frame_grab_started_before_retirement_is_not_cached():
    import types
    holder = {}

    def run(args, **kwargs):
        holder["grabber"].forget(2)                              # retired during the grab
        return types.SimpleNamespace(returncode=0, stdout=b"x")

    grabber = main_frame_grab.MainFrameGrabber(lambda n: "rtsp://x", run=run, decode=lambda raw: np.zeros((4, 4, 3)),
                                               enabled=True)
    holder["grabber"] = grabber
    assert grabber.grab(2) is None and 2 not in grabber._cache
