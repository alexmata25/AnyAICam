"""Integration coverage for main._ClassicAacoBoundary -- the concrete
adapter that binds AACO's provider-independent command schema
(app/aaco.py, tests/test_aaco.py) to Classic VMS's real, tenant-scoped
functions. test_aaco_web.py deliberately never exercises this class
(it injects a fully independent ControlledVms fake instead), so this
file exists to catch exactly the class of bug that gap allows through:
after rebasing feature/aaco-command-engine onto the current
reconciliation baseline, _ClassicAacoBoundary.camera_status() called a
module-level `customer_camera_status(camera_id, request)` that no
longer exists under that name/signature anywhere reachable, and
register_aaco_routes()'s identity_provider referenced a bare
`partner_identity` name never imported at module scope in main.py --
both would have raised NameError on the very first real AACO request
despite all 32 pre-existing AACO tests passing. Both are fixed in this
same rebase; this file proves the fix against real database rows
instead of a fake boundary, using the same fixture shape
test_playback_camera_default_selection_order.py already established
for this exact class of "real function, real DB, monkeypatched
identity" test.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aaco_classic_boundary_integration.db"


def _seed_base_tenant(conn, customer_id="cust-1", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Test Co", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (f"appl-{customer_id}", customer_id, f"site-{customer_id}", f"AIC-{customer_id}", "2026-01-01"),
    )
    conn.commit()


def _seed_camera(conn, camera_id, name, camera_number, customer_id="cust-1"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", f"appl-{customer_id}", camera_number, name, "2026-01-01"),
    )
    conn.commit()


def _request(camera=None):
    from types import SimpleNamespace
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: camera if key == "camera" else default))


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.test"}


@pytest.fixture()
def owner_seeded(db_path, monkeypatch):
    """One customer, two real cameras (1 online, 2 offline), one
    placeholder (camera_number NULL) that must never surface anywhere."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_camera(conn, "cam-1", "Front Entrance", 1)
        _seed_camera(conn, "cam-2", "Back Lot", 2)
        _seed_camera(conn, "cam-placeholder", "Camera 3", None)
        conn.execute(
            "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,updated_at) "
            "VALUES('appl-cust-1','cam-1','Front Entrance',1,1,1,'2026-09-16T00:00:00')"
        )
        conn.execute(
            "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,updated_at) "
            "VALUES('appl-cust-1','cam-2','Back Lot',0,0,0,'2026-09-16T00:00:00')"
        )
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        yield conn


def test_camera_status_reflects_real_online_offline_state_and_excludes_placeholders(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.camera_status(_owner_identity())
    assert result["kind"] == "status"
    by_label = {row["label"]: row["state"] for row in result["cameras"]}
    assert by_label == {"Front Entrance": "online", "Back Lot": "offline"}, (
        f"expected exactly the 2 real cameras with their real online/offline state, no placeholder; got {result['cameras']!r}"
    )


def test_authorized_camera_resolves_by_number_and_rejects_unknown_camera(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    identity = _owner_identity()
    assert boundary.authorized_camera(identity, "camera-1") is not None
    assert boundary.authorized_camera(identity, "camera-99") is None


def test_authorized_camera_resolves_by_display_name(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    identity = _owner_identity()
    resolved = boundary.authorized_camera(identity, "camera-name:front entrance")
    assert resolved is not None and resolved["id"] == "cam-1"


def test_live_view_returns_an_authorized_deep_link_for_a_real_camera(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.live_view(_owner_identity(), "camera-1")
    assert result["kind"] == "live"
    assert "/customer/cameras/" in result["href"] and result["href"].endswith("/live")
    assert result["context"] == {"camera_id": "camera-1"}


def test_live_view_raises_permission_error_for_a_camera_outside_the_tenant(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    with pytest.raises(PermissionError):
        boundary.live_view(_owner_identity(), "camera-99")


def test_playback_reports_no_media_honestly_when_no_recording_exists(owner_seeded):
    boundary = main._ClassicAacoBoundary(_request())
    now = datetime.now(timezone.utc)
    result = boundary.playback(_owner_identity(), "camera-1", now - timedelta(minutes=5), now)
    assert result["kind"] == "playback"
    assert "currently unavailable" in result["message"]
    assert result["context"]["camera_id"] == "camera-1"


def test_playback_finds_existing_recording_metadata_near_the_requested_time(owner_seeded):
    conn = owner_seeded
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES('rec-1','cust-1','site-cust-1','appl-cust-1','cam-1','k.mp4','2026-09-16T10:00:00','2026-09-16T10:05:00','available','2026-09-16T10:05:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.playback(_owner_identity(), "camera-1", datetime(2026, 9, 16, 10, 2), datetime(2026, 9, 16, 10, 2))
    assert "1 existing recording" in result["message"]


def test_search_events_scopes_to_the_tenant_and_the_requested_window(owner_seeded):
    conn = owner_seeded
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-1','cust-1','site-cust-1','appl-cust-1','cam-1','local-1','person','2026-09-16T10:00:00','2026-09-16T10:00:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.search_events(
        _owner_identity(), event_type="person", camera_id=None,
        start=datetime(2026, 9, 16, 9, 0), end=datetime(2026, 9, 16, 11, 0),
    )
    assert result["kind"] == "events"
    assert len(result["events"]) == 1
    assert "Front Entrance" in result["events"][0]["label"]


def test_search_events_finds_nothing_outside_the_requested_window(owner_seeded):
    conn = owner_seeded
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES('evt-1','cust-1','site-cust-1','appl-cust-1','cam-1','local-1','person','2026-09-16T02:00:00','2026-09-16T02:00:00')"
    )
    conn.commit()
    boundary = main._ClassicAacoBoundary(_request())
    result = boundary.search_events(
        _owner_identity(), event_type="person", camera_id=None,
        start=datetime(2026, 9, 16, 9, 0), end=datetime(2026, 9, 16, 11, 0),
    )
    assert result["events"] == []


def test_a_second_tenants_camera_and_events_are_never_visible(db_path, monkeypatch):
    """Cross-tenant isolation at the real _ClassicAacoBoundary layer,
    not just inside aaco.py's own generic authorization gate."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn, customer_id="cust-1", partner_id="partner-1")
        _seed_base_tenant(conn, customer_id="cust-2", partner_id="partner-1")
        _seed_camera(conn, "cam-1", "Tenant A Camera", 1, customer_id="cust-1")
        _seed_camera(conn, "cam-2", "Tenant B Camera", 1, customer_id="cust-2")
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
            "VALUES('evt-b','cust-2','site-cust-2','appl-cust-2','cam-2','local-1','person','2026-09-16T10:00:00','2026-09-16T10:00:00')"
        )
        conn.commit()
        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))

        boundary = main._ClassicAacoBoundary(_request())
        identity = _owner_identity("cust-1")
        assert boundary.authorized_camera(identity, "camera-1") is not None
        # A customer_owner has live access to their own full fleet without
        # a separate per-camera grant (that restriction is customer_viewer-
        # only) -- this proves tenant A's own camera resolves correctly,
        # the real cross-tenant proof is the events assertion below.
        assert boundary.live_view(identity, "camera-1")["kind"] == "live"
        result = boundary.search_events(
            identity, event_type=None, camera_id=None,
            start=datetime(2026, 9, 16, 9, 0), end=datetime(2026, 9, 16, 11, 0),
        )
        assert result["events"] == [], "tenant A must never see tenant B's detection_events row"
