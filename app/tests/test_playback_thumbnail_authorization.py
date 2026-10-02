"""Customer Playback thumbnail retrieval (2026-09-14 Playback phase).

Inspection finding: GET /api/customer/recordings/{camera_id}/{recording_
id}/thumbnail (main.py's customer_recording_thumbnail()) was already a
complete, correct implementation before this phase touched anything --
for a cloud (S3 .mp4) recording it derives the companion thumbnail's
S3 key by replacing the .mp4 extension with .jpg (exactly the key
recording_uploader.py's own _create_recording_thumbnail()/upload step
already writes alongside every recording's own clip -- see that
module's docstring), then 302-redirects to a freshly presigned GET URL
via the same read-only recording-read role every other Playback media
route already uses. It had simply never had a single dedicated test of
its own -- this file is that coverage, not a rewrite. See
docs/PROJECT_CHECKPOINT.md's matching dated entry for the real,
already-uploaded S3 object this was additionally validated against.

Same fixtures/pattern as test_playback_bounded_load.py.
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
    return tmp_path / "test_thumbnail_authorization.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-2','cust-1','site-1','appl-1',2,'Back Yard','2026-01-01')")
    conn.commit()


def _insert_recording(conn, rec_id, camera_id, camera_number, filename):
    s3_key = f"recordings/cust-1/site-1/appl-1/{camera_id}/2026/08/20/events/{filename}"
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'available',?)",
        (rec_id, "cust-1", "site-1", "appl-1", camera_id, s3_key, "2026-08-20T00:00:00", "2026-08-20T00:04:59", "2026-08-20T00:00:00"),
    )
    conn.commit()


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


@pytest.fixture(autouse=True)
def _reset_presigned_url_cache():
    # Hybrid transfer-cost audit (docs/hybrid-transfer-cost-reduction-
    # audit.md): customer_recording_thumbnail() now goes through the
    # shared, module-level presigned-URL reuse cache
    # (_presigned_recording_url_and_ttl()'s own cache) -- reset it
    # around every test in this file so one test's s3_key can never
    # leak a cached URL/failure into another.
    main._presigned_url_cache = {}
    yield
    main._presigned_url_cache = {}


def test_cloud_recording_thumbnail_redirects_to_a_presigned_jpg_derived_from_the_mp4_key(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _insert_recording(conn, "rec-1", "cam-1", 1, "camera1_2026-08-20_00-00-00.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        captured = {}

        def fake_presign(key):
            captured["key"] = key
            return "https://example.com/signed-thumbnail"

        # customer_recording_thumbnail() now calls
        # _cacheable_presigned_redirect(), which goes through
        # _presigned_recording_url_and_ttl() -> _generate_presigned_
        # recording_url() on a cache miss -- patch the actual signing
        # function, not the now-bypassed _presigned_recording_url()
        # wrapper.
        with patch.object(main, "_generate_presigned_recording_url", side_effect=fake_presign):
            response = main.customer_recording_thumbnail("cam-1", "rec-1", _fake_request())
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/signed-thumbnail"
    assert response.headers["cache-control"] == "private, max-age=300"
    assert captured["key"].endswith("camera1_2026-08-20_00-00-00.jpg")
    assert "camera1_2026-08-20_00-00-00.mp4" not in captured["key"]


def test_thumbnail_route_rejects_a_camera_the_customer_is_not_authorized_for(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _insert_recording(conn, "rec-1", "cam-1", 1, "camera1_2026-08-20_00-00-00.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-2", "name": "Back Yard", "camera_number": 2}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_thumbnail("cam-1", "rec-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403


def test_thumbnail_route_404s_when_the_recording_id_belongs_to_a_different_camera(db_path, monkeypatch):
    """The exact cross-camera-leak guard test_playback_bounded_load.py
    already proves for the /url route -- a real recording id, but
    requested via the wrong camera_id in the URL, must never resolve,
    even for a camera the customer IS separately authorized for."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _insert_recording(conn, "rec-1", "cam-1", 1, "camera1_2026-08-20_00-00-00.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
            {"id": "cam-1", "name": "Front Door", "camera_number": 1},
            {"id": "cam-2", "name": "Back Yard", "camera_number": 2},
        ])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_thumbnail("cam-2", "rec-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404


def test_thumbnail_route_404s_for_an_unknown_recording_id(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_thumbnail("cam-1", "does-not-exist", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404


def test_thumbnail_route_404s_when_the_presign_fails_rather_than_exposing_any_credential_or_key(db_path, monkeypatch):
    """When the underlying S3 object genuinely doesn't exist (or the
    presign call fails for any reason), the customer gets a clean 404
    -- never a raw S3 key, bucket name, or credential of any kind."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _insert_recording(conn, "rec-1", "cam-1", 1, "camera1_2026-08-20_00-00-00.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with patch.object(main, "_generate_presigned_recording_url", return_value=None):
            with pytest.raises(Exception) as excinfo:
                main.customer_recording_thumbnail("cam-1", "rec-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404
    assert "s3" not in str(getattr(excinfo.value, "detail", "")).lower()
    assert "aws" not in str(getattr(excinfo.value, "detail", "")).lower()


def test_thumbnail_route_404s_for_a_filename_that_does_not_match_this_recordings_own_camera_number(db_path, monkeypatch):
    """Defense in depth against a corrupted/spoofed s3_key: the
    filename must start with camera{camera_number}_ for the row's OWN
    camera_number, matching the same guard the route already applies
    before ever deriving a thumbnail key from it."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        # camera_number=1 for cam-1, but the stored s3_key's filename claims camera9.
        _insert_recording(conn, "rec-1", "cam-1", 1, "camera9_2026-08-20_00-00-00.mp4")
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recording_thumbnail("cam-1", "rec-1", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 404
