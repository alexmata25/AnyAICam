"""Notifications Reliability Phase (2026-09-14, starting checkpoint
8b11233): focused regression coverage for confirmed defects found by
tracing the real customer notification pipeline end to end --
notification_engine.fanout_appliance_event() (creation) ->
main._customer_notifications() (retrieval) -> main._render_customer_
alerts() (the real Smart Alerts UI) -> new mark-read routes (read/unread
state, previously entirely unimplemented despite the notifications
table's own read_at/acknowledged_at/dismissed_at/bookmarked_at columns).

Confirmed defects, all fixed in this pass:

  1. fanout_appliance_event(): a customer_viewer with ZERO customer_
     camera_permissions rows -- the real state of every brand-new
     viewer, before a customer_owner grants any camera access --
     previously fell through the permission check entirely
     (`if [] and ...` is always False) and was notified about every
     camera on the account, the opposite of camera_access.py's own
     documented fail-closed default (DEFAULT_ACCESS_MODE='selected').
     partner_users.camera_access_mode now governs the empty-rows case.
  2. _customer_notifications(): the customer_viewer JOIN to customer_
     camera_permissions never checked a permission column at all (any
     row, even one with can_alerts explicitly 0, satisfied it) and
     never filtered n.user_id -- since notifications are per-recipient
     rows (one per real customer_owner/customer_viewer), any viewer
     holding a permission row for a camera saw every OTHER recipient's
     own notification rows for that camera too, not just their own.
  3. Read/unread state was schema-only: no route anywhere ever read or
     wrote read_at, and _customer_notifications() did not even select
     it. New POST /api/customer/notifications/{id}/read and .../
     read-all, scoped to the caller's own resolved user_id + customer_id
     -- account-wide LIST visibility for customer_owner is unchanged,
     but mark-read mutation never crosses to a different recipient's row.
  4. _render_customer_alerts() never applied the same UTC ->
     APPLIANCE_TIMEZONE conversion _render_customer_events() already
     had (2026-09-02 "five-hour timestamp offset" fix) -- alert
     timestamps were off by the same several hours.
  5. _render_customer_alerts() called _customer_event_actions() with
     only camera_id, dropping the timestamp/event_id/has_event_clip
     _customer_notifications() now also selects -- every alert card's
     Playback link fell back to a generic, non-deep-linked href instead
     of the event's own clip, unlike the already-correct Events page.

Same import/isolation constraints as test_customer_investigate_and_
alerts.py: imports `main` (Windows-native Python only), every test
redirects to a throwaway sqlite file via override_target() first.
"""

import sqlite3

import pytest

import main
import notification_engine
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_notifications_reliability.db"


# --------------------------------------------------------- seed helpers


def _seed_tenant(conn, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", cloud_id="AIC-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", f"Customer {customer_id}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main Site", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, "2026-01-01"))


def _seed_camera(conn, camera_id, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_number=1, name="Camera 1"):
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, name, "2026-01-01"),
    )


def _seed_owner(conn, user_id="owner-1", email="owner@example.com", customer_id="cust-1"):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Owner", "customer_owner", "x", 1, customer_id, "2026-01-01"),
    )


def _seed_viewer(conn, user_id="viewer-1", email="viewer@example.com", customer_id="cust-1", *, access_mode=None):
    fields = "id,partner_id,email,name,role,password_hash,approved,customer_id,created_at"
    values = [user_id, "partner-1", email, "Viewer", "customer_viewer", "x", 1, customer_id, "2026-01-01"]
    if access_mode is not None:
        fields += ",camera_access_mode"
        values.append(access_mode)
    placeholders = ",".join("?" for _ in values)
    conn.execute(f"INSERT INTO partner_users({fields}) VALUES({placeholders})", values)


def _grant_camera(conn, user_id, camera_id, *, can_alerts=1):
    conn.execute(
        "INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback,can_alerts) VALUES(?,?,1,?)",
        (user_id, camera_id, can_alerts),
    )


def _seed_notification(conn, notif_id, user_id, camera_id, *, customer_id="cust-1", site_id="site-1",
                        event_type="motion", event_id=None, timestamp="2026-08-22T00:00:00"):
    conn.execute(
        "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,"
        "event_type,severity,title,message,timestamp,thumbnail,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (notif_id, user_id, customer_id, site_id, camera_id, event_id, None,
         event_type, "info", event_type.title(), None, timestamp, None, timestamp),
    )


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _viewer_identity(customer_id="cust-1", email="viewer@example.com"):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


# =============================================================== 1. creation-time camera isolation (notification_engine.py)


def test_viewer_with_no_permission_rows_and_default_access_mode_gets_no_notification(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_viewer(conn)  # camera_access_mode left at its real schema default ('selected')
        conn.commit()
        created = notification_engine.fanout_appliance_event(
            {"customer_id": "cust-1", "site_id": "site-1"},
            {"id": "evt-1", "camera_id": "cam-1", "event_type": "motion", "timestamp": "2026-08-22T00:00:01"},
        )
        rows = sqlite3.connect(db_path).execute("SELECT user_id FROM notifications WHERE camera_id='cam-1'").fetchall()
    assert created == 0
    assert rows == []


def test_viewer_with_no_permission_rows_and_access_mode_all_gets_notification(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_viewer(conn, access_mode="all")
        conn.commit()
        created = notification_engine.fanout_appliance_event(
            {"customer_id": "cust-1", "site_id": "site-1"},
            {"id": "evt-1", "camera_id": "cam-1", "event_type": "motion", "timestamp": "2026-08-22T00:00:01"},
        )
    assert created == 1


def test_viewer_with_explicit_permission_rows_is_unaffected_by_this_fix(monkeypatch, db_path):
    """Regression guard: a viewer who already has real permission rows
    keeps the exact prior can_alerts-gated behavior, whatever their
    camera_access_mode happens to be."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-allowed", camera_number=1)
        _seed_camera(conn, "cam-denied", camera_number=2)
        _seed_viewer(conn)
        _grant_camera(conn, "viewer-1", "cam-allowed", can_alerts=1)
        _grant_camera(conn, "viewer-1", "cam-denied", can_alerts=0)
        conn.commit()
        created_allowed = notification_engine.fanout_appliance_event(
            {"customer_id": "cust-1", "site_id": "site-1"},
            {"id": "evt-allowed", "camera_id": "cam-allowed", "event_type": "motion", "timestamp": "2026-08-22T00:00:01"},
        )
        created_denied = notification_engine.fanout_appliance_event(
            {"customer_id": "cust-1", "site_id": "site-1"},
            {"id": "evt-denied", "camera_id": "cam-denied", "event_type": "motion", "timestamp": "2026-08-22T00:00:02"},
        )
    assert created_allowed == 1
    assert created_denied == 0


# =============================================================== 2. retrieval-time camera isolation (_customer_notifications())


def test_viewer_with_can_alerts_revoked_does_not_see_that_cameras_notifications(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_viewer(conn)
        _grant_camera(conn, "viewer-1", "cam-1", can_alerts=0)  # explicit row, alerts off
        _seed_notification(conn, "notif-1", "viewer-1", "cam-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity())
        result = main._customer_notifications(object())
    assert result == []


def test_viewer_does_not_see_another_recipients_own_notification_row(monkeypatch, db_path):
    """The cross-user leak: both users are granted the same camera, but a
    notification row created for the owner must never appear in the
    viewer's own retrieval -- fanout_appliance_event() inserts one
    independent row per real recipient."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        _seed_viewer(conn)
        _grant_camera(conn, "viewer-1", "cam-1", can_alerts=1)
        _seed_notification(conn, "notif-owner", "owner-1", "cam-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity())
        viewer_result = main._customer_notifications(object())
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        owner_result = main._customer_notifications(object())
    assert viewer_result == []  # not their own row
    assert [row["id"] for row in owner_result] == ["notif-owner"]  # owner's own account-wide visibility, unchanged


def test_customer_notifications_exposes_event_id_has_event_clip_and_read_state(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,object_count,event_timestamp,created_at) "
            "VALUES('evt-1','cust-1','site-1','appl-1','cam-1','local-1','motion',1,'2026-08-22T00:00:00','2026-08-22T00:00:00')"
        )
        conn.execute(
            "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,started_at,ended_at,created_at) "
            "VALUES('media-1','evt-1','cust-1','cam-1','clips/evt-1.mp4',NULL,'2026-08-22T00:00:00','2026-08-22T00:00:05','2026-08-22T00:00:05')"
        )
        _seed_notification(conn, "notif-1", "owner-1", "cam-1", event_id="evt-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main._customer_notifications(object())
    assert len(result) == 1
    row = result[0]
    assert row["event_id"] == "evt-1"
    assert row["has_event_clip"] is True
    assert row["read_at"] is None
    assert row["read"] is False


# =============================================================== 3. mark-read routes


def test_mark_notification_read_own_notification_succeeds(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        _seed_notification(conn, "notif-1", "owner-1", "cam-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main.mark_customer_notification_read(object(), "notif-1")
        read_at = sqlite3.connect(db_path).execute("SELECT read_at FROM notifications WHERE id='notif-1'").fetchone()[0]
    assert result["status"] == "ok"
    assert read_at is not None


def test_mark_notification_read_is_idempotent(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        _seed_notification(conn, "notif-1", "owner-1", "cam-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        main.mark_customer_notification_read(object(), "notif-1")
        first_read_at = sqlite3.connect(db_path).execute("SELECT read_at FROM notifications WHERE id='notif-1'").fetchone()[0]
        result = main.mark_customer_notification_read(object(), "notif-1")  # second click
        second_read_at = sqlite3.connect(db_path).execute("SELECT read_at FROM notifications WHERE id='notif-1'").fetchone()[0]
    assert result["status"] == "ok"
    assert first_read_at == second_read_at  # never overwritten by a retried click


def test_mark_notification_read_foreign_customer_denied_with_zero_mutation(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, customer_id="cust-1")
        _seed_tenant(conn, customer_id="cust-2", site_id="site-2", appliance_id="appl-2", cloud_id="AIC-2")
        _seed_camera(conn, "cam-2", customer_id="cust-2", site_id="site-2", appliance_id="appl-2")
        _seed_owner(conn, user_id="owner-2", email="owner2@example.com", customer_id="cust-2")
        _seed_notification(conn, "notif-foreign", "owner-2", "cam-2", customer_id="cust-2", site_id="site-2")
        _seed_owner(conn)  # the actual caller, cust-1's own owner
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        with pytest.raises(main.HTTPException) as excinfo:
            main.mark_customer_notification_read(object(), "notif-foreign")
        read_at = sqlite3.connect(db_path).execute("SELECT read_at FROM notifications WHERE id='notif-foreign'").fetchone()[0]
    assert excinfo.value.status_code == 404
    assert read_at is None  # zero mutation on denial


def test_mark_notification_read_a_different_recipients_own_row_is_denied(monkeypatch, db_path):
    """Account-wide LIST visibility for customer_owner is unchanged, but
    mark-read is a mutation, always scoped to the caller's own resolved
    user_id -- an owner's click can never silently mark a viewer's own
    (or another owner's own) notification row as read."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        _seed_viewer(conn)
        _seed_notification(conn, "notif-viewer", "viewer-1", "cam-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(main.HTTPException) as excinfo:
            main.mark_customer_notification_read(object(), "notif-viewer")
        read_at = sqlite3.connect(db_path).execute("SELECT read_at FROM notifications WHERE id='notif-viewer'").fetchone()[0]
    assert excinfo.value.status_code == 404
    assert read_at is None


def test_mark_all_read_only_touches_callers_own_unread_rows(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        _seed_viewer(conn)
        _seed_notification(conn, "notif-owner-1", "owner-1", "cam-1", timestamp="2026-08-22T00:00:01")
        _seed_notification(conn, "notif-owner-2", "owner-1", "cam-1", timestamp="2026-08-22T00:00:02")
        _seed_notification(conn, "notif-viewer-1", "viewer-1", "cam-1", timestamp="2026-08-22T00:00:03")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main.mark_all_customer_notifications_read(object())
        rows = {r[0]: r[1] for r in sqlite3.connect(db_path).execute("SELECT id,read_at FROM notifications")}
    assert result["marked_read"] == 2
    assert rows["notif-owner-1"] is not None
    assert rows["notif-owner-2"] is not None
    assert rows["notif-viewer-1"] is None  # a different recipient's own row, untouched


# =============================================================== 4 & 5. Alerts page rendering: timezone + deep link


def test_alerts_page_converts_utc_timestamp_to_appliance_timezone(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        # 2026-08-22T18:00:00 UTC -> 1:00 PM America/Chicago (CDT, UTC-5).
        _seed_notification(conn, "notif-1", "owner-1", "cam-1", timestamp="2026-08-22T18:00:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main._render_customer_alerts(object())
    assert "06:00:00 PM" not in result  # the old, unconverted bug's raw-UTC output
    assert "01:00:00 PM" in result


def test_alerts_page_deep_links_to_the_specific_event_when_a_clip_exists(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,object_count,event_timestamp,created_at) "
            "VALUES('evt-1','cust-1','site-1','appl-1','cam-1','local-1','motion',1,'2026-08-22T00:00:00','2026-08-22T00:00:00')"
        )
        conn.execute(
            "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,started_at,ended_at,created_at) "
            "VALUES('media-1','evt-1','cust-1','cam-1','clips/evt-1.mp4',NULL,'2026-08-22T00:00:00','2026-08-22T00:00:05','2026-08-22T00:00:05')"
        )
        _seed_notification(conn, "notif-1", "owner-1", "cam-1", event_id="evt-1", timestamp="2026-08-22T00:00:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main._render_customer_alerts(object())
    assert "camera=cam-1&event=evt-1&autoplay=event" in result


def test_alerts_page_shows_mark_read_button_for_unread_only(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_owner(conn)
        _seed_notification(conn, "notif-unread", "owner-1", "cam-1", timestamp="2026-08-22T00:00:01")
        _seed_notification(conn, "notif-read", "owner-1", "cam-1", timestamp="2026-08-22T00:00:02")
        conn.execute("UPDATE notifications SET read_at='2026-08-22T01:00:00' WHERE id='notif-read'")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main._render_customer_alerts(object())
    assert 'data-notification-id="notif-unread"' in result
    assert 'data-notification-id="notif-read"' in result
    assert result.count('data-notification-id="notif-unread"' ) >= 1
    # Exactly one Mark-read button rendered (the unread one) -- find each
    # card's own data-read attribute rather than counting button markup,
    # which would also match the mark-all-read button's own id.
    assert 'data-notification-id="notif-unread" data-read="0"' in result
    assert 'data-notification-id="notif-read" data-read="1"' in result
    assert "1 unread" in result
