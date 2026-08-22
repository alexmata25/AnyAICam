"""Motion-gated cloud upload milestone, edge-side half: tests for
_load_motion_windows(), _segment_overlaps_motion(), and their
integration into _pending_recording_files().

Approved design (see the commit this ships with): local continuous
recording is completely untouched -- this only changes which already-
recorded, already-closed local segments get selected for cloud upload,
and only for cameras with recording_mode == 'motion' (see
_refresh_camera_map()/db_migrations.py's cameras.cloud_recording_mode
on the cloud side). Continuous-plan cameras and cameras with no
explicit mode are provably unaffected by every test in this file that
checks them.

Fast/pure tests only -- no ffmpeg, no AWS, no network. Every test gets
its own isolated RECORDINGS_FOLDER/MOTION_EVENTS_FILE via tmp_path and
resets the module's in-memory camera map/cutoff/backlog-log state so
no test can see another's, matching test_recording_upload_cutoff.py's
own established fixture pattern.
"""

import json
from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(ru, "MOTION_EVENTS_FILE", tmp_path / "recordings" / "motion_events.jsonl")
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._camera_map.clear()
    yield tmp_path
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._camera_map.clear()


def _make_recording(tmp_path, camera_number, started_at, *, newest=False):
    """Same pattern as test_recording_upload_cutoff.py's own helper:
    _completed_recording_files() always excludes the single
    newest-named file as still-open, so a companion, later-named file
    is written to push the real one out of that slot."""
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{started_at.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = folder / name
    path.write_bytes(b"fake")
    if not newest:
        _make_recording(tmp_path, camera_number, started_at + timedelta(days=3650), newest=True)
    return path


def _set_camera(camera_number, recording_mode, camera_id="cam-1", site_id="site-1"):
    ru._camera_map[camera_number] = {"camera_id": camera_id, "site_id": site_id, "recording_mode": recording_mode}


def _write_motion_event(motion_events_file, camera, start, end, extra=None):
    motion_events_file.parent.mkdir(parents=True, exist_ok=True)
    event = {"id": "evt-1", "camera": camera, "start_time": start.isoformat(), "end_time": end.isoformat(), "event_type": "motion"}
    if extra:
        event.update(extra)
    with motion_events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


# ==================================================== _segment_overlaps_motion() -- pure overlap logic


def test_segment_fully_inside_a_motion_window():
    segment_start = datetime(2026, 8, 22, 10, 5, 0)
    segment_end = segment_start + timedelta(seconds=300)
    motion_windows = [(datetime(2026, 8, 22, 10, 0, 0), datetime(2026, 8, 22, 10, 20, 0))]
    assert ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=0)


def test_segment_overlapping_the_beginning_of_a_motion_window():
    segment_start = datetime(2026, 8, 22, 10, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)  # ends 10:05:00
    motion_windows = [(datetime(2026, 8, 22, 10, 4, 30), datetime(2026, 8, 22, 10, 10, 0))]  # starts before segment ends
    assert ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=0)


def test_segment_overlapping_the_end_of_a_motion_window():
    segment_start = datetime(2026, 8, 22, 10, 5, 0)
    segment_end = segment_start + timedelta(seconds=300)  # 10:05-10:10
    motion_windows = [(datetime(2026, 8, 22, 10, 0, 0), datetime(2026, 8, 22, 10, 5, 30))]  # ends just after segment starts
    assert ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=0)


def test_no_motion_segment_does_not_overlap():
    segment_start = datetime(2026, 8, 22, 12, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)
    motion_windows = [(datetime(2026, 8, 22, 10, 0, 0), datetime(2026, 8, 22, 10, 0, 10))]  # hours earlier
    assert not ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=0)


def test_no_motion_windows_at_all_never_overlaps():
    segment_start = datetime(2026, 8, 22, 12, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)
    assert not ru._segment_overlaps_motion(segment_start, segment_end, [], pre_padding_seconds=15, post_padding_seconds=15)


# -------------------------------------------------------------- padding behavior


def test_padding_extends_a_motion_event_before_and_after():
    # Motion event just outside a segment's raw bounds, but within padding.
    segment_start = datetime(2026, 8, 22, 10, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)  # 10:00-10:05
    motion_windows = [(datetime(2026, 8, 22, 9, 59, 40), datetime(2026, 8, 22, 9, 59, 50))]  # ends 10s before segment starts

    assert not ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=5, post_padding_seconds=5)
    assert ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=15, post_padding_seconds=15)


def test_post_padding_alone_can_rescue_a_later_segment():
    segment_start = datetime(2026, 8, 22, 10, 5, 0)
    segment_end = segment_start + timedelta(seconds=300)  # 10:05-10:10
    motion_windows = [(datetime(2026, 8, 22, 10, 4, 0), datetime(2026, 8, 22, 10, 4, 50))]  # ends 10s before segment starts

    assert not ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=5)
    assert ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=15)


def test_zero_padding_is_a_valid_configuration():
    """Padding is independently configurable per pre/post, including
    down to zero -- an exact-boundary motion event with zero padding
    on the relevant side is not rescued."""
    segment_start = datetime(2026, 8, 22, 10, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)
    motion_windows = [(datetime(2026, 8, 22, 9, 59, 0), datetime(2026, 8, 22, 9, 59, 30))]  # ends 30s before segment
    assert not ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=0)


# ------------------------------------------------------- multiple nearby motion events


def test_multiple_nearby_events_any_single_overlap_is_enough():
    segment_start = datetime(2026, 8, 22, 10, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)
    motion_windows = [
        (datetime(2026, 8, 22, 8, 0, 0), datetime(2026, 8, 22, 8, 0, 5)),      # far earlier, no overlap
        (datetime(2026, 8, 22, 10, 2, 0), datetime(2026, 8, 22, 10, 2, 10)),   # inside the segment
        (datetime(2026, 8, 22, 14, 0, 0), datetime(2026, 8, 22, 14, 0, 5)),    # far later, no overlap
    ]
    assert ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=0, post_padding_seconds=0)


def test_multiple_nearby_events_none_overlapping_is_a_real_skip():
    segment_start = datetime(2026, 8, 22, 10, 0, 0)
    segment_end = segment_start + timedelta(seconds=300)
    motion_windows = [
        (datetime(2026, 8, 22, 8, 0, 0), datetime(2026, 8, 22, 8, 0, 5)),
        (datetime(2026, 8, 22, 14, 0, 0), datetime(2026, 8, 22, 14, 0, 5)),
    ]
    assert not ru._segment_overlaps_motion(segment_start, segment_end, motion_windows, pre_padding_seconds=15, post_padding_seconds=15)


# ==================================================== _load_motion_windows() -- fail-safe file handling


def test_missing_motion_events_file_returns_none(tmp_path):
    assert ru._load_motion_windows(1) is None


def test_healthy_file_with_no_events_for_this_camera_returns_empty_list_not_none(tmp_path):
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=2, start=datetime(2026, 8, 22, 10, 0, 0), end=datetime(2026, 8, 22, 10, 0, 5))
    result = ru._load_motion_windows(1)  # camera 1 has no events, but the file is real and readable
    assert result == []


def test_malformed_json_line_is_skipped_not_fatal_to_the_whole_file(tmp_path):
    ru.MOTION_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ru.MOTION_EVENTS_FILE.open("w", encoding="utf-8") as f:
        f.write("{not valid json at all\n")
        f.write(json.dumps({"camera": 1, "start_time": "2026-08-22T10:00:00", "end_time": "2026-08-22T10:00:05"}) + "\n")
    result = ru._load_motion_windows(1)
    assert result == [(datetime(2026, 8, 22, 10, 0, 0), datetime(2026, 8, 22, 10, 0, 5))]


def test_entirely_unparseable_file_returns_none(tmp_path):
    ru.MOTION_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.MOTION_EVENTS_FILE.write_text("not json\nstill not json\n", encoding="utf-8")
    assert ru._load_motion_windows(1) is None


def test_event_with_missing_timestamp_field_is_skipped_not_fatal():
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=1, start=datetime(2026, 8, 22, 10, 0, 0), end=datetime(2026, 8, 22, 10, 0, 5))
    # A second, malformed event for the same camera missing end_time.
    with ru.MOTION_EVENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"camera": 1, "start_time": "2026-08-22T11:00:00"}) + "\n")
    result = ru._load_motion_windows(1)
    assert result == [(datetime(2026, 8, 22, 10, 0, 0), datetime(2026, 8, 22, 10, 0, 5))]


def test_empty_file_returns_none():
    ru.MOTION_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.MOTION_EVENTS_FILE.write_text("", encoding="utf-8")
    assert ru._load_motion_windows(1) is None


# ==================================================== integration: _pending_recording_files()


def test_continuous_mode_uploads_everything_motion_or_not(tmp_path):
    """Explicit requirement: Continuous cloud plans must be completely
    unaffected. No motion_events.jsonl is even written in this test --
    if Continuous behavior depended on that file at all, this would
    fail with an error, not just a wrong result."""
    _set_camera(1, "continuous")
    ru._cutoff_cache = datetime(2020, 1, 1)
    quiet_segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))

    pending = ru._pending_recording_files(1, set())

    assert quiet_segment in pending


def test_no_explicit_mode_uploads_everything_same_as_continuous(tmp_path):
    """No hidden default: a camera with recording_mode == None (never
    explicitly set) must behave exactly like Continuous, not like
    Motion."""
    _set_camera(1, None)
    ru._cutoff_cache = datetime(2020, 1, 1)
    quiet_segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))

    pending = ru._pending_recording_files(1, set())

    assert quiet_segment in pending


def test_motion_mode_skips_a_segment_with_no_motion(tmp_path):
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    quiet_segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=1, start=datetime(2026, 8, 20, 0, 0, 0), end=datetime(2026, 8, 20, 0, 0, 5))  # unrelated, days earlier

    pending = ru._pending_recording_files(1, set())

    assert quiet_segment not in pending


def test_motion_mode_uploads_a_segment_that_overlaps_real_motion(tmp_path):
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment_start = datetime(2026, 8, 22, 12, 0, 0)
    motion_segment = _make_recording(tmp_path, 1, segment_start)
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=1, start=segment_start + timedelta(seconds=30), end=segment_start + timedelta(seconds=35))

    pending = ru._pending_recording_files(1, set())

    assert motion_segment in pending


def test_motion_mode_only_gates_the_camera_it_applies_to(tmp_path):
    """A motion event on camera 2 must never rescue or affect camera 1's
    segments, and vice versa -- proves the gate is per-camera, not
    global."""
    _set_camera(1, "motion")
    _set_camera(2, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment_start = datetime(2026, 8, 22, 12, 0, 0)
    cam1_segment = _make_recording(tmp_path, 1, segment_start)
    cam2_segment = _make_recording(tmp_path, 2, segment_start)
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=2, start=segment_start + timedelta(seconds=30), end=segment_start + timedelta(seconds=35))

    pending_1 = ru._pending_recording_files(1, set())
    pending_2 = ru._pending_recording_files(2, set())

    assert cam1_segment not in pending_1  # motion was on camera 2, not camera 1
    assert cam2_segment in pending_2


def test_missing_motion_data_fails_safe_uploads_everything(tmp_path):
    """Explicit fail-safe requirement: if motion metadata is
    unavailable, upload rather than silently skip. No
    motion_events.jsonl exists at all here."""
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))

    pending = ru._pending_recording_files(1, set())

    assert segment in pending


def test_corrupt_motion_data_fails_safe_uploads_everything(tmp_path):
    ru.MOTION_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.MOTION_EVENTS_FILE.write_text("completely corrupt, not json\n", encoding="utf-8")
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))

    pending = ru._pending_recording_files(1, set())

    assert segment in pending


def test_fail_safe_never_touches_or_deletes_the_local_file(tmp_path):
    """The explicit "do not silently delete local recordings" fail-safe
    requirement: even under every failure mode above, the local .mkv
    file itself is never removed by _pending_recording_files() --
    it's still sitting on disk exactly where it was."""
    ru.MOTION_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.MOTION_EVENTS_FILE.write_text("garbage\n", encoding="utf-8")
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))

    ru._pending_recording_files(1, set())

    assert segment.exists()


# -------------------------------------------------- restart / idempotency


def test_repeated_calls_give_identical_results_no_persisted_skip_state(tmp_path):
    """No separate "skipped due to no motion" state is ever persisted --
    the decision is a pure function of stable inputs, so calling twice
    (simulating two scan ticks, or a restart between them) must give
    byte-identical results, with no drift or flip-flopping."""
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    quiet_segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 12, 0, 0))
    motion_segment = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 13, 0, 0))
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=1, start=datetime(2026, 8, 22, 13, 0, 30), end=datetime(2026, 8, 22, 13, 0, 35))

    first = ru._pending_recording_files(1, set())
    second = ru._pending_recording_files(1, set())

    assert first == second
    assert quiet_segment not in first and quiet_segment not in second
    assert motion_segment in first and motion_segment in second


def test_a_late_arriving_motion_event_can_still_rescue_an_earlier_unuploaded_segment(tmp_path):
    """Robustness property, not a bug: since nothing is cached, a
    segment that was skipped on an earlier scan (because motion data
    was confirmed-empty for this camera at that point) is picked up
    correctly once a real event for it exists, as long as it's still
    un-uploaded and still exists locally. Starts from a healthy file
    with an unrelated camera's event already in it, so the first scan
    is a real confirmed-no-motion skip, not the missing-file fail-safe
    (that path is covered separately, see
    test_missing_motion_data_fails_safe_uploads_everything)."""
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment_start = datetime(2026, 8, 22, 12, 0, 0)
    segment = _make_recording(tmp_path, 1, segment_start)
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=2, start=datetime(2026, 8, 20, 0, 0, 0), end=datetime(2026, 8, 20, 0, 0, 5))  # unrelated camera, makes the file real/healthy

    first_scan = ru._pending_recording_files(1, set())
    assert segment not in first_scan  # confirmed no motion for camera 1 yet

    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=1, start=segment_start + timedelta(seconds=10), end=segment_start + timedelta(seconds=15))
    second_scan = ru._pending_recording_files(1, set())
    assert segment in second_scan  # now rescued


def test_already_uploaded_segments_are_never_reconsidered_by_the_motion_gate(tmp_path):
    """The existing already_uploaded dedup still fully applies on top of
    the motion gate -- a segment already recorded as uploaded is
    skipped for that reason alone, before the motion gate is even
    consulted, exactly as before this change."""
    _set_camera(1, "motion")
    ru._cutoff_cache = datetime(2020, 1, 1)
    segment_start = datetime(2026, 8, 22, 12, 0, 0)
    segment = _make_recording(tmp_path, 1, segment_start)
    _write_motion_event(ru.MOTION_EVENTS_FILE, camera=1, start=segment_start + timedelta(seconds=10), end=segment_start + timedelta(seconds=15))

    pending = ru._pending_recording_files(1, {segment.name})

    assert segment not in pending  # already uploaded, not re-selected
