"""Real remaining "5-minute assumption" found live during the
2026-09-22 event-linkage follow-up review: build_manual_clip() (the
customer-facing manual clip-download builder behind POST /api/customer/
clips) filtered candidate source files with a hardcoded `timedelta(
minutes=5)` lookback from the requested start_time. That number was
only ever correct by coincidence -- it happens to match both
RECORDING_SEGMENT_SECONDS (Continuous mode's own fixed 300s segments)
and local_recording_policy.DEFAULT_MAX_EVENT_RECORDING_SECONDS (300s).
A camera with an explicitly configured, LONGER local_recording_max_
event_seconds (persist_event_recording()'s own merge-extension safety
cap, a real per-camera setting) could have a real, still-relevant
Event-mode recording start more than 5 minutes before a customer's
requested start_time -- which this hardcoded window silently excluded
from the candidate list, raising "No completed recordings cover the
selected time range." for a request that should have succeeded.

Fixed by widening the lookback window to the larger of
RECORDING_SEGMENT_SECONDS and this camera's own configured
max_event_seconds, so the candidate search is never narrower than
either mode's own actual maximum recording span.
"""
import asyncio
import os
import time as time_module
from datetime import datetime, timedelta

import main


def _write_file(camera_folder, camera_number, dt):
    camera_folder.mkdir(parents=True, exist_ok=True)
    path = camera_folder / f"camera{camera_number}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path.write_bytes(b"x" * 4096)
    old_time = time_module.time() - 3600  # well past any "still being written" cutoff
    os.utime(path, (old_time, old_time))
    return path


def _job_id():
    return "test-job-" + str(id(object()))


async def _run(job_id, camera_number, start_time, end_time):
    main.clip_jobs[job_id] = {"id": job_id, "status": "queued", "progress": 0, "message": ""}
    try:
        await main.build_manual_clip(job_id, camera_number, start_time, end_time)
    except Exception:
        pass  # build_manual_clip() itself already catches everything into job["message"]
    return main.clip_jobs[job_id]


def test_a_recording_starting_past_the_old_hardcoded_5_minutes_is_found_when_max_event_seconds_is_configured_longer(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "CLIPS_FOLDER", tmp_path / "clips")
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"max_event_seconds": 600})

    now = datetime.now()
    start_time = now
    end_time = now + timedelta(seconds=10)
    # 8 minutes (480s) before start_time -- past the OLD hardcoded 300s
    # (5-minute) window, but within this camera's real configured 600s cap.
    _write_file(tmp_path / "camera1", 1, start_time - timedelta(minutes=8))

    job = asyncio.run(_run(_job_id(), 1, start_time, end_time))

    assert job["message"] != "No completed recordings cover the selected time range.", (
        "a recording 8 minutes before start_time must be a valid candidate when this camera's "
        "own max_event_seconds (600s) allows a recording that long"
    )


def test_a_recording_starting_past_both_real_maximums_is_still_correctly_excluded(monkeypatch, tmp_path):
    """The fix widens the window, but must not make it unboundedly wide
    -- a file older than either mode's own real maximum span genuinely
    cannot be the source for this request."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "CLIPS_FOLDER", tmp_path / "clips")
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"max_event_seconds": 300})

    now = datetime.now()
    start_time = now
    end_time = now + timedelta(seconds=10)
    # 20 minutes before start_time -- past both RECORDING_SEGMENT_SECONDS
    # (300s) and this camera's own configured max_event_seconds (300s).
    _write_file(tmp_path / "camera1", 1, start_time - timedelta(minutes=20))

    job = asyncio.run(_run(_job_id(), 1, start_time, end_time))

    assert job["message"] == "No completed recordings cover the selected time range."


def test_default_300_second_max_event_seconds_still_matches_the_original_5_minute_behavior(monkeypatch, tmp_path):
    """A camera with no explicit override (the common case) must behave
    exactly as before this fix -- RECORDING_SEGMENT_SECONDS and the
    default max_event_seconds are both 300s, so the effective window is
    unchanged for every camera that never configured a longer cap."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "CLIPS_FOLDER", tmp_path / "clips")
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"max_event_seconds": 300})

    now = datetime.now()
    start_time = now
    end_time = now + timedelta(seconds=10)
    # 4 minutes before start_time -- within the original 5-minute window.
    _write_file(tmp_path / "camera1", 1, start_time - timedelta(minutes=4))

    job = asyncio.run(_run(_job_id(), 1, start_time, end_time))

    assert job["message"] != "No completed recordings cover the selected time range."
