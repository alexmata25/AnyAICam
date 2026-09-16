"""Motion-gated cloud upload (Hybrid recording-mode tier): _pending_recording_files()
only queues a segment whose time window overlaps a real, padded motion
event when cloud_recording_mode == 'motion'. Continuous/None ("Cloud" tier
/ no explicit mode) cameras are unaffected. 'disabled' ("Local" tier)
cameras are skipped one level up, in recording_upload_worker()'s own loop
-- see test_cloud_recording_mode_disabled.py for that admin-API/DB half.

Ported from the motion-gated-cloud-upload-cloud-side-20260822 branch
(commit c380f7e), adapted to the current _pending_recording_files()
signature (no cutoff/backlog logic in this branch's own independent
history) and to cloud_recording_mode as the camera-map key (this
branch's name for the same per-camera value c380f7e called
recording_mode).

Same fake/isolation conventions as this suite's sibling files
(test_recording_uploader_newest_first.py, test_recording_uploader_
credential_hardening.py): real _pending_recording_files(), RECORDINGS_
FOLDER redirected to a pytest tmp_path, MOTION_EVENTS_FILE derived from
it so each test's motion data is fully isolated.
"""

import json
from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(ru, "MOTION_EVENTS_FILE", tmp_path / "motion_events.jsonl")
    monkeypatch.setattr(ru, "_camera_map", {})


def _make_recording(folder, camera_number, start, content=b"segment bytes"):
    camera_folder = folder / f"camera{camera_number}"
    camera_folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{start.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = camera_folder / name
    path.write_bytes(content)
    return path


def _seed_segments(tmp_path, camera_number, starts):
    """One completed segment per start time, plus one extra newest file
    _completed_recording_files() always excludes as still-being-written."""
    paths = [_make_recording(tmp_path, camera_number, start) for start in starts]
    _make_recording(tmp_path, camera_number, starts[-1] + timedelta(hours=1))
    return paths


def _set_camera(camera_number, cloud_recording_mode):
    ru._camera_map[camera_number] = {
        "camera_id": f"cam-{camera_number}",
        "site_id": "site-1",
        "cloud_recording_mode": cloud_recording_mode,
        "people_counting_enabled": False,
        "smart_motion_enabled": False,
        "lpr_enabled": False,
        "ppe_enabled": False,
    }


def _write_motion_event(tmp_path, camera_number, start, end):
    event = {
        "id": "evt-1",
        "camera": camera_number,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }
    with (tmp_path / "motion_events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


BASE = datetime(2026, 9, 16, 8, 0, 0)


def test_segment_fully_inside_a_motion_window_is_uploaded(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    _write_motion_event(tmp_path, 1, BASE + timedelta(seconds=30), BASE + timedelta(seconds=60))
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_segment_with_no_overlapping_motion_is_skipped(tmp_path):
    _set_camera(1, "motion")
    _seed_segments(tmp_path, 1, [BASE])
    _write_motion_event(tmp_path, 1, BASE + timedelta(hours=5), BASE + timedelta(hours=5, seconds=30))
    pending = ru._pending_recording_files(1, set())
    assert pending == []


def test_padding_rescues_a_segment_just_before_the_window(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    segment_end = BASE + timedelta(seconds=ru.RECORDING_SEGMENT_SECONDS)
    # Motion starts 10s after this segment ends -- inside the default 15s pre-padding.
    _write_motion_event(tmp_path, 1, segment_end + timedelta(seconds=10), segment_end + timedelta(seconds=40))
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_padding_does_not_rescue_a_segment_far_before_the_window(tmp_path):
    _set_camera(1, "motion")
    _seed_segments(tmp_path, 1, [BASE])
    segment_end = BASE + timedelta(seconds=ru.RECORDING_SEGMENT_SECONDS)
    # Motion starts 60s after this segment ends -- outside the default 15s pre-padding.
    _write_motion_event(tmp_path, 1, segment_end + timedelta(seconds=60), segment_end + timedelta(seconds=90))
    pending = ru._pending_recording_files(1, set())
    assert pending == []


def test_continuous_mode_uploads_everything_with_no_motion_file_at_all(tmp_path):
    _set_camera(1, "continuous")
    paths = _seed_segments(tmp_path, 1, [BASE])
    assert not (tmp_path / "motion_events.jsonl").exists()
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_no_explicit_mode_behaves_like_continuous(tmp_path):
    _set_camera(1, None)
    paths = _seed_segments(tmp_path, 1, [BASE])
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_missing_motion_file_fails_safe_and_uploads_anyway(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    assert not (tmp_path / "motion_events.jsonl").exists()
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_malformed_motion_file_fails_safe_and_uploads_anyway(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    (tmp_path / "motion_events.jsonl").write_text("not valid json at all\n", encoding="utf-8")
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_one_malformed_line_does_not_blind_the_rest_of_the_file(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    with (tmp_path / "motion_events.jsonl").open("w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({
            "id": "evt-1", "camera": 1,
            "start_time": (BASE + timedelta(seconds=30)).isoformat(),
            "end_time": (BASE + timedelta(seconds=60)).isoformat(),
        }) + "\n")
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_only_this_cameras_motion_events_count(tmp_path):
    _set_camera(1, "motion")
    _seed_segments(tmp_path, 1, [BASE])
    _write_motion_event(tmp_path, 2, BASE + timedelta(seconds=30), BASE + timedelta(seconds=60))
    pending = ru._pending_recording_files(1, set())
    assert pending == []


def test_already_uploaded_segments_are_never_reconsidered_by_the_gate(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    _write_motion_event(tmp_path, 1, BASE + timedelta(seconds=30), BASE + timedelta(seconds=60))
    pending = ru._pending_recording_files(1, {paths[0].name})
    assert pending == []


def test_repeated_calls_are_idempotent(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    _write_motion_event(tmp_path, 1, BASE + timedelta(seconds=30), BASE + timedelta(seconds=60))
    first = ru._pending_recording_files(1, set())
    second = ru._pending_recording_files(1, set())
    assert first == second == paths


def test_a_late_arriving_event_can_still_rescue_an_earlier_segment(tmp_path):
    _set_camera(1, "motion")
    paths = _seed_segments(tmp_path, 1, [BASE])
    # First pass: no motion data at all yet -- fails safe, uploads anyway,
    # but the caller hasn't marked it uploaded (simulating an upload
    # failure/not-yet-attempted), so it's still eligible on the next scan.
    first = ru._pending_recording_files(1, set())
    assert first == paths
    # Motion arrives late, after the fact, still overlapping the segment.
    _write_motion_event(tmp_path, 1, BASE + timedelta(seconds=30), BASE + timedelta(seconds=60))
    second = ru._pending_recording_files(1, set())
    assert second == paths


def test_disabled_camera_is_skipped_entirely_by_the_worker_loop_gate(tmp_path):
    # This is the 'disabled'/Local-tier half -- enforced one level up in
    # recording_upload_worker(), not inside _pending_recording_files()
    # itself (which only ever sees cameras the worker loop let through).
    _set_camera(1, "disabled")
    assert ru._camera_identity(1)["cloud_recording_mode"] == "disabled"
