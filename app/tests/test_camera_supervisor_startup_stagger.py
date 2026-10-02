"""Regression coverage for the real anyaicam-vms restart-loop root cause
found live on Ryzen (2026-09-22), traced end to end with the user
directly on the appliance:

A host-level watchdog (anyaicam-healthcheck-restart.timer, outside this
codebase) restarts the anyaicam-vms container whenever Docker's own
/health HEALTHCHECK reports unhealthy. /health itself is a trivial,
synchronous, static JSON response with no work of its own -- confirmed
by direct code inspection. The real cause: lifespan()'s own startup loop
creates one "live" and one "recording" asyncio.create_task(process_
supervisor(...)) per camera slot back-to-back, with no gap at all, and
process_supervisor()'s very first action is to spawn a real FFmpeg
subprocess. On a 5-camera appliance that is ~10 FFmpeg processes
launched in the same instant on every container start -- confirmed live:
load average briefly exceeded 20 on an 8-core box immediately after
every restart, long enough that even the trivial /health probe couldn't
get scheduled within its 5s timeout, 3 checks in a row, which the
watchdog then reacted to by restarting the container -- recreating the
exact same thundering herd and repeating indefinitely (observed cadence:
every 6-13 minutes, matching the watchdog's own 300s cooldown plus
however long this specific box takes to re-saturate).

ai_person_detector() already staggers its own startup
(AI_DETECTOR_STARTUP_STAGGER_SECONDS) for exactly this reason --
process_supervisor() had no equivalent at all. Fixed by adding
CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS, applied once before process_
supervisor()'s own reconnect loop begins, indexed by (camera_number,
mode) so a single camera's own "live" and "recording" tasks are also
spread apart from each other, not just from other cameras' tasks.
"""
import asyncio

import main


async def _run_until_stagger_sleep(camera_number, mode, monkeypatch):
    """Runs process_supervisor() only up through its own startup-stagger
    sleep, then stops it there -- raising CancelledError from the fake
    asyncio.sleep() the moment it's first called, before the coroutine
    ever reaches the real reconnect loop / starter functions. Returns the
    list of sleep() calls actually made (should be exactly one: the
    stagger itself)."""
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    try:
        await main.process_supervisor(camera_number, mode)
    except asyncio.CancelledError:
        pass
    return sleep_calls


def test_each_camera_and_mode_gets_a_distinct_stagger_offset(monkeypatch):
    """The real bug this closes: without this fix, ALL of these
    combinations would spawn their first FFmpeg process in the very same
    instant. Every (camera_number, mode) pair below must get its own,
    strictly increasing stagger delay."""
    seen_offsets = []
    for camera_number in range(1, 4):
        for mode in ("live", "recording"):
            calls = asyncio.run(_run_until_stagger_sleep(camera_number, mode, monkeypatch))
            assert calls, f"camera {camera_number} mode={mode}: expected a startup stagger sleep"
            seen_offsets.append(calls[0])

    assert len(set(seen_offsets)) == len(seen_offsets), (
        f"expected every (camera, mode) combination to get a distinct stagger offset, got {seen_offsets}"
    )
    assert seen_offsets == sorted(seen_offsets), "offsets must be strictly increasing, not lockstep"


def test_a_single_cameras_live_and_recording_tasks_are_not_simultaneous(monkeypatch):
    """The specific gap a camera-number-only stagger would still miss:
    camera 1's own "live" and "recording" tasks must not collide with
    EACH OTHER either, not just with other cameras' tasks."""
    live_calls = asyncio.run(_run_until_stagger_sleep(1, "live", monkeypatch))
    recording_calls = asyncio.run(_run_until_stagger_sleep(1, "recording", monkeypatch))

    assert live_calls and recording_calls
    assert live_calls[0] != recording_calls[0], (
        "camera 1's own live and recording supervisor tasks must not share the same stagger offset"
    )


def test_stagger_offsets_scale_with_the_configured_interval(monkeypatch):
    monkeypatch.setattr(main, "CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS", 2.5)

    live_calls = asyncio.run(_run_until_stagger_sleep(1, "live", monkeypatch))
    recording_calls = asyncio.run(_run_until_stagger_sleep(1, "recording", monkeypatch))
    camera2_live_calls = asyncio.run(_run_until_stagger_sleep(2, "live", monkeypatch))

    assert live_calls[0] == 0.0
    assert recording_calls[0] == 2.5
    assert camera2_live_calls[0] == 5.0


def test_stagger_disabled_entirely_skips_the_sleep_call(monkeypatch):
    """CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS=0 (an explicit opt-out)
    must not insert a real, unnecessary asyncio.sleep(0) call at all --
    matching AI_DETECTOR_STARTUP_STAGGER_SECONDS's own established
    all-or-nothing gate shape."""
    monkeypatch.setattr(main, "CAMERA_SUPERVISOR_STARTUP_STAGGER_SECONDS", 0)

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    # With the stagger disabled, process_supervisor() proceeds straight
    # into its real reconnect loop -- give it an unconfigured camera
    # slot so it takes the cheap, already-existing CameraNotConfiguredError
    # branch (which itself awaits asyncio.sleep(CAMERA_NOT_CONFIGURED_POLL_SECONDS)),
    # proving the ONLY sleep encountered is that pre-existing one, not a
    # stagger sleep in front of it.
    monkeypatch.setattr(
        main, "start_live_stream",
        lambda camera_number: (_ for _ in ()).throw(main.CameraNotConfiguredError()),
    )
    monkeypatch.setattr(main, "camera_process_state", {42: {}})

    try:
        asyncio.run(main.process_supervisor(42, "live"))
    except asyncio.CancelledError:
        pass

    assert sleep_calls == [main.CAMERA_NOT_CONFIGURED_POLL_SECONDS], (
        f"expected only the pre-existing not-configured poll sleep, got {sleep_calls}"
    )
