"""Real bug found via e2e browser testing against real staging data
(2026-09-15): _customer_playback_cameras()'s ORDER BY camera_number, id
sorts an un-provisioned camera (camera_number IS NULL -- a
pending_installation placeholder with no device, no recordings, and
never any) BEFORE every genuinely provisioned one, because SQLite (and
Postgres) both sort NULL first in ascending order. _render_customer_
playback() uses this list's own first row (cameras[0]) as the default
camera whenever a customer lands on /playback with no ?camera= deep
link -- confirmed live against the real pilot customer: their default
camera was "Camera 8" (a placeholder), timeline permanently empty,
while all 5 of their real cameras (1-5) sat further down the list.

Fix: `ORDER BY camera_number IS NULL, camera_number, id` -- provisioned
cameras first, placeholders last. Does not remove or hide placeholder
cameras (still present, still selectable, same total count) -- only
changes which one sorts first.
"""
import sqlite3

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_playback_camera_default_selection_order.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.commit()


def _seed_camera(conn, camera_id, name, camera_number):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (camera_id, "cust-1", "site-1", "appl-1", camera_number, name, "2026-01-01"),
    )
    conn.commit()


def _owner_request():
    from types import SimpleNamespace
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def test_a_provisioned_camera_sorts_before_placeholder_cameras_seeded_first(db_path, monkeypatch):
    # Seeds the placeholder rows FIRST (lower rowid/insertion order) and
    # the real camera LAST, so this test cannot pass by accident just
    # because of insertion order -- only the ORDER BY fix itself can
    # make this pass.
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-placeholder-6", "Camera 6", None)
        _seed_camera(conn, "cam-placeholder-7", "Camera 7", None)
        _seed_camera(conn, "cam-placeholder-8", "Camera 8", None)
        _seed_camera(conn, "cam-real-1", "Camera 1", 1)

        import partner_portal
        monkeypatch.setattr(
            partner_portal, "partner_identity",
            lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"},
        )
        cameras = main._customer_playback_cameras(_owner_request())

    assert cameras[0]["id"] == "cam-real-1", (
        f"expected the real, provisioned camera first; got {cameras[0]!r} -- "
        "the exact live regression this test locks in"
    )
    placeholder_ids = {"cam-placeholder-6", "cam-placeholder-7", "cam-placeholder-8"}
    assert {c["id"] for c in cameras[1:]} == placeholder_ids, "placeholders must still all be present, just sorted after"


def test_multiple_provisioned_cameras_still_sort_numerically_among_themselves(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-placeholder", "Camera 9", None)
        _seed_camera(conn, "cam-real-3", "Camera 3", 3)
        _seed_camera(conn, "cam-real-1", "Camera 1", 1)
        _seed_camera(conn, "cam-real-2", "Camera 2", 2)

        import partner_portal
        monkeypatch.setattr(
            partner_portal, "partner_identity",
            lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"},
        )
        cameras = main._customer_playback_cameras(_owner_request())

    assert [c["id"] for c in cameras] == ["cam-real-1", "cam-real-2", "cam-real-3", "cam-placeholder"]


def test_customer_viewer_role_gets_the_same_nulls_last_ordering(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-placeholder", "Camera 6", None)
        _seed_camera(conn, "cam-real-1", "Camera 1", 1)
        conn.execute(
            "INSERT INTO partner_users(id,customer_id,email,role,password_hash,created_at) "
            "VALUES('user-1','cust-1','viewer@example.test','customer_viewer','x','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES('user-1','cam-placeholder',1)"
        )
        conn.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES('user-1','cam-real-1',1)"
        )
        conn.commit()

        import partner_portal
        monkeypatch.setattr(
            partner_portal, "partner_identity",
            lambda request: {"role": "customer_viewer", "customer_id": "cust-1", "email": "viewer@example.test"},
        )
        cameras = main._customer_playback_cameras(_owner_request())

    assert cameras[0]["id"] == "cam-real-1"


def test_no_placeholder_cameras_is_unaffected(db_path, monkeypatch):
    """Regression lock in the other direction: a tenant with only real,
    numbered cameras (no placeholders at all) sees no behavior change."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-real-2", "Camera 2", 2)
        _seed_camera(conn, "cam-real-1", "Camera 1", 1)

        import partner_portal
        monkeypatch.setattr(
            partner_portal, "partner_identity",
            lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.test"},
        )
        cameras = main._customer_playback_cameras(_owner_request())

    assert [c["id"] for c in cameras] == ["cam-real-1", "cam-real-2"]
