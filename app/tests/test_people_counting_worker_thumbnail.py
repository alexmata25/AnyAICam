"""Regression coverage for a real gap found during physical People
Counting validation (2026-09-22): every crossing event people_counting_
worker() (main.py) appended via append_analytics_event() carried
`thumbnail=None`, unconditionally, always -- confirmed by code
inspection to be the ONLY analytics event type in this codebase with no
image at all (motion/person/car/ppe/plate/facial all save one via the
same AI_THUMBNAILS_FOLDER pattern). The worker already has the frame in
hand (used to build detection centroids) at the exact moment a crossing
is decided; it just never saved it.

Fixed by saving that same frame once per detection cycle (not once per
event -- multiple crossings in one cycle share the same frame/URL,
mirroring save_yolo_events()'s own one-frame-per-batch shape) and
attaching the resulting URL to every crossing event this cycle produces.

people_counting_worker() is an infinite `while True` loop; test 2 below
runs it for exactly one iteration by patching its own trailing
`await asyncio.sleep(PEOPLE_COUNTING_INTERVAL_SECONDS)` to raise
CancelledError on first call -- the loop's own `except asyncio.
CancelledError: raise` propagates that straight out, a clean exit after
one full iteration with no race against a real wall-clock timeout. Test
1 instead lets the real interval run (patched very short) and polls for
the first recorded event, since it needs the real PeopleCounter to
actually confirm a multi-step crossing across several iterations.
"""

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


async def _run_one_cycle(camera_number: int, monkeypatch) -> None:
    """Runs the worker's `while True` loop for EXACTLY one iteration:
    main.py's own `await asyncio.sleep(PEOPLE_COUNTING_INTERVAL_SECONDS)`
    at the bottom of the loop body is patched to raise CancelledError on
    its first call, which the loop's own `except asyncio.CancelledError:
    raise` propagates straight out -- a clean exit after one full
    iteration, not a race against a real wall-clock timeout."""
    async def fake_sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    try:
        await main.people_counting_worker(camera_number)
    except asyncio.CancelledError:
        pass


@pytest.fixture
def _people_counting_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(
        main.recording_uploader, "_camera_identity",
        lambda camera_number: {"people_counting_enabled": True},
    )
    monkeypatch.setattr(
        main, "_load_people_counting_rule",
        lambda camera_number: {
            "id": "rule-1", "name": "test line", "direction": "both",
            "geometry": [{"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}],
        },
    )
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "_people_counting_state_save", lambda camera_number, counter: None)
    # Fast enough that asyncio.wait_for's timeout reliably covers at
    # least one full iteration without the test itself being slow.
    monkeypatch.setattr(main, "PEOPLE_COUNTING_INTERVAL_SECONDS", 0.01)
    return tmp_path


def _fake_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


def test_crossing_event_now_gets_a_real_thumbnail(monkeypatch, _people_counting_setup):
    """Direct reproduction: a single person crossing the line must
    produce an event whose thumbnail is a real, existing file -- not
    None."""
    frame = _fake_frame()
    # A person walking steadily downward across the y=0.5 line in small,
    # continuously-trackable steps (each well under the tracker's own
    # max_match_distance=0.20, so the SAME track is matched every call
    # rather than aging out and starting a fresh, side-less track --
    # see people_counting.py's own PeopleCounter.update() docstring for
    # why a big unexplained jump correctly does NOT count as a crossing).
    calls = {"n": 0}
    y_steps = [0.30, 0.40, 0.50, 0.60]

    def fake_detect_objects_frame(camera_number):
        calls["n"] += 1
        y = y_steps[min(calls["n"] - 1, len(y_steps) - 1)]
        return {
            "ok": True, "frame": frame, "error": None,
            "detections": [{"class_name": "person", "x": 270, "y": int(y * 480) - 50, "width": 100, "height": 100}],
        }

    monkeypatch.setattr(main, "detect_objects_frame", fake_detect_objects_frame)

    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))

    async def driver():
        task = asyncio.create_task(main.people_counting_worker(1))
        for _ in range(200):
            if recorded:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(driver())

    assert recorded, "expected at least one crossing event to be recorded"
    event = recorded[0]
    assert event["thumbnail"], "crossing event must carry a real thumbnail URL, not None"
    assert event["thumbnail"].startswith("/recordings/media/ai/")
    # The URL must point at a file that actually exists on disk.
    day_folder = _people_counting_setup
    saved_files = list(day_folder.rglob("*.jpg"))
    assert len(saved_files) >= 1, "no thumbnail file was actually written to AI_THUMBNAILS_FOLDER"


def test_multiple_crossings_in_one_cycle_share_one_thumbnail_not_one_each(monkeypatch, _people_counting_setup):
    """Two crossing events produced by the SAME detection cycle must
    reuse one saved frame/thumbnail URL, not write a duplicate file per
    event -- mirrors save_yolo_events()'s own one-frame-per-batch
    convention exactly. Uses camera 2 (not PEOPLE_COUNTING_DEBUG_CAMERA's
    default of 1) so the worker's debug=True path -- which expects
    update() to return (events, debug_entries), not a bare fake events
    list -- is not exercised; that path is real production code, not
    what this test is checking."""
    frame = _fake_frame()

    class _FakeEvent:
        def __init__(self, direction):
            self.direction = direction
            self.track_id = 1
            self.frame_index = 1
            self.x = 0.5
            self.y = 0.5

    class _FakeCounter:
        in_count = 2
        out_count = 0

        @property
        def occupancy(self):
            return self.in_count - self.out_count

        def update(self, centroids, debug=False):
            return [_FakeEvent("in"), _FakeEvent("in")]

    monkeypatch.setattr(main.people_counting, "PeopleCounter", lambda line: _FakeCounter())
    monkeypatch.setattr(
        main, "detect_objects_frame",
        lambda camera_number: {
            "ok": True, "frame": frame, "error": None,
            "detections": [{"class_name": "person", "x": 270, "y": 190, "width": 100, "height": 100}],
        },
    )

    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))

    asyncio.run(_run_one_cycle(2, monkeypatch))

    assert len(recorded) == 2
    assert recorded[0]["thumbnail"] == recorded[1]["thumbnail"]
    assert recorded[0]["thumbnail"] is not None
    day_folder = _people_counting_setup
    saved_files = list(day_folder.rglob("*.jpg"))
    assert len(saved_files) == 1, f"expected exactly one thumbnail file for one detection cycle, got {len(saved_files)}"
