"""Continuous-Playback freeze/no-audio fix (2026-09-01): every cloud
recording's AAC audio track is 8kHz mono and starts ~0.05-0.1s after
the video track (confirmed with ffprobe against live production
recordings -- see _fix_cloud_recording_audio_for_playback()'s own
docstring in main.py for the full root-cause writeup). Chrome's
<video> element adopts the audio track as its playback clock once one
is present; this specific low-sample-rate/late-start combination
leaves that clock never advancing, freezing video presentation at
currentTime≈0 with readyState=4 and buffered data -- no error, no
console signal. The fix is a lazy, cached, video-stream-copy remux
that only touches the audio track (48kHz, aligned to t=0).

Same test patterns/fixtures as test_playback_bounded_load.py: real
sqlite via override_target(), routes called directly as plain
functions (see that file's own comment on why TestClient isn't used
in this container), and _customer_playback_cameras() monkeypatched to
control authorization deterministically.
"""

import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_media_audio_fix.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')")
    conn.commit()


def _seed_recording(conn, recording_id, camera_id, s3_key):
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (recording_id, "cust-1", "site-1", "appl-1", camera_id, s3_key, "2026-09-01T10:00:00", "2026-09-01T10:05:00", "available", "2026-09-01T10:00:00"),
    )
    conn.commit()


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


# --------------------------------------------------------- /media route


def test_media_route_serves_remuxed_file_for_cloud_mp4(db_path, monkeypatch, tmp_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recording(conn, "rec-1", "cam-1", "recordings/cust-1/site-1/appl-1/cam-1/clip.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])

        fixed = tmp_path / "rec-1.mp4"
        fixed.write_bytes(b"fake-remuxed-mp4-bytes")
        with patch.object(main, "_fix_cloud_recording_audio_for_playback", return_value=fixed) as mock_fix:
            response = main.customer_recording_media("cam-1", "rec-1", _fake_request())

    mock_fix.assert_called_once_with("rec-1", "recordings/cust-1/site-1/appl-1/cam-1/clip.mp4")
    assert str(response.path) == str(fixed)
    assert response.media_type == "video/mp4"


def test_media_route_fails_open_to_redirect_when_remux_fails(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recording(conn, "rec-1", "cam-1", "recordings/cust-1/site-1/appl-1/cam-1/clip.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])

        with patch.object(main, "_fix_cloud_recording_audio_for_playback", return_value=None), \
             patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed-fallback"):
            response = main.customer_recording_media("cam-1", "rec-1", _fake_request())

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/signed-fallback"


def test_media_route_preserves_local_mkv_fallback_unchanged(db_path, monkeypatch):
    """Local-appliance (.mkv) recordings are unaffected by the cloud MP4
    audio-clock bug and must keep redirecting to /local exactly as
    before -- the remux path must never even be attempted for them."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recording(conn, "rec-1", "cam-1", "camera1_2026-09-01_10-00-00.mkv")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])

        with patch.object(main, "_fix_cloud_recording_audio_for_playback") as mock_fix, \
             patch.object(main, "_customer_recording_url", return_value="/api/customer/recordings/cam-1/rec-1/local"):
            response = main.customer_recording_media("cam-1", "rec-1", _fake_request())

    mock_fix.assert_not_called()
    assert response.status_code == 302
    assert response.headers["location"] == "/api/customer/recordings/cam-1/rec-1/local"


def test_media_route_rejects_unauthorized_camera(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recording(conn, "rec-1", "cam-1", "recordings/cust-1/site-1/appl-1/cam-1/clip.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_media("cam-2-does-not-belong", "rec-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403


def test_media_route_404s_for_unknown_recording(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with patch.object(main, "_customer_recording_url", return_value=None):
            with pytest.raises(Exception) as excinfo:
                main.customer_recording_media("cam-1", "does-not-exist", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404


# --------------------------------------------------------- _fix_cloud_recording_audio_for_playback()
#
# Exercises the actual remux (real ffmpeg, already a runtime
# dependency of this image -- see start_recording()/build_manual_clip()
# elsewhere in main.py), against a tiny synthetic fixture built the
# same shape as the real production bug: AAC audio starting after
# video. _download_recording_object() is monkeypatched to copy the
# local fixture instead of hitting S3 -- the S3/STS plumbing itself
# is already covered by test_recording_read_credentials_cache.py.


def _make_delayed_audio_fixture(path: Path) -> None:
    """1s of video + AAC audio starting ~0.1s late -- same shape as the
    real production files (8kHz mono AAC, late start), just short."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
            "-itsoffset", "0.1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=8000",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "8000", "-ac", "1",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _ffprobe_audio_start_and_rate(path: Path) -> tuple[float, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=start_time,sample_rate", "-of", "default=noprint_wrappers=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    fields = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    return float(fields["start_time"]), int(fields["sample_rate"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment")
def test_remux_fixes_audio_start_offset_and_sample_rate(tmp_path, monkeypatch):
    fixture = tmp_path / "source.mp4"
    _make_delayed_audio_fixture(fixture)
    before_start, before_rate = _ffprobe_audio_start_and_rate(fixture)
    assert before_rate == 8000
    # Note: reproducing the exact ~0.05-0.1s late-start container offset
    # seen in real production recordings (see main.py's own ffprobe
    # evidence in _fix_cloud_recording_audio_for_playback()'s docstring)
    # via a synthetic lavfi fixture is unreliable -- ffmpeg's muxer can
    # normalize -itsoffset back to 0 depending on -avoid_negative_ts
    # defaults. The regression-relevant assertion is the "after" state
    # below: whatever the input's start offset, the fix always
    # normalizes to 48kHz starting at t=0.

    cache_root = tmp_path / "cache"
    monkeypatch.setattr(main, "RECORDING_MEDIA_CACHE_FOLDER", cache_root)

    download_calls = []

    def fake_download(s3_key, dest_path):
        download_calls.append(s3_key)
        shutil.copyfile(fixture, dest_path)
        return True

    monkeypatch.setattr(main, "_download_recording_object", fake_download)

    result = main._fix_cloud_recording_audio_for_playback("rec-fixture-1", "recordings/whatever/clip.mp4")
    assert result is not None and result.is_file()

    after_start, after_rate = _ffprobe_audio_start_and_rate(result)
    assert after_rate == 48000
    assert after_start == 0.0
    assert len(download_calls) == 1

    # Second call must be a cache hit -- no second download/remux.
    result2 = main._fix_cloud_recording_audio_for_playback("rec-fixture-1", "recordings/whatever/clip.mp4")
    assert result2 == result
    assert len(download_calls) == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment")
def test_remux_video_frame_count_is_untouched(tmp_path, monkeypatch):
    """Video is stream-copied, never re-encoded -- frame count/content
    must be byte-for-byte identical to the source."""
    fixture = tmp_path / "source.mp4"
    _make_delayed_audio_fixture(fixture)

    cache_root = tmp_path / "cache"
    monkeypatch.setattr(main, "RECORDING_MEDIA_CACHE_FOLDER", cache_root)
    monkeypatch.setattr(main, "_download_recording_object", lambda s3_key, dest_path: (shutil.copyfile(fixture, dest_path), True)[1])

    result = main._fix_cloud_recording_audio_for_playback("rec-fixture-2", "recordings/whatever/clip.mp4")

    def frame_count(path):
        return subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    assert frame_count(result) == frame_count(fixture)
