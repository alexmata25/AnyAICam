"""Customer Playback of the appliance's own (.mkv) recordings, and cloud-only
camera management on the appliance (2026-10-14, local customer access).

Browsers do not play Matroska: the customer's Playback on the appliance got
MEDIA_ERR_SRC_NOT_SUPPORTED for every local recording. A finished segment is
now remuxed to MP4 (video stream copy) through the existing media cache; a
segment still being written keeps the old /local passthrough.
"""
import os
import shutil
import sqlite3
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")


@pytest.fixture()
def playback(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    (recordings / "camera1").mkdir(parents=True)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    monkeypatch.setattr(main, "RECORDING_MEDIA_CACHE_FOLDER", recordings / "_media_cache")
    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: camera_id == "cam-1")
    db_path = tmp_path / "edge.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        import partner_portal
        with TestClient(main.app, follow_redirects=False) as client:
            client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None))
            yield client, db_path, recordings


def _segment(recordings, db_path, name, *, age_seconds):
    path = recordings / "camera1" / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10", "-t", "2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) "
                   "VALUES('cam-1','cust-1','site-1','appl-1','Garage',1,'2026-10-14')")
        db.execute("INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,duration_seconds,"
                   "size_bytes,status,created_at) VALUES(?,'cust-1','site-1','appl-1','cam-1',?,'2026-10-14T10:00:00',"
                   "'2026-10-14T10:00:02',2,1,'available','2026-10-14')",
                   (name.split(".")[0], f"camera1/{name}"))
    return name.split(".")[0]


def test_a_finished_local_mkv_recording_is_served_as_playable_mp4(playback):
    client, db_path, recordings = playback
    rec = _segment(recordings, db_path, "camera1_2026-10-14_10-00-00.mkv", age_seconds=600)
    response = client.get(f"/api/customer/recordings/cam-1/{rec}/media")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    out = next((recordings / "_media_cache").glob(f"local-{rec}-*.mp4"))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=codec_name", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True).stdout
    assert "mp4" in probe and "h264" in probe
    assert client.get(f"/api/customer/recordings/cam-1/{rec}/media").status_code == 200  # cached


def test_a_segment_still_being_written_keeps_the_local_passthrough(playback):
    client, db_path, recordings = playback
    rec = _segment(recordings, db_path, "camera1_2026-10-14_10-05-00.mkv", age_seconds=0)
    response = client.get(f"/api/customer/recordings/cam-1/{rec}/media")
    assert response.status_code == 302 and response.headers["location"].endswith(f"/{rec}/local")
    assert not list((recordings / "_media_cache").glob(f"local-{rec}-*.mp4"))


def test_another_cameras_recording_is_still_refused(playback):
    client, db_path, recordings = playback
    assert client.get("/api/customer/recordings/cam-2/anything/media").status_code == 403


def test_removing_a_camera_is_refused_on_the_appliance(playback, monkeypatch):
    client, _, _ = playback
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    import partner_portal
    token = partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)
    from token_security import sign
    csrf = sign("csrf", 28800)
    client.cookies.set("anyaicam_csrf", csrf)
    response = client.delete("/api/customer/cameras/cam-1", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409 and "AnyAiCam portal" in response.json()["detail"]  # never a local-only removal



def test_remux_is_bounded_and_busy_requests_get_503(playback, monkeypatch):
    import threading
    client, db_path, recordings = playback
    rec = _segment(recordings, db_path, "camera1_2026-10-14_10-10-00.mkv", age_seconds=600)
    monkeypatch.setattr(main, "LOCAL_RECORDING_REMUX_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(main, "LOCAL_RECORDING_REMUX_WAIT_SECONDS", 0.1)
    main.LOCAL_RECORDING_REMUX_SLOTS.acquire()  # another remux is running
    try:
        busy = client.get(f"/api/customer/recordings/cam-1/{rec}/media")
        assert busy.status_code == 503 and busy.headers.get("retry-after") == "5"
    finally:
        main.LOCAL_RECORDING_REMUX_SLOTS.release()
    assert client.get(f"/api/customer/recordings/cam-1/{rec}/media").status_code == 200


def test_a_writer_that_resumes_never_leaves_a_stale_cached_mp4(playback):
    client, db_path, recordings = playback
    name = "camera1_2026-10-14_10-20-00.mkv"
    rec = _segment(recordings, db_path, name, age_seconds=600)
    assert client.get(f"/api/customer/recordings/cam-1/{rec}/media").status_code == 200
    first = sorted(p.name for p in (recordings / "_media_cache").glob("local-*.mp4"))
    source = recordings / "camera1" / name  # the writer resumed: the file changed size and mtime
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10", "-t", "4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    old = time.time() - 600
    os.utime(source, (old, old))
    assert client.get(f"/api/customer/recordings/cam-1/{rec}/media").status_code == 200
    second = sorted(p.name for p in (recordings / "_media_cache").glob("local-*.mp4"))
    assert len(second) == 2 and first[0] in second  # a new, matching copy; never the stale one for the new source


def test_all_cloud_account_camera_and_appliance_changes_are_refused_on_the_appliance(playback, monkeypatch):
    client, _, _ = playback
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    from token_security import sign
    csrf = sign("csrf", 28800)
    client.cookies.set("anyaicam_csrf", csrf)
    h = {"X-CSRF-Token": csrf}
    for method, path in (("post", "/api/customer/appliances/link"), ("post", "/api/customer/appliances/provision"),
                         ("post", "/api/customer/appliances/appl-1/scan"), ("post", "/api/customer/appliances/appl-1/activation-token/recover"),
                         ("post", "/api/customer/cameras/provision"), ("put", "/api/customer/cameras"),
                         ("delete", "/api/customer/cameras/cam-1"), ("put", "/api/customer/cameras/1/name")):
        kwargs = {"headers": h} if method == "delete" else {"headers": h, "json": {}}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 409, (method, path, response.status_code, response.text[:120])
