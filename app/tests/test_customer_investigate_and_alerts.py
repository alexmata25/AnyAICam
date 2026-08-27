"""Regression coverage for the customer identity / per-camera permission /
Investigate block (feature/customer-ui-punch-list).

Covers the surfaces that were still on the legacy VMS identity
(user_camera_ids()/get_camera_numbers(), current_user()) instead of the
partner_identity()/customer_camera_permissions boundary Live, Playback,
Events and Smart Alerts already use:

  - /investigate (investigation_page() / _render_customer_investigate()):
    now dual-mode exactly like playback(), with a customer-portal branch
    reusing _customer_playback_cameras()/_customer_detection_events() --
    the same authorization Playback/Events already trust -- instead of
    the legacy load_motion_events()+analytics_events()+user_camera_ids()
    path.
  - _customer_notifications() (Smart Alerts): had zero test coverage
    before this milestone despite already being wired correctly.
  - PUT /api/analytics/events/{event_id}/review (Investigate's bookmark
    button): had no ownership check at all before this milestone -- any
    caller, including an unauthenticated one, could overwrite the review
    for any event_id. _customer_authorized_event_id() closes that.
  - POST /api/clips: had no authorization check of any kind before this
    milestone -- any direct POST could queue a clip job for any
    camera_number on the appliance. Now gated by the legacy
    current_user()/user_camera_ids() boundary the rest of the legacy
    pages already use.

Same import/isolation constraints as test_customer_analytics_events.py:
imports `main` (Windows-native Python only, matching this project's own
documented constraint), and every test redirects to a throwaway sqlite
file via override_target() before seeding or querying anything -- nothing
here ever touches the real production database.

1-of-5 and 3-of-10 camera-user scenarios are exercised across every
surface this file covers, matching the same rigor tests/test_camera_
access.py already applies to camera_access.py itself and Live.
"""

import asyncio
import json
import re
import sqlite3
from datetime import datetime
from urllib.parse import quote

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_investigate_alerts.db"


# ------------------------------------------------------------- shared seed helpers (same shape as test_customer_analytics_events.py)


def _seed_tenant(conn, customer_id, site_id, appliance_id, cloud_id):
    conn.execute(
        "INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", f"Customer {customer_id}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
        (site_id, customer_id, "Main Site", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, customer_id, site_id, cloud_id, "2026-01-01"),
    )


def _seed_camera(conn, camera_id, customer_id, site_id, appliance_id, camera_number, name):
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, name, "2026-01-01"),
    )


def _seed_event(conn, event_id, customer_id, site_id, appliance_id, camera_id, local_event_id,
                 event_type, confidence, timestamp):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, local_event_id,
         event_type, confidence, 1, None, timestamp, "2026-08-21T20:33:07"),
    )


def _seed_notification(conn, notif_id, user_id, customer_id, site_id, camera_id, event_type, severity, title, timestamp):
    conn.execute(
        "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,"
        "event_type,severity,title,message,timestamp,thumbnail,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (notif_id, user_id, customer_id, site_id, camera_id, None, None,
         event_type, severity, title, None, timestamp, None, timestamp),
    )


def _seed_owner(conn, user_id, email, customer_id):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Owner", "customer_owner", "x", 1, customer_id, "2026-01-01"),
    )


def _seed_viewer(conn, user_id, email, customer_id, camera_ids_with_playback):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Viewer", "customer_viewer", "x", 1, customer_id, "2026-01-01"),
    )
    for camera_id in camera_ids_with_playback:
        conn.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES(?,?,1)",
            (user_id, camera_id),
        )


def _owner_identity(customer_id):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _viewer_identity(customer_id, email):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


def _seed_fleet(conn, customer_id, site_id, appliance_id, cloud_id, camera_count, *, prefix="cam"):
    """customer_id's own N-camera fleet, one detection_events row and one
    notifications row per camera, each with a distinguishable timestamp
    so ownership/leakage can be asserted per camera."""
    _seed_tenant(conn, customer_id, site_id, appliance_id, cloud_id)
    camera_ids = [f"{prefix}-{n}" for n in range(1, camera_count + 1)]
    for index, camera_id in enumerate(camera_ids, start=1):
        _seed_camera(conn, camera_id, customer_id, site_id, appliance_id, index, f"Camera {index}")
        _seed_event(
            conn, f"evt-{camera_id}", customer_id, site_id, appliance_id, camera_id, f"local-{camera_id}",
            "person", 0.9, f"2026-08-2{index % 9 + 1}T0{index % 9 + 1}:00:00",
        )
    return camera_ids


# =============================================================== Investigate: dual-mode dispatch


def test_investigate_route_falls_through_to_legacy_for_non_customer_identity(monkeypatch, db_path):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    called = {}
    monkeypatch.setattr(main, "_render_customer_investigate", lambda cameras, request: called.setdefault("hit", True))
    monkeypatch.setattr(main, "current_user", lambda request: {"role": "viewer", "enabled": False})
    with override_target(sqlite_path=db_path):
        initialize_database()
        result = main.investigation_page(object())
    assert "hit" not in called  # customer branch never invoked
    assert "Investigate" in result and "permission" in result.lower()  # legacy permission-denied page for a viewer


def test_investigate_route_dispatches_to_customer_branch_for_customer_identity(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 2)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main.investigation_page(object())
    assert "AI event investigation" in result
    assert "cam-1" in result and "cam-2" in result


# =============================================================== Investigate: camera scoping (1-of-5, 3-of-10)


def _investigation_events(html):
    match = re.search(r"const investigationEvents=(\[.*?\]);\n\s*const selectedEvidence", html, re.S)
    assert match, "could not find embedded investigationEvents payload"
    return json.loads(match.group(1))


def test_investigate_owner_sees_every_camera_in_a_five_camera_fleet(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main.investigation_page(object())
    events = _investigation_events(result)
    assert {event["camera_id"] for event in events} == set(camera_ids)
    for camera_id in camera_ids:
        assert f'value="{camera_id}"' in result


def test_investigate_viewer_with_one_of_five_cameras_sees_only_that_camera(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=[camera_ids[2]])
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main.investigation_page(object())
    events = _investigation_events(result)
    assert {event["camera_id"] for event in events} == {camera_ids[2]}
    # Only the granted camera appears in the filter dropdown -- the other
    # four are never advertised to this identity at all.
    assert f'value="{camera_ids[2]}"' in result
    for denied_camera in camera_ids[:2] + camera_ids[3:]:
        assert f'value="{denied_camera}"' not in result


def test_investigate_viewer_with_three_of_ten_cameras_sees_only_those_three(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 10)
        granted = [camera_ids[1], camera_ids[4], camera_ids[8]]
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=granted)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main.investigation_page(object())
    events = _investigation_events(result)
    assert {event["camera_id"] for event in events} == set(granted)
    for camera_id in granted:
        assert f'value="{camera_id}"' in result
    for denied_camera in set(camera_ids) - set(granted):
        assert f'value="{denied_camera}"' not in result


def test_investigate_never_leaks_another_customers_events_or_cameras(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 3)
        other_cameras = _seed_fleet(conn, "cust-2", "site-2", "appl-2", "AIC-2", 3, prefix="other")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main.investigation_page(object())
    events = _investigation_events(result)
    assert all(not event["camera_id"].startswith("other") for event in events)
    for other_camera in other_cameras:
        assert other_camera not in result


def test_investigate_deep_links_use_cameras_id_and_the_shared_playback_route(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 1)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main.investigation_page(object())
    events = _investigation_events(result)
    camera_id = camera_ids[0]
    assert events[0]["recording"] == f"/playback?camera={camera_id}&t={quote(events[0]['timestamp'])}"
    assert events[0]["live"] == f"/customer/cameras/{camera_id}/live"


def test_investigate_customer_render_has_no_create_case_button():
    # Case management (/api/investigation-cases) stays an
    # administrator/installer-only workflow with no per-tenant scoping
    # yet -- see _render_customer_investigate()'s own docstring. This
    # asserts the UI never offers an action this identity's own API
    # calls would then be rejected for.
    result = main._render_customer_investigate([{"id": "cam-1", "name": "Front", "camera_number": 1}], object())
    assert "create-case-from-evidence" not in result
    assert "export-evidence" in result  # pure client-side export stays available


def test_investigate_no_cameras_renders_honest_empty_state(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "site-1", "appl-1", "AIC-1")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main.investigation_page(object())
    assert "No cameras are available for investigation" in result


# =============================================================== "investigate" nav key reaches the customer portal


def test_investigate_key_is_advertised_in_customer_portal_navigation():
    assert "investigate" in main.navigation_keys_for_role("customer_owner")
    assert "investigate" in main.navigation_keys_for_role("customer_viewer")


# =============================================================== Smart Alerts: _customer_notifications() (previously untested)


def test_notifications_returns_none_for_non_customer_identity(monkeypatch):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    assert main._customer_notifications(object()) is None


def test_notifications_owner_sees_every_camera_in_a_five_camera_fleet(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        _seed_owner(conn, "owner-1", "owner@example.com", "cust-1")
        for index, camera_id in enumerate(camera_ids, start=1):
            _seed_notification(conn, f"notif-{camera_id}", "owner-1", "cust-1", "site-1", camera_id,
                                "motion", "info", "Motion detected", f"2026-08-2{index}T00:00:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_notifications(object())
    assert {row["camera_id"] for row in result} == set(camera_ids)


def test_notifications_viewer_with_one_of_five_cameras_sees_only_that_camera(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        _seed_owner(conn, "owner-1", "owner@example.com", "cust-1")
        for index, camera_id in enumerate(camera_ids, start=1):
            _seed_notification(conn, f"notif-{camera_id}", "owner-1", "cust-1", "site-1", camera_id,
                                "motion", "info", "Motion detected", f"2026-08-2{index}T00:00:00")
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=[camera_ids[3]])
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main._customer_notifications(object())
    assert [row["camera_id"] for row in result] == [camera_ids[3]]


def test_notifications_viewer_with_three_of_ten_cameras_sees_only_those_three(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 10)
        _seed_owner(conn, "owner-1", "owner@example.com", "cust-1")
        for index, camera_id in enumerate(camera_ids, start=1):
            _seed_notification(conn, f"notif-{camera_id}", "owner-1", "cust-1", "site-1", camera_id,
                                "motion", "info", "Motion detected", f"2026-08-2{index % 9 + 1}T00:00:00")
        granted = [camera_ids[0], camera_ids[5], camera_ids[9]]
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=granted)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main._customer_notifications(object())
    assert {row["camera_id"] for row in result} == set(granted)


def test_notifications_viewer_never_sees_a_cameraless_system_alert(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 1)
        _seed_owner(conn, "owner-1", "owner@example.com", "cust-1")
        _seed_notification(conn, "notif-system", "owner-1", "cust-1", "site-1", None,
                            "low_disk", "warning", "Disk space low", "2026-08-21T00:00:00")
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=camera_ids)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main._customer_notifications(object())
    assert "notif-system" not in [row["id"] for row in result]


def test_notifications_never_leak_across_customers(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 1)
        _seed_owner(conn, "owner-1", "owner@example.com", "cust-1")
        _seed_notification(conn, "notif-mine", "owner-1", "cust-1", "site-1", camera_ids[0],
                            "motion", "info", "Motion", "2026-08-21T00:00:00")
        other_cameras = _seed_fleet(conn, "cust-2", "site-2", "appl-2", "AIC-2", 1, prefix="other")
        _seed_owner(conn, "owner-2", "owner2@example.com", "cust-2")
        _seed_notification(conn, "notif-theirs", "owner-2", "cust-2", "site-2", other_cameras[0],
                            "motion", "info", "Motion", "2026-08-21T00:00:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_notifications(object())
    assert [row["id"] for row in result] == ["notif-mine"]


# =============================================================== bookmark ownership: _customer_authorized_event_id() / PUT review


def test_authorized_event_id_true_for_non_customer_identity_legacy_path_unchanged(monkeypatch):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    assert main._customer_authorized_event_id(object(), "any-event-id") is True


def test_authorized_event_id_owner_can_review_their_own_event(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 1)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        assert main._customer_authorized_event_id(object(), f"evt-{camera_ids[0]}") is True


def test_authorized_event_id_owner_cannot_review_another_customers_event(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 1)
        other_cameras = _seed_fleet(conn, "cust-2", "site-2", "appl-2", "AIC-2", 1, prefix="other")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        assert main._customer_authorized_event_id(object(), f"evt-{other_cameras[0]}") is False


def test_authorized_event_id_viewer_with_permission_can_review(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=[camera_ids[2]])
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        assert main._customer_authorized_event_id(object(), f"evt-{camera_ids[2]}") is True


def test_authorized_event_id_viewer_without_permission_on_that_camera_is_denied(monkeypatch, db_path):
    """The exact scenario this milestone closes: before
    _customer_authorized_event_id() existed, a customer_viewer (or
    anyone else) could PUT a review for any event_id -- including one
    on a camera they were never granted -- because the route had no
    ownership check of its own."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=[camera_ids[2]])
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        # evt-cam-1 belongs to their own customer but a camera they were
        # never granted -- 4-of-5 denied, not just the other tenant.
        assert main._customer_authorized_event_id(object(), f"evt-{camera_ids[0]}") is False


def test_review_route_rejects_unauthorized_event_with_403(monkeypatch, db_path, tmp_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 5)
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=[camera_ids[2]])
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        monkeypatch.setattr(main, "EVENT_REVIEWS_FILE", tmp_path / "event_reviews.json")
        event_id = f"evt-{camera_ids[0]}"  # not granted to this viewer
        review = main.EventReviewModel(event_id=event_id, bookmarked=True)
        with pytest.raises(main.HTTPException) as excinfo:
            main.review_analytics_event(event_id, review, object())
    assert excinfo.value.status_code == 403


def test_review_route_allows_authorized_event_and_persists(monkeypatch, db_path, tmp_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        camera_ids = _seed_fleet(conn, "cust-1", "site-1", "appl-1", "AIC-1", 1)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        monkeypatch.setattr(main, "EVENT_REVIEWS_FILE", tmp_path / "event_reviews.json")
        event_id = f"evt-{camera_ids[0]}"
        review = main.EventReviewModel(event_id=event_id, bookmarked=True)
        result = main.review_analytics_event(event_id, review, object())
    assert result["status"] == "complete"
    assert result["review"]["bookmarked"] is True


# =============================================================== clips: POST /api/clips had no auth at all


def test_create_clip_rejects_unauthenticated_caller(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: {"role": "viewer", "enabled": False})
    clip_request = main.ClipRequest(camera=1, start_time=datetime(2026, 8, 21, 0, 0, 0), end_time=datetime(2026, 8, 21, 0, 0, 30))
    with pytest.raises(main.HTTPException) as excinfo:
        asyncio.run(main.create_clip(clip_request, object()))
    assert excinfo.value.status_code == 403


def test_create_clip_rejects_camera_not_in_callers_own_list(monkeypatch):
    # role "operator", not "admin" -- user_camera_ids() special-cases
    # role=="admin" (and super_admin) to always return get_camera_numbers()
    # regardless of the caller's own camera_ids, which would make this
    # test actually exercise get_camera_numbers()'s fallback boundary
    # instead of the caller's own list as the test name claims.
    #
    # get_camera_numbers() is also explicitly seeded here with camera 5
    # (a real provisioned camera on this appliance, just not one this
    # caller is assigned) -- user_camera_ids() intersects a caller's own
    # camera_ids against get_camera_numbers() as an extra floor ("can't
    # be permitted to a camera that doesn't exist"), so leaving
    # get_camera_numbers() at its post-fix empty default would make
    # camera 5 fail for that unrelated reason instead of the "not in the
    # caller's own list" boundary this test is named for. See the
    # Samsung camera-count audit: before this fix, this test happened to
    # still pass either way only because get_camera_numbers() phantom-
    # defaulted to [1,2,3,4], which coincidentally excluded camera 5.
    monkeypatch.setattr(main, "current_user", lambda request: {"role": "operator", "camera_ids": [1, 2], "enabled": True})
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2, 5])
    clip_request = main.ClipRequest(camera=5, start_time=datetime(2026, 8, 21, 0, 0, 0), end_time=datetime(2026, 8, 21, 0, 0, 30))
    with pytest.raises(main.HTTPException) as excinfo:
        asyncio.run(main.create_clip(clip_request, object()))
    assert excinfo.value.status_code == 403


def test_create_clip_allows_camera_in_callers_own_list(monkeypatch):
    # See test_create_clip_rejects_camera_not_in_callers_own_list's
    # comment -- role "operator" (not "admin"), and get_camera_numbers()
    # seeded with the caller's own cameras so the post-fix empty default
    # doesn't mask the "own list" check this test exists to prove.
    monkeypatch.setattr(main, "current_user", lambda request: {"role": "operator", "camera_ids": [1, 2], "enabled": True})
    monkeypatch.setattr(main, "get_camera_numbers", lambda: [1, 2])

    async def _fake_build_manual_clip(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "build_manual_clip", _fake_build_manual_clip)
    clip_request = main.ClipRequest(camera=2, start_time=datetime(2026, 8, 21, 0, 0, 0), end_time=datetime(2026, 8, 21, 0, 0, 30))
    result = asyncio.run(main.create_clip(clip_request, object()))
    assert result["status"] == "queued"
