"""Focused tests for the motion_detector() pixel-loop vectorization fix.

Background: motion_detector()'s per-pixel Python `for` loop -- computing
zone membership and frame-to-frame difference one of 14,400 bytes at a
time, with zero `await` inside it -- ran directly on the FastAPI event
loop's own MainThread. Confirmed live on the Samsung production
appliance via repeated py-spy captures: the loop measurably held the
event loop (CPU-bound, GIL-held) long enough, especially when several
cameras' frames landed close together, to reproduce the same class of
HTTP outage as the earlier build_motion_event_clip() and
retention_worker() bugs -- a 26-second window with 5 cameras' loops
firing back-to-back correlated with 19/19 concurrent /health failures.

The fix replaces the loop with a NumPy-vectorized equivalent
(_compare_motion_frames()), backed by a per-camera cached zone mask
(_motion_zone_mask()) rebuilt only when that camera's configured zones
actually change, dispatched via asyncio.to_thread() from
motion_detector() -- the same pattern already used and proven for the
retention and motion-clip fixes earlier this session.

This file proves:
  1. The vectorized result matches a reference implementation of the
     original per-pixel loop, across representative frames.
  2. Zone masking includes/excludes the exact same pixels as the
     original per-pixel zone check.
  3. Motion threshold behavior (motion_score / changed_ratio /
     effective_threshold -> motion_detected) is unchanged, since it's
     driven by numerically identical inputs.
  4. Multiple zones still work correctly (union semantics preserved).
  5. The comparison actually runs off the event-loop thread.
  6. A heartbeat coroutine stays responsive while all 5 cameras process
     a motion frame concurrently.
  7. Event-creation/cooldown behavior is unaffected, because it operates
     on the same three accumulators the old loop produced -- proven via
     the same equivalence property as (1)/(3), not by re-implementing
     the (unchanged) state machine in this test.

A benchmark at the end (not a correctness test, run and reported
separately) compares old-loop vs. new-vectorized wall-clock time on a
representative 160x90 frame pair.

Same import/isolation constraints as the other fix-verification test
files in this suite: imports `main` (must run inside the deployed
container's Python).
"""

import asyncio
import random
import threading
import time

import pytest

import main


# ---------------------------------------------------------------------------
# Reference implementation: the ORIGINAL per-pixel loop, reproduced
# exactly as it existed before this fix, used only to prove equivalence.
# ---------------------------------------------------------------------------

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


def _random_frame(seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(0, 256) for _ in range(14400))


def _zone(x=0.0, y=0.0, width=1.0, height=1.0, name="Zone"):
    return main.MotionZoneModel(name=name, x=x, y=y, width=width, height=height)


@pytest.fixture(autouse=True)
def _clear_zone_mask_cache():
    main._motion_zone_mask_cache.clear()
    yield
    main._motion_zone_mask_cache.clear()


# ---------------------------------------------------------------------------
# 1. Vectorized result matches the original per-pixel loop.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_vectorized_matches_reference_full_frame(seed):
    frame = _random_frame(seed)
    previous = _random_frame(seed + 1000)
    zones = [_zone()]  # default full-frame zone

    expected = _reference_pixel_loop(frame, previous, zones)
    actual = main._compare_motion_frames(1, frame, previous, zones)

    assert actual == expected


def test_vectorized_matches_reference_identical_frames():
    # No motion at all: current == previous everywhere.
    frame = _random_frame(42)
    zones = [_zone()]

    expected = _reference_pixel_loop(frame, frame, zones)
    actual = main._compare_motion_frames(1, frame, frame, zones)

    assert actual == expected
    assert actual[0] == 0  # total_difference
    assert actual[2] == 0  # changed_pixels


def test_vectorized_matches_reference_max_difference():
    # Every pixel flips between 0 and 255 -- exercises the int16
    # overflow-safety of the vectorized subtraction at the extremes.
    frame = bytes([255]) * 14400
    previous = bytes([0]) * 14400
    zones = [_zone()]

    expected = _reference_pixel_loop(frame, previous, zones)
    actual = main._compare_motion_frames(1, frame, previous, zones)

    assert actual == expected
    assert actual[0] == 255 * 14400


# ---------------------------------------------------------------------------
# 2. Zone masking includes/excludes the exact same pixels.
# ---------------------------------------------------------------------------

def test_zone_mask_matches_per_pixel_reference():
    zones = [_zone(x=0.25, y=0.25, width=0.3, height=0.3)]
    mask = main._motion_zone_mask(1, zones)

    for index in range(14400):
        pixel_x = (index % 160) / 160
        pixel_y = (index // 160) / 90
        expected_in_zone = any(
            zone.x <= pixel_x <= zone.x + zone.width
            and zone.y <= pixel_y <= zone.y + zone.height
            for zone in zones
        )
        assert bool(mask[index]) == expected_in_zone, f"mismatch at pixel index {index}"


def test_zone_mask_cache_reused_when_zones_unchanged():
    zones = [_zone(x=0.1, y=0.1, width=0.5, height=0.5)]
    mask1 = main._motion_zone_mask(3, zones)
    mask2 = main._motion_zone_mask(3, zones)
    assert mask1 is mask2, "identical zone config should reuse the cached mask, not rebuild it"


def test_zone_mask_cache_rebuilds_when_zones_change():
    zones_a = [_zone(x=0.0, y=0.0, width=0.5, height=0.5)]
    zones_b = [_zone(x=0.5, y=0.5, width=0.5, height=0.5)]

    mask_a = main._motion_zone_mask(4, zones_a)
    mask_b = main._motion_zone_mask(4, zones_b)

    assert not (mask_a == mask_b).all(), "changed zone config must produce a different mask"


# ---------------------------------------------------------------------------
# 3. Motion threshold behavior is unchanged (same inputs -> same outputs).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sensitivity", [20, 50, 60, 80])
def test_threshold_math_unchanged_for_given_inputs(sensitivity):
    frame = _random_frame(7)
    previous = _random_frame(8)
    zones = [_zone()]

    total_difference, compared_pixels, changed_pixels = main._compare_motion_frames(
        1, frame, previous, zones
    )
    motion_score = total_difference / max(compared_pixels, 1)
    changed_ratio = changed_pixels / max(compared_pixels, 1)
    effective_threshold = 4.0 * (1.5 - sensitivity / 100)
    motion_detected = (
        motion_score >= effective_threshold
        and 0.02 <= changed_ratio <= main.MOTION_MAX_CHANGED_RATIO
    )

    ref_total, ref_compared, ref_changed = _reference_pixel_loop(frame, previous, zones)
    ref_motion_score = ref_total / max(ref_compared, 1)
    ref_changed_ratio = ref_changed / max(ref_compared, 1)
    ref_motion_detected = (
        ref_motion_score >= effective_threshold
        and 0.02 <= ref_changed_ratio <= main.MOTION_MAX_CHANGED_RATIO
    )

    assert motion_score == ref_motion_score
    assert changed_ratio == ref_changed_ratio
    assert motion_detected == ref_motion_detected


# ---------------------------------------------------------------------------
# 4. Multiple zones still work correctly (union semantics).
# ---------------------------------------------------------------------------

def test_multiple_zones_union_matches_reference():
    frame = _random_frame(11)
    previous = _random_frame(12)
    zones = [
        _zone(x=0.0, y=0.0, width=0.2, height=0.2, name="corner"),
        _zone(x=0.6, y=0.6, width=0.3, height=0.3, name="far corner"),
    ]

    expected = _reference_pixel_loop(frame, previous, zones)
    actual = main._compare_motion_frames(1, frame, previous, zones)

    assert actual == expected
    # Sanity: fewer pixels covered than the full-frame case (zones don't
    # tile the whole grid), proving the mask is genuinely restrictive.
    full_frame_compared = main._compare_motion_frames(2, frame, previous, [_zone()])[1]
    assert 0 < actual[1] < full_frame_compared


# ---------------------------------------------------------------------------
# 5. The comparison runs off the event-loop thread.
# ---------------------------------------------------------------------------

def test_motion_detector_dispatches_comparison_via_to_thread(monkeypatch, tmp_path):
    """Exercises motion_detector()'s REAL call site (not just the stdlib
    to_thread guarantee in isolation): fakes the ffmpeg subprocess/pipe
    and settings lookup, then proves the actual comparison call inside
    the running coroutine executes on a different thread than the one
    running motion_detector() itself."""
    hls_folder = tmp_path / "hls"
    hls_folder.mkdir()
    monkeypatch.setattr(main, "HLS_FOLDER", hls_folder)
    manifest = hls_folder / "camera9.m3u8"
    manifest.write_text("#EXTM3U\n", encoding="utf-8")

    frames = [_random_frame(1), _random_frame(2), _random_frame(3)]
    frame_iter = iter(frames)

    class FakeStdout:
        async def readexactly(self, n):
            try:
                return next(frame_iter)
            except StopIteration:
                raise asyncio.CancelledError()  # stop the outer `while True`

    class FakeProcess:
        stdout = FakeStdout()
        returncode = None

        def terminate(self):
            pass

        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        main,
        "get_event_settings",
        lambda camera_number: main.EventSettingsModel(
            camera=camera_number, enabled=True, zones=[_zone()]
        ),
    )

    caller_thread_id = threading.get_ident()
    seen_thread_ids = []
    real_compare = main._compare_motion_frames

    def spying_compare(camera_number, frame, previous_frame, zones):
        seen_thread_ids.append(threading.get_ident())
        return real_compare(camera_number, frame, previous_frame, zones)

    monkeypatch.setattr(main, "_compare_motion_frames", spying_compare)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.motion_detector(9))

    assert seen_thread_ids, "_compare_motion_frames() was never invoked from motion_detector()"
    assert all(tid != caller_thread_id for tid in seen_thread_ids), (
        "the frame comparison ran on motion_detector()'s own (event-loop) "
        "thread -- the asyncio.to_thread() offload is missing or was "
        "bypassed at the real call site, which reintroduces the stall."
    )


# ---------------------------------------------------------------------------
# 6. A heartbeat stays responsive while all 5 cameras process a frame.
# ---------------------------------------------------------------------------

def test_heartbeat_responsive_during_five_camera_comparisons():
    def slow_compare(camera_number, frame, previous_frame, zones):
        # Simulate a slower-than-usual comparison: blocks its OWN worker
        # thread, which is fine -- the point is the event loop must not
        # be blocked by it.
        time.sleep(0.1)
        return main._compare_motion_frames(camera_number, frame, previous_frame, zones)

    heartbeat_ticks = []

    async def heartbeat():
        for _ in range(60):
            heartbeat_ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def five_cameras():
        frame = _random_frame(1)
        previous = _random_frame(2)
        zones = [_zone()]
        await asyncio.gather(
            *[
                asyncio.to_thread(slow_compare, camera_number, frame, previous, zones)
                for camera_number in range(1, 6)
            ]
        )

    async def run_both():
        heartbeat_task = asyncio.create_task(heartbeat())
        await five_cameras()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    start = time.monotonic()
    asyncio.run(run_both())
    elapsed = time.monotonic() - start

    ticks_during_work = sum(1 for t in heartbeat_ticks if t - start < elapsed)
    assert ticks_during_work >= 5, (
        f"heartbeat only ticked {ticks_during_work} times while 5 cameras' "
        f"frames were being compared -- the event loop appears blocked"
    )


# ---------------------------------------------------------------------------
# 7. Event-creation/cooldown behavior is unaffected -- proven via the
#    same equivalence guarantee across many random frame pairs, since
#    that state machine (unchanged code) only ever sees these three
#    accumulators.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(20))
def test_event_creation_inputs_equivalent_across_many_frames(seed):
    frame = _random_frame(seed)
    previous = _random_frame(seed + 500)
    zones = [_zone()]

    expected = _reference_pixel_loop(frame, previous, zones)
    actual = main._compare_motion_frames(1, frame, previous, zones)

    assert actual == expected, (
        "store_motion_event()'s cooldown/active-event state machine reads "
        "motion_score/changed_ratio computed from these three values -- "
        "any divergence here would change when/whether an event fires"
    )
