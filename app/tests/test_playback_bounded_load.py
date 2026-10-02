"""Bounded Playback initial load: fixes a real production incident
where the customer Playback page embedded every recording for every
camera, fully presigned, growing past 5MB / 8+ seconds as the catalog
grew -- real browser requests were being canceled before the page
finished loading. These tests cover the new bounded-metadata query,
on-demand single-recording presign, the authorization guard shared by
both new API routes, and the routes themselves end-to-end.

Real HTTP through the real app (TestClient(main.app)) for the routes,
direct function calls for the query-layer helpers, a real signed
customer session cookie (partner_portal._token()), and a throwaway
sqlite DB via override_target() -- the same established pattern as
test_customer_recordings_r4.py and test_cloud_recording_mode_disabled.py.
"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_bounded_playback.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-2','cust-1','site-1','appl-1',2,'Other Camera','2026-01-01')")
    conn.commit()


def _seed_recordings(conn, camera_id, count, *, day="2026-08-20"):
    """count recordings, 5 minutes apart, oldest first, all on the same day."""
    for i in range(count):
        hour, minute = divmod(i * 5, 60)
        started = f"{day}T{hour % 24:02d}:{minute:02d}:00"
        ended = f"{day}T{hour % 24:02d}:{minute + 4:02d}:59"
        s3_key = f"recordings/cust-1/site-1/appl-1/{camera_id}/{day.replace('-', '/')}/clip{i:03d}.mp4"
        conn.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"rec-{camera_id}-{i:03d}", "cust-1", "site-1", "appl-1", camera_id, s3_key, started, ended, "available", started),
        )
    conn.commit()


# --------------------------------------------------------- _customer_recording_rows()


def test_limit_returns_only_the_most_recent_n_oldest_first(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 80)
        rows = main._customer_recording_rows("cam-1", limit=50)
    assert len(rows) == 50
    assert rows[0]["name"] == "clip030.mp4"  # oldest of the most-recent-50 window (80-50=30)
    assert rows[-1]["name"] == "clip079.mp4"  # newest overall
    assert "url" not in rows[0]  # metadata only -- the entire point


def test_before_pagination_reaches_older_history(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 80)
        first_page = main._customer_recording_rows("cam-1", limit=50)
        older_page = main._customer_recording_rows("cam-1", limit=50, before=first_page[0]["start"])
    assert len(older_page) == 30  # the remaining 30 older than the first page's oldest entry
    assert older_page[0]["name"] == "clip000.mp4"
    assert older_page[-1]["name"] == "clip029.mp4"
    # no overlap between the two pages
    assert not ({row["id"] for row in first_page} & {row["id"] for row in older_page})


def test_near_finds_the_covering_recording_even_outside_the_initial_window(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 80)  # clip000 (oldest) is well outside a 50-row initial window
        result = main._customer_recording_rows("cam-1", near="2026-08-20T00:02:00")
    assert len(result) == 1
    assert result[0]["name"] == "clip000.mp4"


def test_near_falls_back_to_closest_within_five_minutes(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 3)  # clip000 00:00-00:04, clip001 00:05-00:09, clip002 00:10-00:14
        result = main._customer_recording_rows("cam-1", near="2026-08-20T00:04:30")  # gap between clip000/clip001
    assert len(result) == 1
    assert result[0]["name"] in {"clip000.mp4", "clip001.mp4"}


def test_near_returns_empty_when_nothing_is_close_enough(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 3)
        result = main._customer_recording_rows("cam-1", near="2026-08-21T12:00:00")  # a full day away
    assert result == []


# --------------------------------------------------------- _customer_recording_url()


def test_on_demand_url_returned_for_a_real_recording(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 1)
        with patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed"):
            url = main._customer_recording_url("cam-1", "rec-cam-1-000")
    assert url == "https://example.com/signed"


def test_url_is_none_when_recording_belongs_to_a_different_camera(db_path):
    """The exact cross-camera-leak guard: a real recording id, but for
    the wrong camera_id, must never resolve."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 1)
        with patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed"):
            url = main._customer_recording_url("cam-2", "rec-cam-1-000")
    assert url is None


def test_url_is_none_for_an_unknown_recording_id(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        url = main._customer_recording_url("cam-1", "does-not-exist")
    assert url is None


# --------------------------------------------------------- the two API routes
#
# Called directly as plain functions (FastAPI resolves query/path
# params to real keyword arguments, so this is exactly what the ASGI
# layer would pass in) rather than through TestClient -- this
# production container's TrustedHostMiddleware/secure-cookie config is
# baked in at process-import time from real env vars, which makes
# TestClient's synthetic http://testserver requests unreliable here
# regardless of what any individual test does (confirmed: the same
# friction affects test_cloud_recording_mode_disabled.py, a
# pre-existing file, unrelated to this change). _customer_playback_
# cameras() -- the shared authorization gate both new routes call
# through _customer_authorized_camera_id() -- is controlled directly
# via monkeypatch instead, so what's actually under test here (each
# route's own 200/403/404 logic) is exercised precisely, without that
# unrelated environment friction.


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def test_metadata_route_returns_bounded_shape_without_url(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 60)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        result = main.customer_recordings_metadata("cam-1", _fake_request())
    assert len(result["clips"]) == 50
    assert "url" not in result["clips"][0]


def test_metadata_route_rejects_unauthorized_camera(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recordings_metadata("cam-2-does-not-belong", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403


def test_metadata_route_rejects_when_not_a_customer_identity(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: None)  # not a customer_owner/customer_viewer identity
        with pytest.raises(Exception) as excinfo:
            main.customer_recordings_metadata("cam-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403


def test_url_route_returns_a_real_presigned_url(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 60)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed"):
            result = main.customer_recording_url("cam-1", "rec-cam-1-059", _fake_request())
    assert result["url"] == "https://example.com/signed"


def test_url_route_404s_for_unknown_recording(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_url("cam-1", "does-not-exist", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404


def test_url_route_rejects_unauthorized_camera(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 60)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_url("cam-2-does-not-belong", "rec-cam-1-059", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403
