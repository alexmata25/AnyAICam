"""Backlog-cutoff safety: tests for _load_or_establish_cutoff() and its
enforcement in _pending_recording_files(). Exists to prevent the exact
failure this was built in response to -- a newly-activated appliance
draining days of pre-existing local recordings the moment upload is
turned on, instead of only ever uploading recordings going forward.

Fast/pure tests only -- no ffmpeg, no AWS, no network. Every test gets
its own isolated CUTOFF_FILE and RECORDINGS_FOLDER via tmp_path, and
resets the module's in-memory cutoff cache/backlog-log-dedup state so
no test can see another's.
"""

from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    yield tmp_path
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()


def _make_recording(tmp_path, camera_number, started_at, *, newest=False):
    """Writes a fake completed-looking recording file. _completed_recording_files()
    always excludes the single newest-named file as still-open, so tests
    that need a file to actually be *eligible* create a companion,
    later-named "still open" file to push the real one out of that slot --
    matching test_recording_uploader.py's own established pattern."""
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{started_at.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = folder / name
    path.write_bytes(b"fake")
    if not newest:
        # A lexicographically-later filename so `path` is not treated as
        # the still-open newest file.
        newest_started = started_at + timedelta(days=3650)
        _make_recording(tmp_path, camera_number, newest_started, newest=True)
    return path


# --------------------------------------------------------- establishment


def test_missing_cutoff_file_establishes_fresh_cutoff_at_now(tmp_path):
    before = datetime.now()
    cutoff = ru._load_or_establish_cutoff()
    after = datetime.now()

    assert before <= cutoff <= after
    assert ru.CUTOFF_FILE.exists()
    import json
    saved = json.loads(ru.CUTOFF_FILE.read_text())
    assert datetime.fromisoformat(saved["cutoff"]) == cutoff


def test_cutoff_is_cached_not_reread_every_call(tmp_path):
    first = ru._load_or_establish_cutoff()
    # Tamper with the on-disk file directly -- if the function actually
    # re-read it, the second call would return a different value.
    ru.CUTOFF_FILE.write_text('{"cutoff": "1999-01-01T00:00:00"}')
    second = ru._load_or_establish_cutoff()
    assert first == second


# --------------------------------------------------------------- restart


def test_restart_reads_existing_cutoff_and_does_not_move_it_forward(tmp_path):
    """The core restart-safety guarantee: a fixed cutoff established at
    first activation must survive a process restart unchanged -- never
    advancing to "now" again, which would silently re-exclude nothing
    new but also prove nothing about correctness; the real risk this
    guards against is a cutoff that *moves*, which could either skip
    real recent recordings (if it jumped forward) or -- unrelated to
    this test but the other failure mode -- re-invite backlog if it
    ever moved backward."""
    original_cutoff = datetime.now() - timedelta(days=10)
    import json
    ru.CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.CUTOFF_FILE.write_text(json.dumps({"cutoff": original_cutoff.isoformat()}))

    # Simulate a fresh process: the only thing that persists across a
    # real restart is the on-disk file, never the in-memory cache.
    ru._cutoff_cache = None

    reloaded = ru._load_or_establish_cutoff()
    assert reloaded == original_cutoff


# ------------------------------------------------------- backlog skipping


def test_recording_before_cutoff_is_skipped(tmp_path):
    cutoff = datetime.now()
    ru._cutoff_cache = cutoff
    old_path = _make_recording(tmp_path, 1, cutoff - timedelta(days=4))

    pending = ru._pending_recording_files(1, set())

    assert old_path not in pending


def test_recording_after_cutoff_is_uploaded(tmp_path):
    cutoff = datetime.now() - timedelta(minutes=1)
    ru._cutoff_cache = cutoff
    new_path = _make_recording(tmp_path, 1, cutoff + timedelta(minutes=5))

    pending = ru._pending_recording_files(1, set())

    assert new_path in pending


def test_mixed_backlog_and_new_recordings_only_new_ones_pass(tmp_path):
    cutoff = datetime.now()
    ru._cutoff_cache = cutoff
    old1 = _make_recording(tmp_path, 1, cutoff - timedelta(days=4), newest=True)
    old2 = _make_recording(tmp_path, 1, cutoff - timedelta(days=2), newest=True)
    new1 = _make_recording(tmp_path, 1, cutoff + timedelta(minutes=5), newest=True)
    # Push the last one out of the "still open" slot.
    _make_recording(tmp_path, 1, cutoff + timedelta(days=3650), newest=True)

    pending = ru._pending_recording_files(1, set())

    assert old1 not in pending
    assert old2 not in pending
    assert new1 in pending


def test_unparseable_filename_is_not_treated_as_backlog(tmp_path):
    """A file the cutoff logic can't even date must fall through to the
    existing unparseable-filename handling in _relay_camera_once(), not
    be silently absorbed into "backlog" and forgotten. _recording_filename_pattern()
    requires the exact digit shape (\\d{4}-\\d{2}-\\d{2}_\\d{2}-\\d{2}-\\d{2}), so
    the realistic unparseable case is a digit-shaped but semantically
    invalid date (month 13, day 99, ...), not an arbitrary string --
    an arbitrary string would fail the filename pattern itself and
    never even become a candidate in the first place."""
    cutoff = datetime.now()
    ru._cutoff_cache = cutoff
    folder = ru._recording_folder(1)
    folder.mkdir(parents=True, exist_ok=True)
    bad = folder / "camera1_2026-13-99_99-99-99.mkv"
    bad.write_bytes(b"fake")
    # Lexicographically-later companion so `bad` isn't excluded as merely
    # "the newest file" -- "9999..." sorts after "2026...".
    (folder / "camera1_9999-01-01_00-00-00.mkv").write_bytes(b"fake")

    pending = ru._pending_recording_files(1, set())

    assert bad in pending  # reaches _relay_camera_once()'s own started_at-is-None handling


def test_pre_cutoff_backlog_is_never_touched_on_disk(tmp_path):
    """Preserve-old-recordings-untouched, verified directly: the file's
    bytes and mtime are identical after being filtered out."""
    cutoff = datetime.now()
    ru._cutoff_cache = cutoff
    old_path = _make_recording(tmp_path, 1, cutoff - timedelta(days=1))
    original_bytes = old_path.read_bytes()
    original_mtime = old_path.stat().st_mtime

    ru._pending_recording_files(1, set())

    assert old_path.exists()
    assert old_path.read_bytes() == original_bytes
    assert old_path.stat().st_mtime == original_mtime


def test_backlog_skip_is_logged_once_not_every_scan(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    cutoff = datetime.now()
    ru._cutoff_cache = cutoff
    _make_recording(tmp_path, 1, cutoff - timedelta(days=1))

    ru._pending_recording_files(1, set())
    ru._pending_recording_files(1, set())
    ru._pending_recording_files(1, set())

    matches = [r for r in caplog.records if "pre_cutoff_backlog_skipped" in r.message]
    assert len(matches) == 1


# ---------------------------------------------------------- no duplicates


def test_already_uploaded_file_excluded_even_when_after_cutoff(tmp_path):
    """Cutoff filtering must never resurrect the pre-existing
    already-uploaded dedup -- a file that's genuinely eligible by date
    but already recorded as uploaded must still never re-upload."""
    cutoff = datetime.now() - timedelta(minutes=1)
    ru._cutoff_cache = cutoff
    new_path = _make_recording(tmp_path, 1, cutoff + timedelta(minutes=5))

    pending = ru._pending_recording_files(1, {new_path.name})

    assert new_path not in pending


def test_remember_uploaded_then_rescan_yields_no_duplicate(tmp_path):
    cutoff = datetime.now() - timedelta(minutes=1)
    ru._cutoff_cache = cutoff
    new_path = _make_recording(tmp_path, 1, cutoff + timedelta(minutes=5))

    first_scan = ru._pending_recording_files(1, set())
    assert new_path in first_scan

    ru._remember_uploaded(1, new_path.name)
    already = set(ru._uploaded_files.get(1, []))
    second_scan = ru._pending_recording_files(1, already)

    assert new_path not in second_scan


# --------------------------------------------- malformed/missing cutoff


def test_corrupt_json_fails_safe_blocks_everything(tmp_path):
    ru.CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.CUTOFF_FILE.write_text("{not valid json")

    cutoff = ru._load_or_establish_cutoff()

    assert cutoff == datetime.max


def test_missing_cutoff_key_fails_safe_blocks_everything(tmp_path):
    ru.CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.CUTOFF_FILE.write_text('{"wrong_key": "2026-01-01T00:00:00"}')

    cutoff = ru._load_or_establish_cutoff()

    assert cutoff == datetime.max


def test_unparseable_timestamp_value_fails_safe_blocks_everything(tmp_path):
    ru.CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.CUTOFF_FILE.write_text('{"cutoff": "not-a-timestamp"}')

    cutoff = ru._load_or_establish_cutoff()

    assert cutoff == datetime.max


def test_corrupt_cutoff_blocks_even_a_brand_new_recording(tmp_path):
    """The real end-to-end proof of fail-safe: with a corrupt cutoff
    file, even a recording that started one second ago is excluded --
    upload is fully paused, not partially/unpredictably applied."""
    ru.CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.CUTOFF_FILE.write_text("garbage")
    very_new = _make_recording(tmp_path, 1, datetime.now() - timedelta(seconds=1))

    pending = ru._pending_recording_files(1, set())

    assert very_new not in pending


def test_corrupt_cutoff_file_is_never_auto_repaired(tmp_path):
    """Fixing a corrupt state file is an operator decision, not
    something this module should silently paper over -- confirms the
    original garbage content is left exactly as-is."""
    ru.CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.CUTOFF_FILE.write_text("garbage")

    ru._load_or_establish_cutoff()

    assert ru.CUTOFF_FILE.read_text() == "garbage"
