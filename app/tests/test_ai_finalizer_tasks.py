import asyncio
import threading

import pytest

from ai_finalizer_tasks import ActivityFinalizers


def test_worker_thread_submission_finishes_before_drain_returns():
    async def scenario():
        owner = ActivityFinalizers()
        calls = []
        async def work():
            await asyncio.sleep(.01)
            calls.append('clip persisted')
        loop = asyncio.get_running_loop()
        thread = threading.Thread(target=lambda: owner.submit(work(), loop))
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        result = await owner.drain(timeout=1)
        assert result == {'accepted': 1, 'timed_out': 0, 'remaining': 0, 'failed': 0}
        assert calls == ['clip persisted']
    asyncio.run(scenario())


def test_timeout_awaits_actual_cancellation_cleanup_and_reports_incomplete():
    async def scenario():
        owner = ActivityFinalizers()
        calls = []
        async def work():
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(.01)
                calls.append('owner released')
        owner.submit(work(), asyncio.get_running_loop())
        await asyncio.sleep(0)
        result = await owner.drain(timeout=.01, cancel_timeout=1)
        assert result['timed_out'] == 1 and result['remaining'] == 0
        assert calls == ['owner released']
    asyncio.run(scenario())


def test_cancellation_resistant_task_is_reported_and_not_falsely_drained():
    async def scenario():
        owner = ActivityFinalizers()
        release = asyncio.Event()
        async def work():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()
        future = owner.submit(work(), asyncio.get_running_loop())
        await asyncio.sleep(0)
        result = await owner.drain(timeout=.01, cancel_timeout=.01)
        assert result['timed_out'] == 1 and result['remaining'] == 1
        with pytest.raises(RuntimeError, match='not stopped'):
            owner.start()
        release.set()
        await asyncio.wrap_future(future)
    asyncio.run(scenario())


def test_shutdown_rejects_new_work_and_allows_clean_lifespan_restart():
    async def scenario():
        owner = ActivityFinalizers()
        await owner.drain(timeout=0)
        coroutine = asyncio.sleep(0)
        with pytest.raises(RuntimeError, match='shutting down'):
            owner.submit(coroutine, asyncio.get_running_loop())
        assert coroutine.cr_frame is None
        owner.start()
        await asyncio.wrap_future(owner.submit(asyncio.sleep(0), asyncio.get_running_loop()))
    asyncio.run(scenario())


def test_closed_loop_submission_closes_coroutine_without_abandoning_it():
    owner = ActivityFinalizers()
    loop = asyncio.new_event_loop()
    loop.close()
    coroutine = asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        owner.submit(coroutine, loop)
    assert coroutine.cr_frame is None


def test_existing_motion_clip_tasks_share_the_same_drain_budget():
    async def scenario():
        owner = ActivityFinalizers()
        writes = []
        async def clip():
            await asyncio.sleep(.01)
            writes.append('saved')
        task = asyncio.create_task(clip())
        result = await owner.drain(timeout=1, extra_tasks=[task])
        assert result == {'accepted': 1, 'timed_out': 0, 'remaining': 0, 'failed': 0}
        assert writes == ['saved']
    asyncio.run(scenario())
