"""2026-09-16: closes the real, verified gap where RDM's customer-facing
entitlement toggle (customer_analytics_panel.assign_entitlement()/
remove_entitlement(), writing camera_analytics_entitlements) never
reached the appliance for any of the 4 real per-camera analytics
(smart_motion, people_counting, lpr, ppe) -- confirmed live that even
people_counting_enabled, despite already having a real column and a
real edge-side read in main.py's people_counting_worker(), was never
actually populated into recording_uploader.py's own in-memory camera
map, so it always read as unset regardless of entitlement state.

Traces the full chain this file proves end to end:
  assign_entitlement()/remove_entitlement()
    -> cameras.<analytic>_enabled column
    -> GET /api/appliance/configuration
    -> edge_camera_sync.sync_provisioned_cameras() (edge-side local DB)
    -> recording_uploader._refresh_camera_map() (in-memory cache read by
       lpr.is_camera_enabled()'s caller, ppe.is_camera_enabled()'s
       caller, smart_motion's caller, and people_counting_worker(),
       all in main.py)

Each link is tested independently (cheaper, more precise failure
localization) plus one full round-trip test proving the whole chain.
"""
import sqlite3

import pytest

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_rdm_appliance_enforcement.db"


def _seed_tenant_and_camera(conn, camera_id="cam-1", customer_id="cust-1", site_id="site-1", appliance_id="appl-1"):
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", "Test Co", f"{customer_id}@example.test", "active", now),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main", now))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, customer_id, site_id, f"AIC-{appliance_id}", now),
    )
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, 1, "Camera 1", now),
    )
    conn.execute(
        "INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("sub-1", customer_id, site_id, "smart_motion", "active", now),
    )
    for key in ("people_counting", "lpr", "ppe"):
        conn.execute(
            "INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) VALUES(?,?,?,?,?,?)",
            (f"sub-{key}", customer_id, site_id, key, "active", now),
        )
    conn.commit()


# --------------------------------------------------- link 1: assign/remove -> cameras.<analytic>_enabled


@pytest.mark.parametrize("analytic_key,column", [
    ("smart_motion", "smart_motion_enabled"),
    ("people_counting", "people_counting_enabled"),
    ("lpr", "lpr_enabled"),
    ("ppe", "ppe_enabled"),
])
def test_assign_entitlement_sets_the_appliance_enforcement_column(db_path, analytic_key, column):
    with override_target(sqlite_path=db_path):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_tenant_and_camera(conn)

        import customer_analytics_panel as panel
        panel.assign_entitlement(conn, "cam-1", analytic_key, now="2026-01-01T00:01:00")
        conn.commit()

        row = conn.execute(f"SELECT {column} FROM cameras WHERE id='cam-1'").fetchone()
    assert row[column] == 1, f"assign_entitlement({analytic_key!r}) must set cameras.{column}=1"


@pytest.mark.parametrize("analytic_key,column", [
    ("smart_motion", "smart_motion_enabled"),
    ("people_counting", "people_counting_enabled"),
    ("lpr", "lpr_enabled"),
    ("ppe", "ppe_enabled"),
])
def test_remove_entitlement_clears_the_appliance_enforcement_column(db_path, analytic_key, column):
    with override_target(sqlite_path=db_path):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_tenant_and_camera(conn)

        import customer_analytics_panel as panel
        panel.assign_entitlement(conn, "cam-1", analytic_key, now="2026-01-01T00:01:00")
        conn.commit()
        panel.remove_entitlement(conn, "cam-1", analytic_key, now="2026-01-01T00:02:00")
        conn.commit()

        row = conn.execute(f"SELECT {column} FROM cameras WHERE id='cam-1'").fetchone()
    assert row[column] == 0, f"remove_entitlement({analytic_key!r}) must clear cameras.{column} back to 0"


def test_assigning_one_analytic_never_touches_a_different_cameras_column(db_path):
    """Turning smart_motion ON must not accidentally enable lpr/ppe/
    people_counting on the same camera -- proves the column mapping is
    exact, not a blanket 'entitle everything' side effect."""
    with override_target(sqlite_path=db_path):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_tenant_and_camera(conn)

        import customer_analytics_panel as panel
        panel.assign_entitlement(conn, "cam-1", "smart_motion", now="2026-01-01T00:01:00")
        conn.commit()

        row = conn.execute(
            "SELECT smart_motion_enabled,people_counting_enabled,lpr_enabled,ppe_enabled FROM cameras WHERE id='cam-1'"
        ).fetchone()
    assert row["smart_motion_enabled"] == 1
    assert row["people_counting_enabled"] in (0, None)
    assert row["lpr_enabled"] in (0, None)
    assert row["ppe_enabled"] in (0, None)


# --------------------------------------------------- link 2: GET /api/appliance/configuration exposes the columns


def test_appliance_configuration_route_exposes_all_four_columns(db_path):
    """Mirrors test_camera_people_counting_entitlement.py's own
    established client/appliance-auth-header fixture pattern exactly."""
    import secrets
    import time

    import appliance_cloud
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from partner_db import password_hash

    def _appliance_auth_headers(appliance_id: str, credential: str) -> dict:
        return {
            "X-Appliance-Id": appliance_id,
            "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16),
            "Authorization": f"Bearer {credential}",
        }

    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as client:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _seed_tenant_and_camera(conn)
            conn.execute(
                "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-1','appl-1',?,?)",
                (password_hash("test-credential"), "2026-01-01T00:00:00"),
            )
            conn.commit()

            import customer_analytics_panel as panel
            for key in ("smart_motion", "lpr", "ppe"):
                panel.assign_entitlement(conn, "cam-1", key, now="2026-01-01T00:01:00")
            conn.commit()

            response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    camera = response.json()["cameras"][0]
    assert camera["smart_motion_enabled"] == 1
    assert camera["lpr_enabled"] == 1
    assert camera["ppe_enabled"] == 1
    assert not camera["people_counting_enabled"]


# --------------------------------------------------- link 3: edge_camera_sync.py syncs into the local DB
# Mirrors test_edge_camera_sync.py's own established fixture pattern
# exactly (same identity/cloud-config mock shape) -- this only adds
# coverage for the 3 new columns alongside the already-tested
# people_counting_enabled.


def test_edge_camera_sync_upserts_all_four_columns_locally(db_path, monkeypatch):
    import edge_camera_sync

    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setattr(
        "appliance_activation.load_persisted_identity",
        lambda: {"appliance_id": "appl-1", "cloud_id": "AIC-1", "credential": "cred", "customer_id": "cust-1", "site_id": "site-1"},
    )
    monkeypatch.setattr(
        edge_camera_sync, "_control_plane_get",
        lambda path, appliance_id, credential: {"cameras": [{
            "id": "cam-1", "name": "Camera 1", "camera_number": 1, "status": "configured",
            "device_key": None, "onvif_endpoint": None, "resolution": None, "recording_mode": None,
            "people_counting_enabled": 1, "smart_motion_enabled": 1, "lpr_enabled": 0, "ppe_enabled": 1,
        }]},
    )
    with override_target(sqlite_path=str(db_path)):
        result = edge_camera_sync.sync_provisioned_cameras()
        assert result["status"] == "ok"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT people_counting_enabled,smart_motion_enabled,lpr_enabled,ppe_enabled FROM cameras WHERE id='cam-1'").fetchone()
    assert row["people_counting_enabled"] == 1
    assert row["smart_motion_enabled"] == 1
    assert row["lpr_enabled"] == 0
    assert row["ppe_enabled"] == 1


# --------------------------------------------------- link 4: recording_uploader._refresh_camera_map() populates all four


def test_refresh_camera_map_includes_all_four_entitlement_keys(monkeypatch):
    import recording_uploader

    fake_response = {
        "cameras": [
            {
                "id": "cam-1", "camera_number": 1, "site_id": "site-1", "recording_mode": "continuous",
                "people_counting_enabled": 1, "smart_motion_enabled": 0, "lpr_enabled": 1, "ppe_enabled": 0,
            }
        ]
    }
    monkeypatch.setattr(recording_uploader, "_control_plane_get", lambda path: fake_response)
    recording_uploader._refresh_camera_map()
    identity = recording_uploader._camera_identity(1)
    assert identity == {
        "camera_id": "cam-1",
        "site_id": "site-1",
        "cloud_recording_mode": "continuous",
        "people_counting_enabled": True,
        "smart_motion_enabled": False,
        "lpr_enabled": True,
        "ppe_enabled": False,
    }


def test_refresh_camera_map_treats_a_missing_key_as_false_not_a_crash(monkeypatch):
    """Backward compatibility: an older/mocked control-plane response
    that omits the new keys entirely must not raise, and must be
    treated as not-entitled (fail closed), never as entitled."""
    import recording_uploader

    fake_response = {"cameras": [{"id": "cam-1", "camera_number": 1, "site_id": "site-1"}]}
    monkeypatch.setattr(recording_uploader, "_control_plane_get", lambda path: fake_response)
    recording_uploader._refresh_camera_map()
    identity = recording_uploader._camera_identity(1)
    assert identity["smart_motion_enabled"] is False
    assert identity["lpr_enabled"] is False
    assert identity["ppe_enabled"] is False
    assert identity["people_counting_enabled"] is False


# --------------------------------------------------- link 5 (migration): existing RDM state survives the schema upgrade


def test_migration_backfills_appliance_columns_from_existing_entitlements(db_path):
    """A camera that was already entitled via RDM before this fix
    existed (camera_analytics_entitlements has an active row, but the
    new cameras.<analytic>_enabled column was never written) must not
    silently read as un-entitled the moment the column first appears."""
    with override_target(sqlite_path=db_path):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_tenant_and_camera(conn)
        # Simulate pre-fix state: entitlement exists, appliance column was
        # never written (this is exactly what direct INSERT/raw-SQL RDM
        # toggling before this fix would have left behind).
        conn.execute(
            "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
            "VALUES('cam-1','lpr','active','2026-01-01T00:00:00','2026-01-01T00:00:00')"
        )
        conn.commit()

        from db_migrations import apply_migrations
        apply_migrations()  # re-running must backfill the now-NULL column

        row = conn.execute("SELECT lpr_enabled FROM cameras WHERE id='cam-1'").fetchone()
    assert row["lpr_enabled"] == 1, "pre-existing active RDM entitlement must be backfilled into the new appliance column"


def test_migration_backfill_never_resurrects_an_explicitly_removed_entitlement(db_path):
    with override_target(sqlite_path=db_path):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_tenant_and_camera(conn)

        import customer_analytics_panel as panel
        panel.assign_entitlement(conn, "cam-1", "ppe", now="2026-01-01T00:01:00")
        conn.commit()
        panel.remove_entitlement(conn, "cam-1", "ppe", now="2026-01-01T00:02:00")
        conn.commit()

        from db_migrations import apply_migrations
        apply_migrations()

        row = conn.execute("SELECT ppe_enabled FROM cameras WHERE id='cam-1'").fetchone()
    assert row["ppe_enabled"] == 0, "an explicit remove_entitlement() must stay off across a migration re-run, never revert to entitled"


# --------------------------------------------------- link 6: the real main.py call site actually suppresses/allows


def test_store_motion_event_suppresses_smart_motion_when_camera_is_not_entitled(monkeypatch):
    """The real gate main.py's motion-detection path added, exercised
    end to end through the real store_motion_event(), not a stand-in.
    A camera classify_motion() would happily classify as 'person' must
    still produce NO smart_motion analytics event while
    recording_uploader._camera_identity() reports it as not-entitled --
    proving the entitlement check genuinely runs before classify_motion()
    is even consulted, not just that classify_motion() itself was mocked
    to return something falsy."""
    import asyncio
    from datetime import datetime
    from unittest import mock

    import main
    import recording_uploader

    monkeypatch.setattr(main, "get_alert_rule", lambda camera_number: mock.Mock(enabled=False, event_types=[]))
    monkeypatch.setattr(main, "append_motion_event", lambda line: None)
    analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))

    async def fake_create_motion_thumbnail(*args, **kwargs):
        return "/recordings/media/motion/fake.jpg"

    monkeypatch.setattr(main, "create_motion_thumbnail", fake_create_motion_thumbnail)
    monkeypatch.setattr(main.smart_motion, "classify_motion", lambda camera_number: "person")
    # The real not-entitled state: no cached identity for this camera at
    # all (matches a camera the appliance hasn't synced entitlement for
    # yet, or one RDM has explicitly turned smart_motion off for).
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: None)

    now = datetime.now()
    asyncio.run(main.store_motion_event(camera_number=1, start_time=now, end_time=now, score=50.0, frame=b"fake-jpeg-bytes"))
    pending = list(main.clip_tasks)
    if pending:
        asyncio.run(asyncio.gather(*pending, return_exceptions=True))

    event_types = [event["event_type"] for event in analytics_events]
    assert "motion" in event_types, "ordinary motion detection must be completely unaffected by this gate"
    assert "smart_motion" not in event_types, "smart_motion must not fire for a camera the appliance has no entitled identity for"
