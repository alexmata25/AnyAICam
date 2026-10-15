"""Regression coverage for the three issues found in the Ryzen VMS 1.2.6
customer acceptance (2026-10-15).

1. Playback cold start: the first /playback load took ~4 minutes because
   the request catalogued the whole uncatalogued backlog, one ffprobe per
   file, oldest first. The request path now has a time budget, works
   newest first, and local_recording_catalog_worker() catalogs the rest.
2. /customer-account on the appliance always redirected the owner to
   /customer/setup: the activation check needs appliance_credentials rows,
   which only the cloud database has.
3. One Playback thumbnail was missing: the clip was 1.65 s long and the
   thumbnail was taken at 3 s, which produces no frame.
"""
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import time as time_module
from datetime import datetime
from types import SimpleNamespace

import pytest

import appliance_activation
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database


# ------------------------------------------------------------ shared seeding
def _seed_tenant(conn, *, cameras=(), appliance_credential=False):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Real Customer','customer@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main Site','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES('appl-1','cust-1','site-1','AIC-C814766E','activated','2026-01-01')")
    if appliance_credential:
        conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,created_by) VALUES('cred-1','appl-1','x','2026-01-01','activation')")
    for number in cameras:
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,device_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"cam-{number}", "cust-1", "site-1", "appl-1", number, f"Camera {number}", "configured", f"urn:uuid:device-{number}", "2026-01-01"),
        )
    conn.commit()


# ------------------------------------------------------------ 1. catalog budget
def _write_recording(folder, camera_number, dt, age_seconds=600):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"camera{camera_number}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path.write_bytes(b"x" * 1024)
    old = time_module.time() - age_seconds
    os.utime(path, (old, old))
    return path


def _catalogued_names(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return sorted(row["s3_key"].rsplit("/", 1)[-1] for row in db.execute("SELECT s3_key FROM recordings").fetchall())


@pytest.fixture()
def catalog_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, cameras=(1, 2))
        conn.close()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    folder = tmp_path / "recordings" / "camera1"
    paths = [_write_recording(folder, 1, datetime(2026, 10, 15, 10, minute, 0)) for minute in range(0, 50, 5)]
    probed = []

    def slow_probe(path):
        probed.append(path.name)
        time_module.sleep(0.05)
        return 300.0

    monkeypatch.setattr(main, "_probe_recording_duration_seconds", slow_probe)
    return db_path, paths, probed


def test_request_path_catalog_stops_at_its_budget_and_catalogs_the_newest_files_first(catalog_setup):
    db_path, paths, probed = catalog_setup
    with override_target(sqlite_path=str(db_path)):
        started = time_module.monotonic()
        added = main._catalog_local_recordings_for_camera("cam-1", budget_seconds=0.12)
        elapsed = time_module.monotonic() - started

    newest_first = sorted((p.name for p in paths), reverse=True)
    assert 1 <= added < len(paths)
    assert probed == newest_first[:added]  # newest first, nothing probed past the budget
    assert _catalogued_names(db_path) == sorted(newest_first[:added])
    assert elapsed < 0.12 + 0.2  # one probe in flight at most past the budget


def test_a_later_request_continues_where_the_budget_stopped(catalog_setup):
    db_path, paths, probed = catalog_setup
    with override_target(sqlite_path=str(db_path)):
        first = main._catalog_local_recordings_for_camera("cam-1", budget_seconds=0.12)
        second = main._catalog_local_recordings_for_camera("cam-1", budget_seconds=0.12)
    assert second >= 1
    assert len(set(probed)) == len(probed) == first + second  # no file probed twice
    assert len(_catalogued_names(db_path)) == first + second


def test_no_budget_catalogs_everything(catalog_setup):
    db_path, paths, probed = catalog_setup
    with override_target(sqlite_path=str(db_path)):
        added = main._catalog_local_recordings_for_camera("cam-1", budget_seconds=None)
    assert added == len(paths)
    assert _catalogued_names(db_path) == sorted(p.name for p in paths)


def test_the_background_pass_catalogs_every_camera_without_a_budget(catalog_setup, tmp_path):
    db_path, paths, probed = catalog_setup
    camera2 = [_write_recording(tmp_path / "recordings" / "camera2", 2, datetime(2026, 10, 15, 11, m, 0)) for m in (0, 5)]
    with override_target(sqlite_path=str(db_path)):
        added = main.local_recording_catalog_pass()
        again = main.local_recording_catalog_pass()
    assert added == len(paths) + len(camera2)
    assert again == 0  # idempotent


def test_request_callers_use_the_default_budget_and_the_worker_runs_on_the_appliance():
    signature = inspect.signature(main._catalog_local_recordings_for_camera)
    assert signature.parameters["budget_seconds"].default == main.PLAYBACK_CATALOG_BUDGET_SECONDS
    assert 0 < main.PLAYBACK_CATALOG_BUDGET_SECONDS <= 10
    source = inspect.getsource(main)
    assert "asyncio.create_task(local_recording_catalog_worker())" in source
    # Every request-path caller relies on the default budget.
    assert source.count("_catalog_local_recordings_for_camera(camera_id)") == 4
    assert "budget_seconds=None" in inspect.getsource(main.local_recording_catalog_pass)


# ------------------------------------------------------------ 2. /customer-account on the appliance
@pytest.fixture()
def account_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db_path = tmp_path / "account.db"
    identity_file = tmp_path / "appliance_identity.json"
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", identity_file)
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as client:
            yield client, db_path, identity_file


def _write_identity(identity_file, customer_id="cust-1"):
    identity_file.write_text(json.dumps({
        "appliance_id": "appl-1", "cloud_id": "AIC-C814766E", "credential": "secret", "customer_id": customer_id,
        "site_id": "site-1", "partner_id": "partner-1", "activated_at": "2026-10-14T00:00:00", "activation_version": 1,
    }), encoding="utf-8")


def _get_account(client):
    cookie = partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)
    return client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: cookie})


def test_on_the_appliance_an_activated_owner_with_cameras_sees_the_account_page(account_client, monkeypatch):
    """The Ryzen case: the appliance is activated for this customer, the
    cameras are synced, and the local DB has no appliance_credentials."""
    client, db_path, identity_file = account_client
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, cameras=(1, 2, 3))
    conn.close()
    _write_identity(identity_file)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")

    response = _get_account(client)
    assert response.status_code == 200, response.headers.get("location")
    assert "Camera 1" in response.text and "Camera 3" in response.text


def test_on_the_appliance_the_owner_is_never_sent_to_the_cloud_setup_wizard(account_client, monkeypatch):
    client, db_path, identity_file = account_client
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)  # no cameras, no slots synced yet
    conn.close()
    _write_identity(identity_file)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")

    response = _get_account(client)
    assert response.status_code == 200
    assert response.headers.get("location") != "/customer/setup"


def test_an_identity_for_another_customer_does_not_count_as_activated(account_client, monkeypatch):
    import partner_workspace

    client, db_path, identity_file = account_client
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, cameras=(1,))
    conn.close()
    _write_identity(identity_file, customer_id="someone-else")
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")

    response = _get_account(client)
    assert response.status_code == 200
    source = inspect.getsource(partner_workspace)
    assert "own.get('customer_id')==customer['id']" in source


def test_in_the_cloud_the_setup_redirect_is_unchanged(account_client, monkeypatch):
    client, db_path, identity_file = account_client
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, cameras=(1,))  # no appliance_credentials row
    conn.close()
    _write_identity(identity_file)  # ignored outside the appliance
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")

    response = _get_account(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/customer/setup"


def test_in_the_cloud_an_activated_owner_still_sees_the_account_page(account_client, monkeypatch):
    client, db_path, identity_file = account_client
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, cameras=(1,), appliance_credential=True)
    conn.close()
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")

    response = _get_account(client)
    assert response.status_code == 200


# ------------------------------------------------------------ 3. thumbnail of a short clip
def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


@pytest.fixture()
def thumbnail_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "thumb.db"
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, cameras=(1,))
    filename = "camera1_2026-10-15_14-23-15.mkv"
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES('rec-short','cust-1','site-1','appl-1','cam-1',?,'2026-10-15T19:23:15','2026-10-15T19:23:17','available','2026-10-15T19:30:00')",
        (f"local/camera1/{filename}",),
    )
    conn.commit()
    conn.close()
    recordings = tmp_path / "recordings"
    (recordings / "camera1").mkdir(parents=True)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: camera_id == "cam-1")
    return db_path, recordings / "camera1" / filename


def test_a_clip_shorter_than_the_seek_point_falls_back_to_its_first_frame(thumbnail_setup, monkeypatch):
    db_path, clip = thumbnail_setup
    clip.write_bytes(b"x" * 1024)
    seeks = []
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] != "ffmpeg":
            return real_run(command, **kwargs)
        seek = command[command.index("-ss") + 1]
        seeks.append(seek)
        if seek == "0":  # past the end of a 1.65 s clip ffmpeg exits 0 and writes nothing
            open(command[-1], "wb").write(b"\xff\xd8jpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with override_target(sqlite_path=str(db_path)):
        response = main.customer_recording_thumbnail("cam-1", "rec-short", _fake_request())
    assert seeks == ["3", "0"]
    assert response.status_code == 200
    assert not list(clip.parent.parent.glob("thumbnails/camera1/*.tmp.jpg"))


def test_a_normal_clip_still_uses_the_3_second_frame_only(thumbnail_setup, monkeypatch):
    db_path, clip = thumbnail_setup
    clip.write_bytes(b"x" * 1024)
    seeks = []

    def fake_run(command, **kwargs):
        seeks.append(command[command.index("-ss") + 1])
        open(command[-1], "wb").write(b"\xff\xd8jpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with override_target(sqlite_path=str(db_path)):
        response = main.customer_recording_thumbnail("cam-1", "rec-short", _fake_request())
    assert seeks == ["3"]
    assert response.status_code == 200


def test_an_unreadable_clip_is_still_a_404(thumbnail_setup, monkeypatch):
    db_path, clip = thumbnail_setup
    clip.write_bytes(b"x" * 1024)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: SimpleNamespace(returncode=1, stderr=b"invalid data"))
    with override_target(sqlite_path=str(db_path)):
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_thumbnail("cam-1", "rec-short", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_real_1_6_second_mkv_gets_a_thumbnail(thumbnail_setup):
    db_path, clip = thumbnail_setup
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
         "-t", "1.65", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(clip)],
        check=True, timeout=60,
    )
    with override_target(sqlite_path=str(db_path)):
        response = main.customer_recording_thumbnail("cam-1", "rec-short", _fake_request())
    assert response.status_code == 200
    thumbnail = clip.parent.parent / "thumbnails" / "camera1" / "camera1_2026-10-15_14-23-15.jpg"
    assert thumbnail.is_file() and thumbnail.stat().st_size > 0
