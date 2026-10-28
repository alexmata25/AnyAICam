"""Source-extracted lifecycle probes: no app startup, cameras, uploads or model loading.

Exercise the actual finalizer and lifespan shutdown body with local worker/owner
spies. These tests do not certify Windows service-stop behavior or clip flushing.
"""
import ast
import asyncio
import copy
import gc
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


def production_functions():
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8-sig"))
    finalizer = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_finalize_ai_activity")
    lifespan = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    lifecycle = next(n for n in lifespan.body if isinstance(n, ast.Try)
                     and any(isinstance(child, ast.Yield) for statement in n.body for child in ast.walk(statement)))
    calls = []
    namespace = {
        "asyncio": asyncio, "datetime": datetime,
        "_ai_activity_limits": lambda camera: (30, 300),
        "ai_activities": SimpleNamespace(close_due=lambda *a, **kw: datetime.now() + timedelta(seconds=60),
                                         close=lambda activity: calls.append("activity.closed")),
        "event_media_sharing": SimpleNamespace(owner_finished=lambda *a: calls.append(("owner.finished", a))),
        "structured_log": lambda message, **kw: calls.append(message),
        "ai_frames": SimpleNamespace(stop_all=lambda: calls.append("frames.stopped")),
        "ffmpeg_processes": [], "supervisor_tasks": [], "motion_tasks": [], "ai_tasks": [],
        "camera_worker_tasks": lambda: [],
    }
    for node in lifecycle.finalbody:
        for name in ast.walk(node):
            if isinstance(name, ast.Name) and name.id.endswith("_task"):
                namespace[name.id] = None
    shutdown = ast.AsyncFunctionDef(
        name="shutdown_probe", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=copy.deepcopy(lifecycle.finalbody), decorator_list=[], type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(finalizer), shutdown], type_ignores=[]))
    exec(compile(module, "main.py lifecycle source", "exec"), namespace)
    return namespace, calls


def activity():
    return SimpleNamespace(owner_id="isolated-shutdown-probe")


def test_close_without_cancellation_reproduces_fixture_warning():
    ns, calls = production_functions()
    contexts = []
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda loop, context: contexts.append(context))
    task = loop.create_task(ns["_finalize_ai_activity"](1, activity(), None))
    loop.run_until_complete(asyncio.sleep(0))
    assert not task.done()
    loop.close()
    del task
    gc.collect()
    assert any("Task was destroyed but it is pending" in c["message"] for c in contexts)
    assert not calls  # GeneratorExit intentionally does no blocking cleanup.


def test_cancel_and_await_finalizer_releases_owner_before_loop_close():
    ns, calls = production_functions()
    loop = asyncio.new_event_loop()
    contexts = []
    loop.set_exception_handler(lambda loop, context: contexts.append(context))
    task = loop.create_task(ns["_finalize_ai_activity"](1, activity(), None))
    loop.run_until_complete(asyncio.sleep(0))
    task.cancel()
    loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    assert task.cancelled()
    assert not asyncio.all_tasks(loop)
    assert calls == ["activity.closed", ("owner.finished", ("isolated-shutdown-probe", 1, False))]
    loop.close()
    assert not contexts


def test_asyncio_runner_cancels_untracked_finalizer():
    ns, calls = production_functions()

    async def scenario():
        task = asyncio.create_task(ns["_finalize_ai_activity"](1, activity(), None))
        await asyncio.sleep(0)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert calls[-1] == ("owner.finished", ("isolated-shutdown-probe", 1, False))


def test_lifespan_shutdown_leaves_finalizer_for_outer_runner():
    # Characterize the current ownership gap, not a desired release behavior.
    # Update this assertion when explicit finalizer shutdown ownership is added.
    ns, calls = production_functions()

    async def scenario():
        ns["cloud_upload_task"] = asyncio.create_task(asyncio.sleep(60))
        finalizer = asyncio.create_task(ns["_finalize_ai_activity"](1, activity(), None))
        await asyncio.sleep(0)
        await ns["shutdown_probe"]()
        assert ns["cloud_upload_task"].cancelled()
        assert "shutdown.complete" in calls
        assert not finalizer.done()  # Not owned by the lifespan's explicit task list.
        assert "activity.closed" not in calls
        return finalizer

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert calls.index("shutdown.complete") < calls.index("activity.closed")
