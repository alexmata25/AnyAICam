"""Picklable, module-level fake inference functions for
test_aaco_llm.py's ProcessPoolExecutor-based tests.

Deliberately NOT defined inside test_aaco_llm.py itself: a
ProcessPoolExecutor on Windows (the only platform with no fork(), so it
always uses "spawn") re-imports whatever module a submitted function
lives in from scratch in the fresh worker process. pytest's own import
machinery (assertion rewriting) can make functions defined directly in
a test file awkward or unreliable to pickle by reference for exactly
that reason. A plain, ordinary module with no test collection and no
import-hook involvement sidesteps that risk entirely, on every
platform, not just the one this happened to be found on."""
import os
import time


def _fake_infer_raises(model_path, system_prompt, text, max_tokens):
    raise RuntimeError("inference backend crashed")


def _fake_infer_sleeps_then_succeeds(model_path, system_prompt, text, max_tokens):
    time.sleep(5)
    return '{"operation": "camera_status"}'


def _fake_infer_crashes_the_process(model_path, system_prompt, text, max_tokens):
    # os._exit() terminates this worker process immediately, with no
    # cleanup -- exactly as abruptly as a real native GGML_ASSERT/
    # abort() would, without needing an actual native crash to prove
    # ProcessPoolExecutor contains it to this one worker.
    os._exit(1)


def _fake_infer_returns_camera_status(model_path, system_prompt, text, max_tokens):
    return '{"operation": "camera_status"}'
