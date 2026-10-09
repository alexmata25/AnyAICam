"""A camera_number above the boot-time supervisor ceiling (2026-10-08, Orange).

A removed camera's number is never reused (customer camera Add/Delete), so
after replacements an appliance can run 4 cameras numbered 1, 2, 3 and 9.
Supervisor slots were 1..count+4 = 1..8: camera 9 was never supervised (no
live view or recording, even after a restart) and GET /ready answered 500
(KeyError: 9). Reproduced by Orange on an isolated 1.2.4 test appliance.
"""
import asyncio

import pytest

import main

AFTER_REPLACEMENTS = [1, 2, 3, 9]  # cameras 4..8 were replaced over time


@pytest.fixture()
def appliance(monkeypatch):
    """The process state as a VMS booted before camera 9 was provisioned."""
    monkeypatch.setattr(main, "get_camera_numbers", lambda customer_id=None: list(AFTER_REPLACEMENTS))
    boot = range(1, 9)
    monkeypatch.setattr(main, "camera_process_state", main._PerCameraState(main._new_camera_process_state, boot))
    monkeypatch.setattr(main, "camera_reconnect_counts", main._PerCameraState(int, boot))
    monkeypatch.setattr(main, "_supervised_camera_numbers", set(boot))
    return boot


def test_the_boot_ceiling_covers_the_highest_camera_number_not_only_the_count(appliance):
    assert main.get_supervisor_slot_count() == 9 + main.CAMERA_SUPERVISOR_HEADROOM
    assert main.get_supervisor_slot_count() > main.get_camera_count() + main.CAMERA_SUPERVISOR_HEADROOM


def test_a_restart_supervises_the_camera_above_the_old_ceiling(monkeypatch, appliance):
    monkeypatch.setattr(main, "_supervised_camera_numbers", set())
    claimed = main._claim_camera_supervisor_slots(range(1, main.get_supervisor_slot_count() + 1))
    assert 9 in claimed


def test_a_camera_provisioned_while_running_gets_its_supervisors_without_a_restart(monkeypatch, appliance):
    started = []

    async def fake_supervisor(camera_number, mode):
        started.append((camera_number, mode))
    monkeypatch.setattr(main, "process_supervisor", fake_supervisor)

    async def run():
        tasks = []
        assert main.start_camera_supervisors(main.get_camera_numbers(), tasks) == [9]
        assert main.start_camera_supervisors(main.get_camera_numbers(), tasks) == []  # once only
        await asyncio.gather(*tasks)
    asyncio.run(run())
    assert sorted(started) == [(9, "live"), (9, "recording")]
    assert main.camera_process_state[9]["live"] == "starting"


def test_the_watcher_starts_the_missing_supervisors(monkeypatch, appliance):
    started = []

    async def fake_supervisor(camera_number, mode):
        started.append((camera_number, mode))
    monkeypatch.setattr(main, "process_supervisor", fake_supervisor)
    monkeypatch.setattr(main, "CAMERA_NOT_CONFIGURED_POLL_SECONDS", 0)

    async def run():
        tasks = []
        watcher = asyncio.create_task(main.camera_supervisor_watcher(tasks))
        for _ in range(50):
            await asyncio.sleep(0)
            if len(started) == 2:
                break
        watcher.cancel()
        await asyncio.gather(watcher, *tasks, return_exceptions=True)
    asyncio.run(run())
    assert sorted(started) == [(9, "live"), (9, "recording")]


def test_camera_status_never_fails_on_a_camera_without_a_supervisor_entry(appliance):
    assert 9 not in main.camera_process_state
    status = main._legacy_camera_status()
    assert [camera["camera"] for camera in status["cameras"]] == AFTER_REPLACEMENTS


def test_ready_does_not_answer_500_for_a_camera_above_the_ceiling(appliance):
    from fastapi.testclient import TestClient
    client = TestClient(main.app, base_url="https://app.anyaicam.com")  # no lifespan: no real supervisors
    response = client.get("/ready")
    assert response.status_code in {200, 503}, response.text
    assert "KeyError" not in response.text
