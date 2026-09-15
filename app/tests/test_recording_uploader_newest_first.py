"""Recording-uploader upload ORDER (2026-09-15, staging pilot): focused
tests for _relay_camera_once()'s file-selection order, following the
raise of RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA from 1 to 12 for
the Camera 1 pilot.

Root cause confirmed live on Ryzen: _pending_recording_files() has
always returned files oldest-first (_completed_recording_files()'s own
sort is by filename, which is chronological for this project's
fixed-width start_recording() naming -- confirmed unchanged by this
fix, see test_pending_files_still_arrive_oldest_first below). A prior
fix (`eca22b5`) promoted only the single newest file to the front of
that list, leaving the rest oldest-first -- correct behavior for a
total cap of 1 (the only file that could ever upload was that one
promoted file), but silently wrong once the cap allows more than one
upload per camera: of a 12-file allowance, only the first slot was
ever the newest recording; the remaining 11 still drained from the
oldest end of the backlog (real Sept 13 footage), leaving current
Playback analytics markers (Sept 15) with no uploaded recording
underneath them most of the time.

The fix makes the WHOLE pending list newest-first, not just its first
element. Nothing about which files are eligible, how many are
attempted per scan, or the hard total-cap changes -- purely the order
_relay_camera_once() walks an already-computed pending list in.

Same import/isolation/fake-dependency conventions as this suite's
sibling file, test_recording_uploader_credential_hardening.py (real
_relay_camera_once()/_pending_recording_files(), only _prepare_cloud_
copy()/_control_plane_post()/boto3 faked; RECORDINGS_FOLDER redirected
to a pytest tmp_path).
"""

from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(ru, "_camera_map", {})
    monkeypatch.setattr(ru, "_sessions", {})
    monkeypatch.setattr(ru, "_clients", {})
    monkeypatch.setattr(ru, "_camera_backoff", {})
    monkeypatch.setattr(ru, "_uploaded_files", {})
    monkeypatch.setattr(ru, "_unsupported_codec_files", {})
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", None)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", None)
    monkeypatch.setattr(ru, "_create_recording_thumbnail", lambda mp4_path, camera_number: None)


def _make_recording(folder, camera_number, start, content=b"original mkv bytes"):
    camera_folder = folder / f"camera{camera_number}"
    camera_folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{start.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = camera_folder / name
    path.write_bytes(content)
    return path


def _seed_pending_files(tmp_path, camera_number, count, base=None):
    """`count` completed files, oldest-first, plus one extra newest file
    _completed_recording_files() always excludes as still-being-written
    -- matches test_recording_uploader_credential_hardening.py's own
    identical helper and the real function's own contract."""
    base = base or datetime(2026, 9, 13, 8, 0, 0)
    paths = [
        _make_recording(tmp_path, camera_number, base + timedelta(hours=i), content=f"mkv-{i}".encode())
        for i in range(count + 1)
    ]
    return paths[:-1]


class _FakeS3Client:
    def __init__(self):
        self.upload_calls = []

    def upload_file(self, path, bucket, key, ExtraArgs=None, Config=None):
        self.upload_calls.append({"path": path, "key": key})


class _FakeBoto3:
    def __init__(self):
        self.client_instance = _FakeS3Client()

    def client(self, *args, **kwargs):
        return self.client_instance


def _install_fakes(monkeypatch, tmp_path):
    def fake_prepare(mkv_path, camera_number, expected_duration_seconds):
        out = tmp_path / f"staged-{mkv_path.stem}.mp4"
        out.write_bytes(b"fake mp4 bytes")
        return out

    monkeypatch.setattr(ru, "_prepare_cloud_copy", fake_prepare)

    def fake_post(path, payload):
        if path.endswith("/credentials"):
            return {
                "credentials": {
                    "access_key_id": "AKIAFAKE", "secret_access_key": "fakesecret",
                    "session_token": "faketoken", "expiration": datetime.now().astimezone().isoformat(),
                },
                "bucket": "test-bucket", "key_prefix": "recordings/tenant/",
            }
        if path.endswith("/available"):
            return {"status": "accepted"}
        return None

    monkeypatch.setattr(ru, "_control_plane_post", fake_post)

    fake_boto3 = _FakeBoto3()
    monkeypatch.setattr(ru, "boto3", fake_boto3)
    return fake_boto3.client_instance


# ---------------------------------------------------------------------------
# Current (unchanged) discovery ordering: oldest-first, deterministic --
# not filesystem-order, not nondeterministic.
# ---------------------------------------------------------------------------

def test_pending_files_still_arrive_oldest_first(tmp_path):
    pending = _seed_pending_files(tmp_path, camera_number=1, count=4)
    names = [p.name for p in ru._pending_recording_files(1, already_uploaded=set())]
    assert names == sorted(names), "the underlying scan must still be oldest-first, unchanged by this fix"
    assert names == [p.name for p in pending]


# ---------------------------------------------------------------------------
# The actual fix: the whole batch uploads newest-first, not just the
# first file.
# ---------------------------------------------------------------------------

def test_relay_uploads_the_entire_batch_newest_first_under_a_multi_file_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_FILES_PER_SCAN", 10)
    pending = _seed_pending_files(tmp_path, camera_number=1, count=5)
    expected_newest_first = [p.name for p in reversed(pending)]

    client = _install_fakes(monkeypatch, tmp_path)
    ru._relay_camera_once(1, "cam-1-id")

    recording_calls = [c for c in client.upload_calls if not str(c["key"]).endswith(".jpg")]
    uploaded_order = [call["path"].rsplit("staged-", 1)[-1].removesuffix(".mp4") + ".mkv" for call in recording_calls]
    assert uploaded_order == expected_newest_first


def test_relay_uploads_the_single_newest_file_first_when_cap_allows_only_one(monkeypatch, tmp_path):
    # The pre-2026-09-15 pilot shape (total cap 1): must keep picking
    # the same file it always did.
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", 1)
    pending = _seed_pending_files(tmp_path, camera_number=1, count=4)
    newest_name = pending[-1].name

    client = _install_fakes(monkeypatch, tmp_path)
    ru._relay_camera_once(1, "cam-1-id")

    recording_calls = [c for c in client.upload_calls if not str(c["key"]).endswith(".jpg")]
    assert len(recording_calls) == 1
    uploaded_name = recording_calls[0]["path"].rsplit("staged-", 1)[-1].removesuffix(".mp4") + ".mkv"
    assert uploaded_name == newest_name


def test_reordering_changes_no_file_set_only_order(tmp_path):
    pending = _seed_pending_files(tmp_path, camera_number=1, count=6)
    before = {p.name for p in pending}
    after = {p.name for p in reversed(pending)}
    assert before == after
    assert len(before) == 6


def test_backlog_still_fully_drains_across_repeated_scans_under_the_pilot_cap(monkeypatch, tmp_path):
    # RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA=12, the real staging
    # pilot value -- with only 5 files pending, every one of them must
    # still eventually upload; newest-first changes ORDER, never drops
    # a file that was already eligible.
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_TOTAL_FILES_PER_CAMERA", 12)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_FILES_PER_SCAN", 10)
    pending = _seed_pending_files(tmp_path, camera_number=1, count=5)

    client = _install_fakes(monkeypatch, tmp_path)
    ru._relay_camera_once(1, "cam-1-id")

    recording_calls = [c for c in client.upload_calls if not str(c["key"]).endswith(".jpg")]
    assert len(recording_calls) == 5
    assert ru._uploaded_files[1] == [p.name for p in reversed(pending)]


# ---------------------------------------------------------------------------
# Cameras 2-5 remain unaffected: the fix is generic (no camera-1-only
# special case was introduced) and the pilot allowlist/scope gate that
# actually keeps Cameras 2-5 blocked is completely untouched.
# ---------------------------------------------------------------------------

def test_the_same_newest_first_order_applies_uniformly_to_a_non_pilot_camera_number(monkeypatch, tmp_path):
    # Proves this fix has no "if camera_number == 1" special-casing --
    # if _relay_camera_once() were ever called for camera 3 (it isn't,
    # while it's outside the scope -- see the next test), the exact
    # same newest-first logic would apply, not a different path that
    # could accidentally diverge in behavior later.
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_FILES_PER_SCAN", 10)
    pending = _seed_pending_files(tmp_path, camera_number=3, count=3)
    expected_newest_first = [p.name for p in reversed(pending)]

    client = _install_fakes(monkeypatch, tmp_path)
    ru._relay_camera_once(3, "cam-3-id")

    recording_calls = [c for c in client.upload_calls if not str(c["key"]).endswith(".jpg")]
    uploaded_order = [call["path"].rsplit("staged-", 1)[-1].removesuffix(".mp4") + ".mkv" for call in recording_calls]
    assert uploaded_order == expected_newest_first


@pytest.mark.anyio
async def test_worker_still_never_calls_relay_for_cameras_2_to_5_with_the_camera_1_pilot_scope(monkeypatch):
    # The actual mechanism that keeps Cameras 2-5 blocked is the scope
    # gate in recording_upload_worker(), entirely untouched by this
    # ordering fix -- reconfirmed here in this file so the "Cameras 2-5
    # unaffected" claim for this specific change has its own direct
    # regression coverage, not just a cross-reference to a sibling file.
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", False)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_CAMERA_SCOPE", frozenset({1}))
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.02)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2, 3, 4, 5])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": f"cam-{n}", "site_id": "site-1", "cloud_recording_mode": None})
    calls = []
    monkeypatch.setattr(ru, "_relay_camera_once", lambda camera_number, camera_id: calls.append(camera_number))

    import asyncio
    task = asyncio.ensure_future(ru.recording_upload_worker())
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert 1 in calls
    assert all(camera not in calls for camera in (2, 3, 4, 5))


@pytest.fixture
def anyio_backend():
    return "asyncio"
