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

from database_backend import override_target
from partner_db import initialize_database

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(ru, "MOTION_EVENTS_FILE", tmp_path / "motion_events.jsonl")
    monkeypatch.setattr(ru, "_camera_map", {})
    # Isolates the new (2026-09-16) daily-cloud-allowance bookkeeping's
    # own local DB reads/writes (camera_cloud_upload_daily) -- every
    # test in this file that isn't specifically exercising the
    # allowance itself gets a huge default so the pre-existing motion-
    # gate assertions below are never incidentally affected by it.
    with override_target(sqlite_path=tmp_path / "test_recording_uploader_motion_gate.db"):
        initialize_database()
        monkeypatch.setattr(ru, "_daily_cloud_seconds", 10**9)
        yield


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


# --------------------------------------------------------------- Hybrid daily cloud-upload allowance


def test_allowance_permits_segments_up_to_the_configured_daily_limit(tmp_path, monkeypatch):
    # Product architecture (2026-09-16): only 'continuous' (Cloud tier)
    # cameras ever reach the allowance check at all -- Hybrid ('motion')
    # is excluded one level up, in recording_upload_worker()'s own gate
    # (see test_recording_uploader.py's dedicated worker-loop proof of
    # that), so it's never exercised here. An opt-in ceiling on the
    # premium continuous tier is still a real, useful lever an RDM
    # administrator may choose to configure.
    _set_camera(1, "continuous")
    monkeypatch.setattr(ru, "_daily_cloud_seconds", ru.RECORDING_SEGMENT_SECONDS * 2)
    starts = [BASE, BASE + timedelta(seconds=ru.RECORDING_SEGMENT_SECONDS)]
    paths = _seed_segments(tmp_path, 1, starts)
    pending = ru._pending_recording_files(1, set())
    assert pending == paths  # exactly 2 segments fit a 2-segment allowance


def test_allowance_rejects_a_segment_that_would_exceed_the_daily_limit(tmp_path, monkeypatch):
    _set_camera(1, "continuous")
    # 1.5 segments' worth -- the 2nd full segment cannot fit.
    monkeypatch.setattr(ru, "_daily_cloud_seconds", int(ru.RECORDING_SEGMENT_SECONDS * 1.5))
    starts = [BASE, BASE + timedelta(seconds=ru.RECORDING_SEGMENT_SECONDS)]
    paths = _seed_segments(tmp_path, 1, starts)
    pending = ru._pending_recording_files(1, set())
    assert pending == paths[:1]


def test_zero_remaining_allowance_permits_nothing(tmp_path, monkeypatch):
    # _pending_recording_files() checks usage under the REAL current
    # date (datetime.now()), not this file's own fixed BASE constant --
    # pre-seed under that same real "today" so this test is not
    # fragile to which calendar day it happens to run on.
    _set_camera(1, "continuous")
    monkeypatch.setattr(ru, "_daily_cloud_seconds", ru.RECORDING_SEGMENT_SECONDS)
    starts = [BASE]
    _seed_segments(tmp_path, 1, starts)
    real_today = datetime.now().strftime("%Y-%m-%d")
    ru._record_local_cloud_upload_seconds(1, real_today, ru.RECORDING_SEGMENT_SECONDS)
    pending = ru._pending_recording_files(1, set())
    assert pending == []


def test_already_used_seconds_today_reduce_the_remaining_allowance(tmp_path, monkeypatch):
    _set_camera(1, "continuous")
    monkeypatch.setattr(ru, "_daily_cloud_seconds", ru.RECORDING_SEGMENT_SECONDS * 3)
    real_today = datetime.now().strftime("%Y-%m-%d")
    ru._record_local_cloud_upload_seconds(1, real_today, ru.RECORDING_SEGMENT_SECONDS * 2)
    starts = [BASE, BASE + timedelta(seconds=ru.RECORDING_SEGMENT_SECONDS)]
    paths = _seed_segments(tmp_path, 1, starts)
    pending = ru._pending_recording_files(1, set())
    assert pending == paths[:1]  # only 1 segment's worth of allowance remains


def test_no_configured_ceiling_means_unlimited_for_continuous(tmp_path, monkeypatch):
    """The Continuous/Cloud tier's own default -- no RDM override means
    no cap at all, matching its "customer pays for what they use"
    product purpose."""
    _set_camera(1, "continuous")
    monkeypatch.setattr(ru, "_daily_cloud_seconds", None)
    starts = [BASE, BASE + timedelta(seconds=ru.RECORDING_SEGMENT_SECONDS)]
    paths = _seed_segments(tmp_path, 1, starts)
    pending = ru._pending_recording_files(1, set())
    assert pending == paths


def test_allowance_usage_is_persisted_and_survives_a_fresh_process(tmp_path, monkeypatch):
    """The real point of this whole feature: a container/service
    restart must never reset or bypass the allowance. Simulated here by
    reading the usage back through a brand-new call with no in-memory
    state carried over -- the module itself never cached this value in
    a process-lifetime dict anywhere, only in the DB."""
    today = BASE.strftime("%Y-%m-%d")
    ru._record_local_cloud_upload_seconds(1, today, 1200)
    assert ru._local_daily_seconds_used(1, today) == 1200
    ru._record_local_cloud_upload_seconds(1, today, 300)
    assert ru._local_daily_seconds_used(1, today) == 1500  # accumulates, never overwrites


def test_allowance_resets_on_a_new_calendar_day(tmp_path, monkeypatch):
    ru._record_local_cloud_upload_seconds(1, "2026-09-15", ru.RECORDING_SEGMENT_SECONDS * 10)
    assert ru._local_daily_seconds_used(1, "2026-09-16") == 0


def test_allowance_is_tracked_independently_per_camera(tmp_path, monkeypatch):
    today = BASE.strftime("%Y-%m-%d")
    ru._record_local_cloud_upload_seconds(1, today, ru.RECORDING_SEGMENT_SECONDS * 5)
    assert ru._local_daily_seconds_used(2, today) == 0


# The core product-architecture requirement -- Hybrid ('motion') never
# uploads a single continuous segment to cloud, regardless of motion or
# any configured allowance -- is proven at the real enforcement point,
# recording_upload_worker()'s own per-camera gate, in test_recording_
# uploader.py::test_worker_never_calls_relay_for_a_hybrid_or_disabled_
# camera. This file's own motion-window tests above continue to prove
# that logic itself is still correct (preserved, not deleted) if ever
# reached again by a future product tier -- see _pending_recording_
# files()'s own docstring.
