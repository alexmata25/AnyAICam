"""Customer-facing analytics read-path milestone: tests for
_customer_detection_events(), _build_analytics_summary(), and the
customer-scoped branches of analytics_event_search() /
analytics_summary_api().

Same import/isolation constraints as test_customer_recordings_r4.py:
imports `main` (must run inside the deployed container or via
Windows-native Python, not this WSL host's plain python3/pytest), and
every test redirects to a throwaway sqlite file via override_target()
before seeding or querying anything -- nothing here ever writes to the
real production database.

Auth is exercised for real (not bypassed): partner_portal.partner_identity
is monkeypatched to return a chosen identity dict, and
_customer_detection_events() is called with the actual local
`from partner_portal import partner_identity` import inside it picking up
the patched value at call time -- this proves the real auth wiring, not
just the raw SQL a hand-copied query would.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_analytics.db"


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
                 event_type, confidence, timestamp, object_count=1, created_at="2026-08-21T20:33:07"):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, local_event_id,
         event_type, confidence, object_count, None, timestamp, created_at),
    )
    conn.commit()


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
    conn.commit()


# ------------------------------------------------------------- fixtures matching the real proof event

REAL_CUSTOMER, REAL_SITE, REAL_APPLIANCE, REAL_CLOUD_ID = "d277d69443", "05d6a66c6e", "7eb499b6d1", "AIC-7C4887FA"
REAL_CAMERA_ID, REAL_CAMERA_NUMBER = "fe09d61c7d", 3
REAL_EVENT_ID = "e77e173bc820fffdb726e799"
REAL_LOCAL_EVENT_ID = "b2609518c1ae"


def _seed_real_proof_event(conn):
    _seed_tenant(conn, REAL_CUSTOMER, REAL_SITE, REAL_APPLIANCE, REAL_CLOUD_ID)
    _seed_camera(conn, REAL_CAMERA_ID, REAL_CUSTOMER, REAL_SITE, REAL_APPLIANCE, REAL_CAMERA_NUMBER, "Camera 3")
    _seed_event(
        conn, REAL_EVENT_ID, REAL_CUSTOMER, REAL_SITE, REAL_APPLIANCE, REAL_CAMERA_ID, REAL_LOCAL_EVENT_ID,
        "car", 0.6666, "2026-08-21T03:19:20.758992", object_count=1,
    )


def _owner_identity(customer_id=REAL_CUSTOMER):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _viewer_identity(customer_id, email):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


# ------------------------------------------------------------- _customer_detection_events()


def test_returns_none_for_non_customer_identity(monkeypatch, db_path):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: {"role": "administrator", "customer_id": None})
    assert main._customer_detection_events(object()) is None


def test_returns_none_when_no_identity_at_all(monkeypatch, db_path):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    assert main._customer_detection_events(object()) is None


def test_customer_owner_sees_own_row(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        result = main._customer_detection_events(object())
    assert len(result) == 1
    assert result[0]["id"] == REAL_EVENT_ID
    assert result[0]["camera"] == REAL_CAMERA_NUMBER
    assert result[0]["site"] == "Main Site"
    assert result[0]["event_type"] == "car"
    assert result[0]["confidence"] == 0.6666
    assert result[0]["timestamp"] == "2026-08-21T03:19:20.758992"
    assert result[0]["thumbnail"] is None
    assert result[0]["linked_recording"] is None
    assert result[0]["plate_number"] is None
    assert result[0]["vehicle_color"] is None
    assert result[0]["mock"] is False


def test_customer_owner_never_sees_a_different_customers_rows(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)  # customer d277d69443's real event
        # A second, unrelated customer with its own event.
        _seed_tenant(conn, "cust-other", "site-other", "appl-other", "AIC-OTHER")
        _seed_camera(conn, "cam-other", "cust-other", "site-other", "appl-other", 1, "Camera 1")
        _seed_event(conn, "evt-other-id", "cust-other", "site-other", "appl-other", "cam-other", "local-other",
                    "person", 0.9, "2026-08-21T05:00:00")

        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-other"))
        result = main._customer_detection_events(object())
    assert len(result) == 1
    assert result[0]["id"] == "evt-other-id"
    assert REAL_EVENT_ID not in [row["id"] for row in result]  # no leakage of the other tenant's row


def test_unrelated_customer_with_no_events_sees_empty_list_not_none(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)
        _seed_tenant(conn, "cust-empty", "site-empty", "appl-empty", "AIC-EMPTY")

        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-empty"))
        result = main._customer_detection_events(object())
    assert result == []  # real, honest empty -- not None (which would fall back to mock), not the other tenant's data


def test_customer_viewer_sees_only_permitted_camera(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "site-1", "appl-1", "AIC-1")
        _seed_camera(conn, "cam-1", "cust-1", "site-1", "appl-1", 1, "Camera 1")
        _seed_camera(conn, "cam-2", "cust-1", "site-1", "appl-1", 2, "Camera 2")
        _seed_event(conn, "evt-1", "cust-1", "site-1", "appl-1", "cam-1", "local-1", "person", 0.8, "2026-08-21T01:00:00")
        _seed_event(conn, "evt-2", "cust-1", "site-1", "appl-1", "cam-2", "local-2", "car", 0.7, "2026-08-21T02:00:00")
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=["cam-1"])

        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main._customer_detection_events(object())
    assert [row["id"] for row in result] == ["evt-1"]  # only the permitted camera's event


def test_customer_viewer_without_any_grant_sees_empty_list(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "site-1", "appl-1", "AIC-1")
        _seed_camera(conn, "cam-1", "cust-1", "site-1", "appl-1", 1, "Camera 1")
        _seed_event(conn, "evt-1", "cust-1", "site-1", "appl-1", "cam-1", "local-1", "person", 0.8, "2026-08-21T01:00:00")
        _seed_viewer(conn, "viewer-1", "viewer@example.com", "cust-1", camera_ids_with_playback=[])

        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main._customer_detection_events(object())
    assert result == []


def test_unknown_viewer_email_sees_empty_list(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "site-1", "appl-1", "AIC-1")
        _seed_camera(conn, "cam-1", "cust-1", "site-1", "appl-1", 1, "Camera 1")
        _seed_event(conn, "evt-1", "cust-1", "site-1", "appl-1", "cam-1", "local-1", "person", 0.8, "2026-08-21T01:00:00")

        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "ghost@example.com"))
        result = main._customer_detection_events(object())
    assert result == []


# ------------------------------------------------------------- analytics_event_search() route function


def test_route_returns_proof_row_with_mock_data_false(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        response = main.analytics_event_search(object())
    assert response["mock_data"] is False
    assert len(response["events"]) == 1
    assert response["events"][0]["id"] == REAL_EVENT_ID
    assert response["events"][0]["event_type"] == "car"


def test_route_unrelated_customer_gets_empty_list_and_mock_data_false(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)
        _seed_tenant(conn, "cust-empty", "site-empty", "appl-empty", "AIC-EMPTY")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-empty"))
        response = main.analytics_event_search(object())
    assert response == {"events": [], "mock_data": False}  # real empty, never masquerading as mock


def test_route_filters_still_apply_on_top_of_tenant_scoping(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "site-1", "appl-1", "AIC-1")
        _seed_camera(conn, "cam-1", "cust-1", "site-1", "appl-1", 1, "Camera 1")
        _seed_event(conn, "evt-1", "cust-1", "site-1", "appl-1", "cam-1", "local-1", "person", 0.8, "2026-08-21T01:00:00")
        _seed_event(conn, "evt-2", "cust-1", "site-1", "appl-1", "cam-1", "local-2", "car", 0.7, "2026-08-21T02:00:00")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        response = main.analytics_event_search(object(), event_type="car")
    assert [event["id"] for event in response["events"]] == ["evt-2"]


def test_route_legacy_behavior_unchanged_for_non_customer_caller(monkeypatch, db_path):
    """Proves the non-customer branch is byte-for-byte the original
    behavior -- same analytics_events()/mock_data source, completely
    untouched by this milestone."""
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    fixture_events = [{"id": "legacy-1", "event_type": "person", "timestamp": "2026-01-01T00:00:00", "camera": 1, "site": "home"}]
    monkeypatch.setattr(main, "analytics_events", lambda: fixture_events)
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", type("P", (), {"exists": staticmethod(lambda: True)})())
    response = main.analytics_event_search(object())
    assert response == {"events": fixture_events, "mock_data": False}


# ------------------------------------------------------------- analytics_summary_api() route function / _build_analytics_summary()


def test_summary_totals_type_and_camera_for_the_real_tenant(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        summary = main.analytics_summary_api(object())
    assert summary["mock_data"] is False
    assert summary["total_events"] == 1
    assert summary["type_counts"] == {"car": 1}
    assert summary["camera_counts"][str(REAL_CAMERA_NUMBER)] == 1
    assert summary["active_camera"] == REAL_CAMERA_NUMBER


def test_summary_unrelated_customer_is_all_zero_not_mock(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_real_proof_event(conn)
        _seed_tenant(conn, "cust-empty", "site-empty", "appl-empty", "AIC-EMPTY")
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-empty"))
        summary = main.analytics_summary_api(object())
    assert summary["mock_data"] is False
    assert summary["total_events"] == 0
    assert summary["type_counts"] == {}


def test_build_analytics_summary_is_a_pure_function_matching_legacy_output_shape():
    events = [{"event_type": "car", "confidence": 0.5, "camera": 1, "timestamp": "2026-08-21T00:00:00"}]
    result = main._build_analytics_summary(events, mock_data=True)
    assert result["mock_data"] is True
    assert result["total_events"] == 1
    assert set(result.keys()) == {
        "total_events", "today_count", "last_24_count", "last_7_count", "average_confidence",
        "peak_hour", "active_camera", "type_counts", "camera_counts", "hourly_counts",
        "seven_day", "mock_data", "checked_at",
    }


# ------------------------------------------------------------- structural isolation (no reach into other subsystems)


def test_new_code_never_references_detection_sync_or_recording_paths():
    import ast
    import inspect

    forbidden = {
        "ai_person_detector", "motion_detector", "save_yolo_events", "append_analytics_event",
        "analytics_sync", "recording_uploader", "recording_upload_worker",
    }
    for func in (main._customer_detection_events, main._build_analytics_summary,
                 main.analytics_event_search, main.analytics_summary_api):
        tree = ast.parse(inspect.getsource(func))
        referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                referenced.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                referenced.add(node.module)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
        overlap = referenced & forbidden
        assert not overlap, f"{func.__name__} unexpectedly references {overlap}"
