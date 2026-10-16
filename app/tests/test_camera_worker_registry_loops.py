"""The per-camera worker registries are module-level, so they outlive one
application lifespan. When the application starts again in the same process
(test clients, a reload), a task left there by the earlier event loop must
neither be gathered at shutdown ("The future belongs to a different loop")
nor stop the camera from getting a fresh worker on the new loop."""
import asyncio

import main


def _foreign_task():
    """A pending task that belongs to another, never-run event loop."""
    old_loop = asyncio.new_event_loop()

    async def never_finishes():
        await asyncio.sleep(3600)

    task = old_loop.create_task(never_finishes())
    return old_loop, task


def test_a_task_from_an_earlier_loop_is_not_gathered_and_the_camera_gets_a_fresh_worker(monkeypatch):
    old_loop, stale = _foreign_task()
    started = []

    async def fake_motion_detector(number):
        started.append(number)
        await asyncio.sleep(0)

    monkeypatch.setattr(main, "motion_detector", fake_motion_detector)
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", True)
    monkeypatch.setattr(main, "AI_PERSON_DETECTION_ENABLED", False)
    monkeypatch.setattr(main, "_camera_worker_tasks", {"motion": {7: stale}, "ai": {}})
    monkeypatch.setattr(main, "_camera_worker_retiring", {"motion": {}, "ai": {}})

    async def new_lifespan():
        # Shutdown gathers camera_worker_tasks(): the stale task must not be in it.
        assert stale not in main.camera_worker_tasks()
        result = main.reconcile_camera_workers({7})
        fresh = main._camera_worker_tasks["motion"][7]
        assert fresh is not stale and fresh.get_loop() is asyncio.get_running_loop()
        assert result["started"] == {"motion": [7]}
        await asyncio.gather(*main.camera_worker_tasks(), return_exceptions=True)  # no ValueError
        return fresh

    fresh = asyncio.run(new_lifespan())
    assert fresh.done() and started == [7]
    stale.cancel()
    old_loop.close()


def test_tasks_of_the_running_loop_are_still_listed_and_kept(monkeypatch):
    monkeypatch.setattr(main, "_camera_worker_retiring", {"motion": {}, "ai": {}})

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(3600))
        monkeypatch.setattr(main, "_camera_worker_tasks", {"motion": {3: task}, "ai": {}})
        listed = main.camera_worker_tasks()
        kept = main.reconcile_camera_workers(None)  # unknown camera map: only starts, never retires
        still = main._camera_worker_tasks["motion"].get(3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return listed, kept, still, task

    listed, kept, still, task = asyncio.run(scenario())
    assert listed == [task] and still is task and kept["retired"] == []
