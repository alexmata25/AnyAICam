"""Regression coverage for a real bug found 2026-09-17 while building the
can_unlock viewer-permission management UI: camera_access.py's
set_camera_access() unconditionally DELETEd and re-INSERTed every
customer_camera_permissions row for a 'selected'-mode user on every call,
and the INSERT never carried can_settings/can_talk/can_unlock -- so those
three columns silently reset to 0 (SQLite's own column default) the
moment an owner changed ANYTHING about a viewer's camera list, even for a
camera completely unrelated to the change. A viewer granted can_unlock on
camera 1 would silently lose it the next time the owner merely added or
removed camera 2 from their list.

Real SQLite fixtures via database_backend.override_target(), matching
this codebase's established convention (see test_door_access.py)."""

import pytest

import camera_access
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_access.db"


def _seed(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A','2026-01-01')")
    for cam_id, number in (("cam-1", 1), ("cam-2", 2), ("cam-3", 3)):
        conn.execute(
            "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (cam_id, "cust-a", "site-a", "appl-a", number, cam_id, "2026-01-01"),
        )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('viewer-1','viewer@example.test','customer_viewer','cust-a','x','selected','2026-01-01')"
    )
    conn.commit()


@pytest.fixture()
def db(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database, connection
        initialize_database()
        with connection() as conn:
            _seed(conn)
        with connection() as conn:
            yield conn


def _grant(db, camera_id, **cols):
    row = db.execute(
        "SELECT can_settings,can_talk,can_unlock FROM customer_camera_permissions WHERE user_id='viewer-1' AND camera_id=?",
        (camera_id,),
    ).fetchone()
    return dict(row) if row else None


def test_extra_grants_survive_an_unrelated_camera_being_added_to_the_list(db):
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1"], now="2026-09-17")
    db.execute("UPDATE customer_camera_permissions SET can_unlock=1 WHERE user_id='viewer-1' AND camera_id='cam-1'")
    db.commit()
    assert _grant(db, "cam-1")["can_unlock"] == 1

    # Adding cam-2 to the list is completely unrelated to cam-1's unlock
    # grant -- it must survive untouched.
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1", "cam-2"], now="2026-09-17")
    assert _grant(db, "cam-1")["can_unlock"] == 1
    assert _grant(db, "cam-2")["can_unlock"] == 0  # newly added, starts at the real default


def test_extra_grants_survive_an_unrelated_camera_being_removed_from_the_list(db):
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1", "cam-2"], now="2026-09-17")
    db.execute("UPDATE customer_camera_permissions SET can_talk=1 WHERE user_id='viewer-1' AND camera_id='cam-1'")
    db.commit()

    # Revoking cam-2 entirely must not disturb cam-1's own can_talk grant.
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1"], now="2026-09-17")
    assert _grant(db, "cam-1")["can_talk"] == 1
    assert _grant(db, "cam-2") is None  # genuinely revoked, row gone


def test_all_three_extra_grants_survive_together(db):
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1"], now="2026-09-17")
    db.execute(
        "UPDATE customer_camera_permissions SET can_settings=1,can_talk=1,can_unlock=1 WHERE user_id='viewer-1' AND camera_id='cam-1'"
    )
    db.commit()
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1", "cam-3"], now="2026-09-17")
    grant = _grant(db, "cam-1")
    assert grant == {"can_settings": 1, "can_talk": 1, "can_unlock": 1}


def test_removing_and_re_adding_the_same_camera_does_not_resurrect_a_stale_grant(db):
    """A camera that drops off the list and later comes back is a fresh
    grant, not a preserved one -- set_camera_access() only preserves
    state that survived CONTINUOUSLY between two calls, matching its own
    existing "no stale leftover row" contract for can_live itself."""
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1"], now="2026-09-17")
    db.execute("UPDATE customer_camera_permissions SET can_unlock=1 WHERE user_id='viewer-1' AND camera_id='cam-1'")
    db.commit()
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=[], now="2026-09-17")
    assert _grant(db, "cam-1") is None
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1"], now="2026-09-17")
    assert _grant(db, "cam-1")["can_unlock"] == 0


def test_base_columns_are_unaffected_by_this_fix(db):
    camera_access.set_camera_access(db, user_id="viewer-1", access_mode="selected", camera_ids=["cam-1"], now="2026-09-17")
    row = db.execute(
        "SELECT can_live,can_playback,can_download,can_share,can_alerts FROM customer_camera_permissions WHERE user_id='viewer-1' AND camera_id='cam-1'"
    ).fetchone()
    assert dict(row) == {"can_live": 1, "can_playback": 1, "can_download": 0, "can_share": 0, "can_alerts": 1}
