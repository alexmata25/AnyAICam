"""Bounded per-camera Live Relay concurrency (2026-09-13): regression
coverage for live_relay_uploader.py's _relay_tick()/_relay_camera_bounded()
-- the fix for a confirmed-live defect where the previous strictly
sequential for-loop in live_relay_worker() made every camera wait for
every other camera's full session-check + upload + notify round trip,
producing a measured 1-34 second per-camera freshness spread against the
30-second manifest staleness threshold under real 5-camera load.

Matches this project's own established async-test convention (see
test_ai_safety_concurrency.py): asyncio.run(scenario()) wrapping a plain
async function, no pytest-asyncio dependency.

Each test monkeypatches live_relay_uploader._relay_camera_once directly
(the one function _relay_camera_bounded actually calls via
asyncio.to_thread) rather than boto3/urllib -- concurrency, isolation,
and cancellation are properties of the SCHEDULING harness added this
pass, not of _relay_camera_once's own already-existing, unchanged
internals. test_ordering_within_one_camera_survives_the_new_harness is
the one exception: it exercises the real _relay_camera_once end to end
(with only its network-facing dependencies mocked) to prove wrapping it
in the new bounded-concurrency harness does not disturb the pre-existing
per-camera sequential upload order.
"""

import asyncio
import threading
import time

import pytest

import live_relay_uploader as lru


@pytest.fixture(autouse=True)
def _reset_relay_globals(monkeypatch):
    """Every test gets a clean slate -- these are all real module-level
    dicts live_relay_worker()/_relay_camera_once() mutate in production."""
    monkeypatch.setattr(lru, "_active_cameras", {})
    monkeypatch.setattr(lru, "_sessions", {})
    monkeypatch.setattr(lru, "_uploaded_segments", {})
    monkeypatch.setattr(lru, "_next_sequence", {})
    monkeypatch.setattr(lru, "_last_applied_relay_state", {})
    monkeypatch.setattr(lru, "_reconcile_relay_commands", lambda: None)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- bounded concurrency


def test_different_cameras_run_concurrently_not_serially(monkeypatch):
    """The actual regression: 5 cameras, each taking a real, measurable
    slice of time, must all be in flight together -- not one after
    another. A tick's total wall time should be close to ONE camera's
    own duration, not five times that."""
    lru._active_cameras.update({n: f"cam-{n}" for n in range(1, 6)})
    call_duration = 0.15

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        time.sleep(call_duration)

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        started = time.monotonic()
        await lru._relay_tick(semaphore, "/hls")
        return time.monotonic() - started

    elapsed = _run(scenario())
    # 5 serial calls would take >= 5*0.15=0.75s; concurrent execution
    # should finish in roughly one call's duration plus scheduling slack.
    assert elapsed < call_duration * 2.5, f"took {elapsed}s -- looks serial, not concurrent"


def test_concurrency_never_exceeds_the_configured_bound(monkeypatch):
    """More active cameras than the semaphore's own size -- the number
    of _relay_camera_once calls actually running AT THE SAME INSTANT
    must never exceed the bound, even though all of them eventually run
    within the same tick."""
    camera_count = 10
    bound = 3
    lru._active_cameras.update({n: f"cam-{n}" for n in range(1, camera_count + 1)})
    current = 0
    peak = 0
    lock = threading.Lock()

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.05)
        with lock:
            current -= 1

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(bound)
        await lru._relay_tick(semaphore, "/hls")

    _run(scenario())
    assert peak <= bound
    assert peak > 1  # proves it's genuinely concurrent, not accidentally still serial


# ------------------------------------------------------------- slow-camera / exception isolation


def test_one_slow_camera_does_not_delay_the_others(monkeypatch):
    """A single stuck/slow camera must not hold up the rest -- the fast
    cameras' own calls must complete promptly, independent of the slow
    one's own duration."""
    lru._active_cameras.update({1: "cam-slow", 2: "cam-fast-a", 3: "cam-fast-b"})
    fast_completion_times = {}
    started = time.monotonic()

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        if camera_number == 1:
            time.sleep(0.4)
        else:
            fast_completion_times[camera_number] = time.monotonic() - started

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        await lru._relay_tick(semaphore, "/hls")

    _run(scenario())
    assert set(fast_completion_times) == {2, 3}
    for camera_number, completed_at in fast_completion_times.items():
        assert completed_at < 0.4, f"camera {camera_number} was delayed by the slow camera"


def test_one_cameras_exception_does_not_abort_the_others(monkeypatch, caplog):
    """A real exception from one camera's _relay_camera_once must never
    prevent the other cameras in the same tick from running to
    completion -- the previous sequential for-loop's shared try/except
    would have skipped every camera after the failing one."""
    lru._active_cameras.update({1: "cam-broken", 2: "cam-ok-a", 3: "cam-ok-b"})
    completed = []

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        if camera_number == 1:
            raise RuntimeError("simulated upload failure")
        completed.append(camera_number)

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        with caplog.at_level("WARNING", logger="anyaicam.live_relay_uploader"):
            await lru._relay_tick(semaphore, "/hls")

    _run(scenario())
    assert sorted(completed) == [2, 3]


def test_failing_cameras_log_line_identifies_camera_number_and_camera_id(monkeypatch, caplog):
    lru._active_cameras.update({7: "cam-broken-id"})

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        with caplog.at_level("WARNING", logger="anyaicam.live_relay_uploader"):
            await lru._relay_tick(semaphore, "/hls")

    _run(scenario())
    messages = [r.getMessage() for r in caplog.records]
    assert any("camera_number=7" in m and "cam-broken-id" in m for m in messages)


# ------------------------------------------------------------- cancellation / shutdown


def test_a_cancelled_camera_task_propagates_cancellation_not_a_warning(monkeypatch, caplog):
    """A CancelledError surfacing from one camera's task must never be
    logged as a mere warning and swallowed -- it must propagate out of
    _relay_tick() so the worker's own outer `except asyncio.CancelledError:
    raise` still fires and shutdown stays clean."""
    lru._active_cameras.update({1: "cam-a", 2: "cam-b"})

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        if camera_number == 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        with caplog.at_level("WARNING", logger="anyaicam.live_relay_uploader"):
            await lru._relay_tick(semaphore, "/hls")

    with pytest.raises(asyncio.CancelledError):
        _run(scenario())
    assert not any("camera_iteration_failed" in r.getMessage() for r in caplog.records)


def test_cancelling_the_worker_task_while_ticks_are_in_flight_raises_cleanly(monkeypatch):
    """Simulates real shutdown: live_relay_worker() itself gets cancelled
    while a tick's camera tasks are still running. Must raise
    CancelledError cleanly with no unhandled task exceptions."""
    lru._active_cameras.update({n: f"cam-{n}" for n in range(1, 4)})

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        time.sleep(0.3)

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)
    monkeypatch.setattr(lru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(lru, "LIVE_RELAY_ENABLED", True)

    async def scenario():
        task = asyncio.create_task(lru.live_relay_worker("/hls"))
        await asyncio.sleep(0.05)  # let the worker start its first tick
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())  # no unhandled-exception warnings should print during this


# ------------------------------------------------------------- credential caching / duplicate prevention


def test_credential_session_caching_is_independent_per_camera_under_concurrency(monkeypatch):
    """Two cameras running concurrently in the same tick must never
    cross-contaminate each other's cached STS session -- each keeps its
    own entry in _sessions, exactly as before this change."""
    lru._active_cameras.update({1: "cam-a", 2: "cam-b"})
    ensure_calls = []

    def fake_ensure_session(camera_number, camera_id):
        ensure_calls.append(camera_number)
        session = {"credentials": {}, "bucket": "b", "key_prefix": f"live/{camera_id}/", "expires_at": "z"}
        lru._sessions[camera_number] = session
        return session

    def fake_relay_camera_once(hls_folder, camera_number, camera_id):
        fake_ensure_session(camera_number, camera_id)

    monkeypatch.setattr(lru, "_relay_camera_once", fake_relay_camera_once)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        await lru._relay_tick(semaphore, "/hls")

    _run(scenario())
    assert sorted(ensure_calls) == [1, 2]
    assert lru._sessions[1]["key_prefix"] == "live/cam-a/"
    assert lru._sessions[2]["key_prefix"] == "live/cam-b/"


def test_a_segment_is_never_uploaded_twice_across_two_concurrent_ticks(tmp_path, monkeypatch):
    """Two cameras, two consecutive ticks, simulating a slow-rotating
    segment still present on the second tick -- each camera's own
    segment must be uploaded exactly once total, using the real
    _pending_segments()/_remember_uploaded() bookkeeping, now reached
    through the concurrent harness instead of the old serial loop."""
    hls_folder = tmp_path
    (hls_folder / "camera1_000001.ts").write_bytes(b"data1")
    (hls_folder / "camera1.m3u8").write_text("#EXTM3U\ncamera1_000001.ts\n")
    (hls_folder / "camera2_000001.ts").write_bytes(b"data2")
    (hls_folder / "camera2.m3u8").write_text("#EXTM3U\ncamera2_000001.ts\n")

    lru._active_cameras.update({1: "cam-a", 2: "cam-b"})
    uploaded = []

    def fake_ensure_session(camera_number, camera_id):
        return {"credentials": {}, "bucket": "b", "key_prefix": f"live/{camera_id}/", "expires_at": "z"}

    def fake_upload_segment(session, local_path):
        uploaded.append(local_path.name)
        return session["key_prefix"] + local_path.name

    def fake_control_plane_request(path, payload):
        return {}

    monkeypatch.setattr(lru, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr(lru, "_upload_segment", fake_upload_segment)
    monkeypatch.setattr(lru, "_control_plane_request", fake_control_plane_request)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        await lru._relay_tick(semaphore, hls_folder)  # tick 1
        await lru._relay_tick(semaphore, hls_folder)  # tick 2 -- same files still present

    _run(scenario())
    assert sorted(uploaded) == ["camera1_000001.ts", "camera2_000001.ts"]


# ------------------------------------------------------------- ordering within one camera, through the new harness


def test_ordering_within_one_camera_survives_the_new_harness(tmp_path, monkeypatch):
    """The real _relay_camera_once, reached through _relay_camera_bounded/
    _relay_tick for the first time -- proves the pre-existing per-camera
    sequential upload order (by ascending sequence number) is completely
    unaffected by the new across-camera concurrency."""
    hls_folder = tmp_path
    names = [f"camera1_{i:06d}.ts" for i in range(1, 4)]
    for name in names:
        (hls_folder / name).write_bytes(b"x")
    (hls_folder / "camera1.m3u8").write_text("#EXTM3U\n" + "\n".join(names) + "\n")

    lru._active_cameras.update({1: "cam-a"})
    notified_sequences = []

    def fake_ensure_session(camera_number, camera_id):
        return {"credentials": {}, "bucket": "b", "key_prefix": "live/cam-a/", "expires_at": "z"}

    def fake_upload_segment(session, local_path):
        return session["key_prefix"] + local_path.name

    def fake_control_plane_request(path, payload):
        if path.endswith("/segment-available"):
            notified_sequences.append(payload["sequence"])
        return {}

    monkeypatch.setattr(lru, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr(lru, "_upload_segment", fake_upload_segment)
    monkeypatch.setattr(lru, "_control_plane_request", fake_control_plane_request)

    async def scenario():
        semaphore = asyncio.Semaphore(8)
        await lru._relay_tick(semaphore, hls_folder)

    _run(scenario())
    assert notified_sequences == sorted(notified_sequences)
    assert notified_sequences == [0, 1, 2]
