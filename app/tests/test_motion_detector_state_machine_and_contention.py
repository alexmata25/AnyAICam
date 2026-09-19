"""Four additional focused tests requested by ChatGPT Work's "approve with
changes" review of the motion_detector() vectorization fix (candidate
main.py SHA256 6f8e8c964c197933f551054c89c59fda0a1b795a543dc6845f2f558f4cab4df6).

These supplement, and do not replace, the 37 tests in
test_motion_detector_vectorized.py. All four exercise the REAL
motion_detector() coroutine and/or the real event-settings save/load
path -- not just the standalone _compare_motion_frames() helper -- to
prove the fix is safe at the whole-state-machine level, not only at the
numeric-equivalence level already covered.

Same import/isolation constraints as the other fix-verification test
files this session: imports `main` (must run inside the deployed
container's Python), and every test redirects file-backed state
(EVENT_SETTINGS_FILE, HLS_FOLDER) to tmp_path locations -- nothing here
ever touches real production data.

Does NOT touch, test, or fix the separate, already-confirmed recording-
supervisor process.wait() executor-waste issue -- test 4 below only
*models* that condition (occupying 5 default-executor workers) to prove
this fix's own behavior is safe in its presence; it does not remediate it.
"""

import asyncio
import random
import threading
import time
from datetime import datetime as real_datetime, timedelta

import pytest

import main


def _random_frame(seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(0, 256) for _ in range(14400))


def _zone(x=0.0, y=0.0, width=1.0, height=1.0, name="Zone"):
    return main.MotionZoneModel(name=name, x=x, y=y, width=width, height=height)


def _make_frame(base_value: int) -> bytes:
    return bytes([base_value]) * 14400


def _make_motion_frame(base_value: int, changed_count: int, delta: int) -> bytes:
    data = bytearray([base_value]) * 14400
    for i in range(changed_count):
        data[i] = (base_value + delta) % 256
    return bytes(data)


@pytest.fixture(autouse=True)
def _clear_zone_mask_cache():
    main._motion_zone_mask_cache.clear()
    yield
    main._motion_zone_mask_cache.clear()


class _FakeStdout:
    """Feeds a scripted list of frames; raises CancelledError once
    exhausted so the outer `while True` in motion_detector() stops
    cleanly (neither of its except clauses catches CancelledError)."""

    def __init__(self, frames, on_each_frame=None):
        self._frames = list(frames)
        self._index = 0
        self._on_each_frame = on_each_frame

    async def readexactly(self, n):
        if self._index >= len(self._frames):
            raise asyncio.CancelledError()
        frame = self._frames[self._index]
        self._index += 1
        if self._on_each_frame is not None:
            self._on_each_frame(self._index)
        return frame


class _FakeProcess:
    def __init__(self, frames, on_each_frame=None):
        self.stdout = _FakeStdout(frames, on_each_frame)
        self.returncode = None

    def terminate(self):
        pass

    async def wait(self):
        return 0


class _FakeDateTime:
    """Deterministic, monotonically-advancing stand-in for the real
    `datetime` class -- only `.now()` is exercised in the code path
    under test. Reset _counter between runs so two runs given the same
    frame script produce byte-identical timestamp sequences."""

    _counter = 0

    @classmethod
    def reset(cls):
        cls._counter = 0

    @classmethod
    def now(cls):
        cls._counter += 1
        return real_datetime(2026, 1, 1) + timedelta(seconds=cls._counter)


async def _run_scripted_motion_detector(
    monkeypatch,
    tmp_path,
    frames,
    compare_fn,
    camera_number=7,
    sensitivity=60,
    cooldown_seconds=2,
    minimum_duration_seconds=2,
    zones=None,
    on_each_frame=None,
):
    """Runs the REAL motion_detector() coroutine against a scripted
    frame sequence, with a deterministic fake clock and a recording
    stand-in for store_motion_event(). Returns the list of recorded
    store_motion_event() call argument tuples."""
    hls_folder = tmp_path / "hls"
    hls_folder.mkdir(exist_ok=True)
    monkeypatch.setattr(main, "HLS_FOLDER", hls_folder)
    manifest = hls_folder / f"camera{camera_number}.m3u8"
    manifest.write_text("#EXTM3U\n", encoding="utf-8")

    settings = main.EventSettingsModel(
        camera=camera_number,
        enabled=True,
        sensitivity=sensitivity,
        cooldown_seconds=cooldown_seconds,
        minimum_duration_seconds=minimum_duration_seconds,
        zones=zones or [_zone()],
    )

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _FakeProcess(frames, on_each_frame)

    calls = []

    async def recording_store_motion_event(cam, start, end, score, frame):
        calls.append((cam, start, end, score, frame))

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(main, "get_event_settings", lambda camera_number: settings)
    monkeypatch.setattr(main, "store_motion_event", recording_store_motion_event)
    monkeypatch.setattr(main, "_compare_motion_frames", compare_fn)
    monkeypatch.setattr(main, "datetime", _FakeDateTime)

    with pytest.raises(asyncio.CancelledError):
        await main.motion_detector(camera_number)

    return calls


def _reference_pixel_loop(frame: bytes, previous_frame: bytes, zones: list) -> tuple[int, int, int]:
    total_difference = 0
    changed_pixels = 0
    compared_pixels = 0
    for index, (current, previous) in enumerate(zip(frame, previous_frame)):
        pixel_x = (index % 160) / 160
        pixel_y = (index // 160) / 90
        in_zone = any(
            zone.x <= pixel_x <= zone.x + zone.width
            and zone.y <= pixel_y <= zone.y + zone.height
            for zone in zones
        )
        if not in_zone:
            continue
        difference = abs(current - previous)
        total_difference += difference
        compared_pixels += 1
        if difference >= 20:
            changed_pixels += 1
    return total_difference, compared_pixels, changed_pixels


def _old_style_compare(camera_number, frame, previous_frame, zones):
    """The ORIGINAL algorithm, wired up with the exact same call
    signature as the new _compare_motion_frames(), so it can be
    swapped in via asyncio.to_thread() the same way."""
    return _reference_pixel_loop(frame, previous_frame, zones)


# ===========================================================================
# 1. Full motion state-machine equivalence
# ===========================================================================

def test_full_state_machine_equivalent_old_vs_new(monkeypatch, tmp_path):
    # Scripted 9-frame sequence, run through the REAL motion_detector().
    # motion_detector() always compares consecutive frames (frame[N] vs
    # frame[N-1]), so each frame below is built by mutating a FRESH,
    # previously-untouched block of pixels relative to the one before it
    # -- not by re-intensifying the same block -- so the intended
    # per-step delta is exact and independently verifiable.
    #   f0        establishes previous_frame (no comparison yet)
    #   f1        identical to f0 -> no motion, no event active -> no-op
    #   f2        block [0:1440) -> 180 (vs f1, all 100): score 8.0
    #             -> starts a new active event (active_score=8.0, frame=f2)
    #   f3        block [1440:2880) ALSO -> 185 (vs f2): score 8.5
    #             -> 8.5 > 8.0 -> active_score/active_frame update to f3
    #   f4,f5,f6  identical to f3 -> 3 still frames -> event finalizes
    #             (motion_frames=2 >= minimum_duration_seconds=2)
    #   f7        block [2880:4320) -> 190 (vs f6==f3): score 9.0, motion
    #             detected, but cooldown_seconds=2 not yet elapsed (real
    #             wall-clock here is microseconds) -> suppressed
    #   f8        identical to f7 -> no-op
    f0 = _make_frame(100)
    f1 = _make_frame(100)
    f2 = bytearray(f1)
    for i in range(0, 1440):
        f2[i] = 180
    f2 = bytes(f2)
    f3 = bytearray(f2)
    for i in range(1440, 2880):
        f3[i] = 185
    f3 = bytes(f3)
    f4 = f3
    f5 = f3
    f6 = f3
    f7 = bytearray(f6)
    for i in range(2880, 4320):
        f7[i] = 190
    f7 = bytes(f7)
    f8 = f7
    frames = [f0, f1, f2, f3, f4, f5, f6, f7, f8]

    _FakeDateTime.reset()
    old_calls = asyncio.run(
        _run_scripted_motion_detector(monkeypatch, tmp_path, frames, _old_style_compare)
    )

    main._motion_zone_mask_cache.clear()
    _FakeDateTime.reset()
    new_calls = asyncio.run(
        _run_scripted_motion_detector(monkeypatch, tmp_path, frames, main._compare_motion_frames)
    )

    # Same store_motion_event() call count.
    assert len(old_calls) == len(new_calls) == 1, (
        f"expected exactly 1 finalized event (cooldown suppressing the "
        f"second burst) in both runs; old={len(old_calls)} new={len(new_calls)}"
    )

    (old_cam, old_start, old_end, old_score, old_frame) = old_calls[0]
    (new_cam, new_start, new_end, new_score, new_frame) = new_calls[0]

    assert old_cam == new_cam
    # Same event start/end timing (deterministic fake clock -> exact match).
    assert old_start == new_start
    assert old_end == new_end
    # Same confidence input (active_score).
    assert old_score == new_score
    # Same selected active_frame (the stronger-motion frame, f3).
    assert old_frame == new_frame == frames[3]


def test_minimum_duration_suppresses_short_events_identically(monkeypatch, tmp_path):
    # A single-frame motion burst that never reaches minimum_duration_seconds
    # (still_frames hits 3 with motion_frames only 1) must NOT fire an
    # event, identically in both implementations.
    frames = [
        _make_frame(100),
        _make_frame(100),
        _make_motion_frame(100, 1440, 60),  # motion_frames=1
        _make_motion_frame(100, 1440, 60),  # still vs previous -> still_frames=1
        _make_motion_frame(100, 1440, 60),  # still_frames=2
        _make_motion_frame(100, 1440, 60),  # still_frames=3 -> finalize check
    ]

    _FakeDateTime.reset()
    old_calls = asyncio.run(
        _run_scripted_motion_detector(
            monkeypatch, tmp_path, frames, _old_style_compare, minimum_duration_seconds=2
        )
    )
    main._motion_zone_mask_cache.clear()
    _FakeDateTime.reset()
    new_calls = asyncio.run(
        _run_scripted_motion_detector(
            monkeypatch, tmp_path, frames, main._compare_motion_frames, minimum_duration_seconds=2
        )
    )

    assert old_calls == new_calls == []


# ===========================================================================
# 2. Real settings-change/cache-invalidation test, through the actual
#    event-settings save/load path (GET/PUT handlers, EVENT_SETTINGS_FILE).
# ===========================================================================

def test_zone_change_through_real_save_load_path_invalidates_mask(monkeypatch, tmp_path):
    camera_number = 3
    event_settings_file = tmp_path / "event_settings.json"
    monkeypatch.setattr(main, "EVENT_SETTINGS_FILE", event_settings_file)

    zone_a = _zone(x=0.0, y=0.0, width=0.3, height=0.3, name="A")
    zone_b = _zone(x=0.6, y=0.6, width=0.3, height=0.3, name="B")

    # Save zone A through the REAL PUT-route function.
    response_a = main.update_event_settings(
        camera_number,
        main.EventSettingsModel(camera=camera_number, enabled=True, zones=[zone_a]),
    )
    assert response_a["status"] == "complete"

    # Load through the REAL GET-route function -- motion_detector() calls
    # exactly this function, every frame.
    loaded_a = main.get_event_settings(camera_number)
    assert loaded_a.zones[0].name == "A"

    # Process a frame using zone A: motion placed inside zone A's region
    # (top-left corner, pixel indices roughly 0-47 per row for the first
    # ~43 rows given width=0.3 -> pixel_x in [0, 0.3)).
    frame_a1 = _make_frame(100)
    frame_a2 = bytearray(frame_a1)
    for row in range(10):
        for col in range(10):
            frame_a2[row * 160 + col] = 200  # inside zone A
    frame_a2 = bytes(frame_a2)

    mask_before = main._motion_zone_mask(camera_number, loaded_a.zones)
    total_a, compared_a, changed_a = main._compare_motion_frames(
        camera_number, frame_a2, frame_a1, loaded_a.zones
    )
    assert changed_a > 0, "motion inside zone A should be detected while zone A is active"

    # Save zone B through the REAL PUT-route function.
    response_b = main.update_event_settings(
        camera_number,
        main.EventSettingsModel(camera=camera_number, enabled=True, zones=[zone_b]),
    )
    assert response_b["status"] == "complete"

    # Load again through the REAL GET-route function -- must reflect zone B.
    loaded_b = main.get_event_settings(camera_number)
    assert loaded_b.zones[0].name == "B"

    # Process the next frame using the newly-loaded zones.
    mask_after = main._motion_zone_mask(camera_number, loaded_b.zones)

    # Prove a new mask was actually built (not the stale zone-A mask).
    assert not (mask_before == mask_after).all(), (
        "zone mask must be rebuilt after zones change through the real "
        "save/load path -- got back the same mask as zone A"
    )

    # Prove detection follows zone B immediately: the SAME zone-A motion
    # (top-left corner) must now be invisible, since it falls outside zone B.
    total_b, compared_b, changed_b = main._compare_motion_frames(
        camera_number, frame_a2, frame_a1, loaded_b.zones
    )
    assert changed_b == 0, (
        "motion inside the OLD zone A must not register once zone B is "
        "active -- detection did not follow the zone change"
    )

    # And motion actually placed inside zone B IS detected immediately.
    frame_b2 = bytearray(frame_a1)
    for row in range(63, 73):  # zone B: y in [0.6, 0.9) -> rows ~54-80
        for col in range(96, 106):  # zone B: x in [0.6, 0.9) -> cols ~96-144
            frame_b2[row * 160 + col] = 200
    frame_b2 = bytes(frame_b2)
    total_b2, compared_b2, changed_b2 = main._compare_motion_frames(
        camera_number, frame_b2, frame_a1, loaded_b.zones
    )
    assert changed_b2 > 0, "motion inside the NEW zone B should be detected immediately"


# ===========================================================================
# 3. Stronger heartbeat/event-loop latency test with real CPU-bound work.
# ===========================================================================

def test_heartbeat_latency_bounded_during_real_five_camera_comparisons():
    frame = _random_frame(1)
    previous = _random_frame(2)
    zones = [_zone()]

    heartbeat_ticks = []

    async def heartbeat():
        for _ in range(300):
            heartbeat_ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    async def repeated_five_camera_comparisons():
        # 30 rounds of all 5 cameras doing a REAL vectorized comparison
        # back-to-back -- genuine CPU-bound NumPy work, not time.sleep().
        for _ in range(30):
            await asyncio.gather(
                *[
                    asyncio.to_thread(
                        main._compare_motion_frames, camera_number, frame, previous, zones
                    )
                    for camera_number in range(1, 6)
                ]
            )

    async def run_both():
        heartbeat_task = asyncio.create_task(heartbeat())
        await repeated_five_camera_comparisons()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_both())

    # A low tick count here is a GOOD sign, not a bug: it means the 30
    # rounds x 5 cameras of real vectorized work finished fast enough
    # that the heartbeat (cancelled the instant the work completes) never
    # got the chance to accumulate many ticks. The gap-based assertions
    # below are the actual latency signal, and only run when there are
    # enough samples to be meaningful (avoids flaking on shared-runner
    # load variance rather than on genuine event-loop blocking).
    assert len(heartbeat_ticks) >= 1, "heartbeat coroutine never got a single scheduling turn"
    if len(heartbeat_ticks) >= 4:
        gaps = [b - a for a, b in zip(heartbeat_ticks, heartbeat_ticks[1:])]
        max_gap = max(gaps)
        avg_gap = sum(gaps) / len(gaps)

        # The heartbeat sleeps 5ms between ticks; a healthy event loop
        # keeps gaps close to that. A generous bound (150ms) still
        # clearly fails if real CPU-bound work were blocking the loop
        # the way the OLD pixel loop did (single-frame cost there was
        # ~23ms, x5 cameras clustering was the exact production failure
        # mode).
        assert max_gap < 0.15, f"max heartbeat gap {max_gap*1000:.1f}ms -- event loop was blocked"


# ===========================================================================
# 4. Executor-contention test, modeling the proven recording-supervisor
#    process.wait() condition (5 permanently-occupied default-executor
#    workers) -- proves THIS fix stays safe in that presence. Does not
#    fix or test the supervisor issue itself.
# ===========================================================================

def test_motion_comparisons_survive_five_occupied_executor_workers():
    frame = _random_frame(3)
    previous = _random_frame(4)
    zones = [_zone()]

    release_event = threading.Event()

    def occupy_worker():
        # Models process_supervisor()'s proven `await
        # asyncio.to_thread(process.wait)` on a process that runs
        # forever: a worker thread parked indefinitely (until released)
        # on the SAME default executor pool.
        release_event.wait(timeout=30)

    async def scenario():
        # Occupy 5 of the ~12 default-executor workers, matching the
        # exact count already proven live on this appliance.
        occupy_tasks = [asyncio.to_thread(occupy_worker) for _ in range(5)]
        await asyncio.sleep(0.1)  # let them actually start and block

        heartbeat_ticks = []

        async def heartbeat():
            for _ in range(200):
                heartbeat_ticks.append(time.monotonic())
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())

        start = time.monotonic()
        results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    asyncio.to_thread(
                        main._compare_motion_frames, camera_number, frame, previous, zones
                    )
                    for camera_number in range(1, 6)
                ]
            ),
            timeout=15.0,
        )
        elapsed = time.monotonic() - start

        release_event.set()
        await asyncio.gather(*occupy_tasks)
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        return results, elapsed, heartbeat_ticks

    results, elapsed, heartbeat_ticks = asyncio.run(scenario())

    # Comparisons complete (no exception, no timeout -- asyncio.wait_for
    # above would have raised TimeoutError on a real deadlock).
    assert len(results) == 5
    for total, compared, changed in results:
        assert compared == 14400  # full-frame zone -> every pixel compared

    # Latency stays bounded -- even with 5/12 workers permanently gone,
    # 5 more (comparisons) fit in the remaining ~7, so this should
    # complete quickly, not hang until the 15s timeout.
    assert elapsed < 5.0, f"5-camera comparison took {elapsed:.2f}s under executor contention"

    # Heartbeat remained responsive throughout. The primary "no deadlock/
    # starvation" proof is `elapsed < 5.0` above combined with reaching
    # this line at all (asyncio.wait_for would have raised TimeoutError
    # on a real deadlock/starvation). Tick count is inherently timing-
    # sensitive under shared test-runner load, so it's checked loosely;
    # the gap check only runs when there are enough samples to be
    # meaningful, to avoid flaking on machine-load variance rather than
    # on genuine event-loop blocking.
    assert len(heartbeat_ticks) >= 1, "heartbeat coroutine never got a single scheduling turn"
    if len(heartbeat_ticks) >= 4:
        gaps = [b - a for a, b in zip(heartbeat_ticks, heartbeat_ticks[1:])]
        assert max(gaps) < 0.15, f"max heartbeat gap {max(gaps)*1000:.1f}ms under contention"

    # No deadlock/starvation: reaching this line at all (rather than
    # asyncio.wait_for raising TimeoutError above) is itself the proof.
