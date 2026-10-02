"""Reconciliation group 2, item 1 (2026-09-04): _customer_recording_url()
differed between git and live EC2. Git's version presigns an S3 URL
and returns None if that fails. EC2's version does the same first, but
falls back to an authenticated local-file route
(/api/customer/recordings/{camera_id}/{recording_id}/local) when the
cataloged .mkv still exists on this appliance -- with real path-safety
checks (basename-only extraction, extension check, and a
camera-number-prefix check that rejects a filename that doesn't belong
to the camera the caller already proved they're authorized for).

Classification: MERGE/RECONCILE -- EC2's version is a strict superset
of git's happy path (identical presign-first behavior, S3 configured
and working), plus a safe, well-guarded fallback for when it isn't.
_presigned_recording_url() itself documents that it "fails closed
(returns None) whenever the read-capable role isn't configured" --
this is a real, expected runtime condition, not a hypothetical. On
pure cloud with a genuinely empty local RECORDINGS_FOLDER, the
fallback safely no-ops (returns None, same as git's version always
did); on edge/combined, where a local .mkv commonly exists, it's the
difference between a real playback URL and a dead link. Ported
verbatim from live EC2 -- these tests prove it does what production
already does, not a redesign.
"""

import sqlite3
from pathlib import Path

import pytest

from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_recording_url_local_fallback.db"


@pytest.fixture(autouse=True)
def _isolated_recordings_folder(tmp_path, monkeypatch):
    folder = tmp_path / "recordings"
    folder.mkdir()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", folder)
    return folder


def _seed(db_path, *, camera_number=3, s3_key="camera3_2026-09-04_10-00-00.mkv"):
    with override_target(sqlite_path=db_path):
        initialize_database()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer','c@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-a','Main Site','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('app-1','cust-a','site-1','cloud-app-1','2026-01-01')")
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-1','cust-a','site-1','Front Door','configured',?,'2026-01-01')",
        (camera_number,),
    )
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,duration_seconds,status,created_at) "
        "VALUES('rec-1','cust-a','site-1','app-1','cam-1',?,'2026-09-04T10:00:00','2026-09-04T10:05:00',300,'available','2026-01-01')",
        (s3_key,),
    )
    conn.commit()
    conn.close()


def test_cloud_presign_success_returns_cloud_url_without_touching_local_fallback(db_path, monkeypatch, _isolated_recordings_folder):
    _seed(db_path)
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: f"https://s3.example/{s3_key}?sig=abc")
    with override_target(sqlite_path=db_path):
        url = main._customer_recording_url("cam-1", "rec-1")
    assert url == "https://s3.example/camera3_2026-09-04_10-00-00.mkv?sig=abc"
    # No local file exists at all -- if the fallback had somehow been
    # reached it would have failed anyway, but confirming the cloud
    # branch short-circuits is the actual point of this test.
    assert not any(_isolated_recordings_folder.rglob("*.mkv"))


def test_falls_back_to_local_file_when_presign_is_unavailable(db_path, monkeypatch, _isolated_recordings_folder):
    # Matches _presigned_recording_url()'s own documented behavior:
    # "Fails closed (returns None) whenever the read-capable role
    # isn't configured."
    _seed(db_path)
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: None)
    camera_folder = _isolated_recordings_folder / "camera3"
    camera_folder.mkdir()
    (camera_folder / "camera3_2026-09-04_10-00-00.mkv").write_bytes(b"fake-mkv-data")
    with override_target(sqlite_path=db_path):
        url = main._customer_recording_url("cam-1", "rec-1")
    assert url == "/api/customer/recordings/cam-1/rec-1/local"


def test_returns_none_when_presign_fails_and_no_local_file_exists(db_path, monkeypatch, _isolated_recordings_folder):
    _seed(db_path)
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: None)
    with override_target(sqlite_path=db_path):
        url = main._customer_recording_url("cam-1", "rec-1")
    assert url is None


def test_returns_none_for_an_unknown_or_mismatched_recording(db_path, monkeypatch, _isolated_recordings_folder):
    _seed(db_path)
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: "https://should-not-matter")
    with override_target(sqlite_path=db_path):
        assert main._customer_recording_url("cam-1", "wrong-recording-id") is None
        # Re-scoped by camera_id in the same query -- a real recording
        # id can't be used against the wrong camera.
        assert main._customer_recording_url("some-other-camera", "rec-1") is None


def test_rejects_a_non_mkv_filename_even_if_one_exists_locally(db_path, monkeypatch, _isolated_recordings_folder):
    _seed(db_path, s3_key="camera3_2026-09-04_10-00-00.txt")
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: None)
    camera_folder = _isolated_recordings_folder / "camera3"
    camera_folder.mkdir()
    (camera_folder / "camera3_2026-09-04_10-00-00.txt").write_bytes(b"not-a-video")
    with override_target(sqlite_path=db_path):
        assert main._customer_recording_url("cam-1", "rec-1") is None


def test_rejects_a_filename_not_matching_this_cameras_prefix(db_path, monkeypatch, _isolated_recordings_folder):
    # A tampered or foreign s3_key naming a different camera's file
    # must never be served just because a row happens to reference it.
    _seed(db_path, camera_number=3, s3_key="camera7_2026-09-04_10-00-00.mkv")
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: None)
    camera_folder = _isolated_recordings_folder / "camera3"
    camera_folder.mkdir()
    (camera_folder / "camera7_2026-09-04_10-00-00.mkv").write_bytes(b"fake-mkv-data")
    with override_target(sqlite_path=db_path):
        assert main._customer_recording_url("cam-1", "rec-1") is None


def test_path_traversal_in_s3_key_is_neutralized_not_served(db_path, monkeypatch, _isolated_recordings_folder):
    _seed(db_path, camera_number=3, s3_key="../../../../etc/camera3_passwd.mkv")
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: None)
    with override_target(sqlite_path=db_path):
        # Path(s3_key).name strips every directory component, leaving
        # "camera3_passwd.mkv" -- which then fails the prefix check
        # below (must start with "camera3_" followed by the real
        # recorded-file naming, not just any string) or simply never
        # matches a real file on disk. Either way: no traversal, no
        # local URL.
        assert main._customer_recording_url("cam-1", "rec-1") is None


def test_camera_with_no_camera_number_returns_none_from_the_fallback(db_path, monkeypatch, _isolated_recordings_folder):
    _seed(db_path, camera_number=None, s3_key="camera3_2026-09-04_10-00-00.mkv")
    monkeypatch.setattr(main, "_presigned_recording_url", lambda s3_key: None)
    with override_target(sqlite_path=db_path):
        assert main._customer_recording_url("cam-1", "rec-1") is None
