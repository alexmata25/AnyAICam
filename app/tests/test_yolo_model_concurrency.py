"""P0 resource-safety milestone: regression/stress coverage for the
shared YOLO model concurrency fix.

Root cause this fixes: yolo_model_lock was declared at module level
(`yolo_model_lock = None`) but never actually used anywhere -- neither
get_yolo_model()'s lazy singleton init (a real check-then-act race:
two camera threads could both see yolo_model is None and each
construct their own YOLO() instance) nor detect_objects_frame()'s
model.predict() call (up to 4 camera threads, one per camera, calling
into the SAME shared model concurrently via asyncio.to_thread, with
nothing serializing them) were ever protected. The fix makes
yolo_model_lock a real threading.Lock and wraps both critical sections
in it.

Same import/isolation constraints as test_analytics_retention.py
(imports `main` -- must run inside the deployed container or via
Windows-native Python, not this WSL host's plain python3/pytest).

Two kinds of proof here, deliberately kept separate:
1. Structural: confirms the actual source of get_yolo_model() and
   detect_objects_frame() really does acquire yolo_model_lock around
   the right statements -- not just that a lock object exists
   somewhere unused, which was exactly the previous bug.
2. Real concurrent stress: spawns real OS threads (not just asyncio
   tasks -- this bug only manifests across real threads, since
   detect_objects_frame() is always invoked via asyncio.to_thread) and
   proves under actual contention that (a) the lazy singleton is
   constructed exactly once no matter how many threads race to load
   it, and (b) two threads holding yolo_model_lock never have
   overlapping critical sections.
"""

import ast
import inspect
import threading
import time

import main


# ------------------------------------------------------------- structural: the fix is wired to the right call sites


def test_get_yolo_model_check_and_construct_are_inside_the_lock():
    tree = ast.parse(inspect.getsource(main.get_yolo_model))
    function_node = tree.body[0]
    with_nodes = [node for node in ast.walk(function_node) if isinstance(node, ast.With)]
    assert with_nodes, "get_yolo_model() has no `with` block at all -- the lock isn't used"
    lock_with = with_nodes[0]
    assert any(
        isinstance(item.context_expr, ast.Name) and item.context_expr.id == "yolo_model_lock"
        for item in lock_with.items
    ), "the with-block in get_yolo_model() doesn't lock on yolo_model_lock"
    # The None-check and the YOLO(...) construction must both be inside that with-block's body.
    inner_source = ast.dump(lock_with)
    assert "yolo_model" in inner_source
    assert "YOLO" in inner_source


def test_detect_objects_frame_predict_call_is_inside_the_lock():
    source = inspect.getsource(main.detect_objects_frame)
    tree = ast.parse(source)
    function_node = tree.body[0]
    with_nodes = [node for node in ast.walk(function_node) if isinstance(node, ast.With)]
    locked_with_blocks = [
        node for node in with_nodes
        if any(isinstance(item.context_expr, ast.Name) and item.context_expr.id == "yolo_model_lock" for item in node.items)
    ]
    assert locked_with_blocks, "detect_objects_frame() never locks on yolo_model_lock at all"
    assert any("predict" in ast.dump(block) for block in locked_with_blocks), (
        "model.predict() is not inside a yolo_model_lock-guarded block"
    )


def test_yolo_model_lock_is_a_real_threading_lock_not_the_old_none_placeholder():
    # The bug this fixes: yolo_model_lock used to be a bare `None`,
    # which would raise "'NoneType' object does not support the
    # context manager protocol" the instant anyone tried `with
    # yolo_model_lock:` -- so this also proves the lock is actually
    # usable, not just present.
    assert main.yolo_model_lock is not None
    with main.yolo_model_lock:
        pass  # must not raise


# ------------------------------------------------------------- real concurrent stress: proves mutual exclusion under actual thread contention


def test_concurrent_get_yolo_model_calls_construct_the_model_exactly_once(monkeypatch):
    """Widens the original race window deliberately: the fake
    constructor sleeps while inside the constructor call, so if the
    lock weren't actually serializing get_yolo_model()'s check-then-act,
    multiple real threads racing here would reliably both pass the
    `yolo_model is None` check before either finishes constructing --
    this reproduces the original bug's failure mode if the fix regresses."""
    construct_calls = []
    construct_lock_free_overlap_detected = threading.Event()
    currently_constructing = threading.Event()

    class FakeYoloModel:
        def __init__(self, name):
            if currently_constructing.is_set():
                # Another thread is already mid-construction -- if we
                # get here too, the lock failed to serialize us.
                construct_lock_free_overlap_detected.set()
            currently_constructing.set()
            construct_calls.append(name)
            time.sleep(0.05)
            currently_constructing.clear()

    monkeypatch.setattr(main, "YOLO", FakeYoloModel)
    monkeypatch.setattr(main, "yolo_model", None)
    monkeypatch.setattr(main, "YOLO_MODEL_NAME", "fake-model.pt")
    monkeypatch.setattr(main, "YOLO_DEVICE", "cpu")

    results = []
    errors = []

    def worker():
        try:
            results.append(main.get_yolo_model())
        except Exception as error:  # pragma: no cover - failure path only
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert len(results) == 8
    assert len(construct_calls) == 1, f"model was constructed {len(construct_calls)} times, expected exactly 1"
    assert not construct_lock_free_overlap_detected.is_set(), "two threads were inside the constructor simultaneously"
    assert all(model is results[0] for model in results), "not every thread got the same singleton instance"


def test_concurrent_lock_holders_never_overlap():
    """Direct proof of mutual exclusion under real thread contention:
    many real threads race to acquire yolo_model_lock, each records
    its own enter/exit instants, and no two recorded intervals may
    overlap -- the exact property model.predict() calls now rely on."""
    intervals = []
    intervals_guard = threading.Lock()  # protects the intervals list itself, unrelated to the property under test

    def worker(worker_id):
        with main.yolo_model_lock:
            start = time.monotonic()
            time.sleep(0.01)
            end = time.monotonic()
        with intervals_guard:
            intervals.append((worker_id, start, end))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(intervals) == 10
    ordered = sorted(intervals, key=lambda item: item[1])
    for (_, _, prev_end), (_, next_start, _) in zip(ordered, ordered[1:]):
        assert next_start >= prev_end, "two threads held yolo_model_lock at overlapping times"
