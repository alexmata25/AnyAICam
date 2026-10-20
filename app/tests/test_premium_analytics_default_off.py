"""Premium analytics default OFF (owner, 2026-10-19; Linux 1.2.7 and Windows 1.3.2 final builds).

Required behaviour, tested here against the real code paths:
  * fresh installation: no premium analytic runs; basic motion/recording/live/playback never depend on licensing;
  * premium analytics run only with BOTH a valid entitlement (per camera) AND an explicit customer activation;
  * purchasing/assigning unlocks (authorized, listed in the signed snapshot) but does not turn ON;
  * enforcement is in the processing pipeline (recording_uploader._camera_identity on the appliance), not the UI;
  * revocation / expiry / an unverifiable or older snapshot stops premium processing (fail closed);
  * upgrades keep what was ON (explicit rows seeded once) and never enable anything that was OFF;
  * Local mode: an assignment alone does not start an analytic; an explicit ON is needed.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import analytics_control_center as acc
import appliance_entitlements
import db_migrations
import recording_uploader
from database_backend import override_target
from partner_db import connection, initialize_database
from test_appliance_feature_entitlements import (  # noqa: F401 -- fixtures and helpers
    EDGE_IDENTITY, _cached, _resign, _sync, appliance, isolated_product_mode_state, prices)
from test_aac_voice_call_cloud_edge_flow import (  # noqa: F401
    appliance_cloud, cloud_client, cloud_db, edge_camera_sync, edge_db, edge_env)

PREMIUM_FIELDS = ("smart_motion_enabled", "people_counting_enabled", "lpr_enabled", "ppe_enabled", "facial_recognition_enabled")
NOW = "2026-10-19T00:00:00"


@pytest.fixture(autouse=True)
def _fresh_grant_cache(tmp_path, monkeypatch):
    # 168 h design: the analytics grant lives in its own MAC-protected state file, keyed from ANYAICAM_APP_SECRETS.
    monkeypatch.setenv("ANYAICAM_APP_SECRETS", "test-appliance-secret-0123456789abcdef")
    monkeypatch.setattr(appliance_entitlements, "ANALYTICS_STATE_FILE", tmp_path / "analytics_entitlements.json")
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    yield
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})


def _doc():
    """The stored (MAC-verified) analytics document, or None."""
    state = appliance_entitlements._read_analytics_state()
    return state["grant"] if state else None


def _rows(path, sql, params=()):
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _exec(path, sql, params=()):
    with override_target(sqlite_path=str(path)):
        with connection() as db:
            db.execute(sql, params)


# ---------------------------------------------------------------- operational switch defaults (cloud + appliance DB)

@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "p.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as conn:
            conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p','P','x')")
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('c','p','C','c@x.test','active','x')")
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s','c','S','x')")
            conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('a','c','s','AIC-A','x')")
            for n in (1, 2, 3):
                conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,created_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (f"cam-{n}", "c", "s", "a", f"Cam {n}", "configured", n, "x"))
    return path


def test_a_missing_operations_row_is_off_for_every_premium_analytic(db):
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            for key in ("smart_motion", "people_counting", "lpr", "ppe", "facial_recognition"):
                assert acc.operational_enabled(conn, "cam-1", key) is False
                status = acc.operational_status(conn, "cam-1", key)
                assert status["customer_enabled"] is False and status["effective_enabled"] is False


def test_an_explicit_on_row_is_on_and_off_or_paused_is_off(db):
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-1','lpr',1,'x')")
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-2','lpr',0,'x')")
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,system_paused,updated_at) VALUES('cam-3','lpr',1,1,'x')")
            assert acc.operational_enabled(conn, "cam-1", "lpr") is True
            assert acc.operational_enabled(conn, "cam-2", "lpr") is False
            assert acc.operational_enabled(conn, "cam-3", "lpr") is False


def test_a_system_pause_on_an_analytic_with_no_row_can_never_turn_it_on(db):
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            acc.set_system_pause(conn, "cam-1", "ppe", paused=True, reason="thermal")
            assert acc.operational_enabled(conn, "cam-1", "ppe") is False
            acc.set_system_pause(conn, "cam-1", "ppe", paused=False)
            assert acc.operational_enabled(conn, "cam-1", "ppe") is False   # preference stayed OFF (the default)
            row = conn.execute("SELECT enabled FROM camera_analytics_operations WHERE camera_id='cam-1' AND analytic_key='ppe'").fetchone()
            assert row["enabled"] == 0


# ---------------------------------------------------------------- upgrade migration (runs once)

def _assign(conn, camera, key, *, flag=True, status="active"):
    conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (camera, key, status, NOW, NOW))
    column = {"smart_motion": "smart_motion_enabled", "people_counting": "people_counting_enabled", "lpr": "lpr_enabled",
              "ppe": "ppe_enabled"}.get(key)
    if column:
        conn.execute(f"UPDATE cameras SET {column}=? WHERE id=?", (1 if flag else 0, camera))


def _sub(conn, key, status="active", quantity=5):
    conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (f"sub-{key}", "c", "s", key, status, quantity, NOW))


def _ops(conn):
    return {(r["camera_id"], r["analytic_key"]): (r["enabled"], r["updated_by"]) for r in
            conn.execute("SELECT camera_id,analytic_key,enabled,updated_by FROM camera_analytics_operations").fetchall()}


def test_cloud_upgrade_preserves_on_only_where_the_entitlement_is_still_valid(db, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            _sub(conn, "lpr"); _sub(conn, "facial_recognition"); _sub(conn, "ppe")
            _sub(conn, "people_counting", status="cancelled")            # lapsed entitlement
            _assign(conn, "cam-1", "lpr")                                # ran before + valid -> stays ON
            _assign(conn, "cam-1", "facial_recognition")                 # facial: assignment alone + valid -> ON
            _assign(conn, "cam-2", "lpr", flag=False)                    # assigned but flag off -> no row (OFF)
            _assign(conn, "cam-2", "ppe")
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-2','ppe',0,'x')")
            _assign(conn, "cam-3", "people_counting")                    # ran before but entitlement LAPSED -> no row
            conn.execute("UPDATE cameras SET smart_motion_enabled=1 WHERE id='cam-3'")   # flag without assignment -> no row
            seeded = db_migrations.seed_explicit_analytics_operations(conn)
            ops = _ops(conn)
            assert seeded == 2
            assert ops[("cam-1", "lpr")] == (1, "migration:" + db_migrations.SEED_EXPLICIT_ANALYTICS_OPERATIONS)
            assert ops[("cam-1", "facial_recognition")][0] == 1
            assert ops[("cam-2", "ppe")] == (0, None)                              # an explicit OFF is never changed
            for absent in (("cam-2", "lpr"), ("cam-3", "people_counting"), ("cam-3", "smart_motion")):
                assert absent not in ops
            assert db_migrations.seed_explicit_analytics_operations(conn) == 0     # idempotent
            # the lapsed analytic stays OFF even after a re-purchase: the customer must turn it on again
            conn.execute("UPDATE analytics_subscriptions SET status='active' WHERE id='sub-people_counting'")
            assert acc.operational_enabled(conn, "cam-3", "people_counting") is False
            assert acc.operational_enabled(conn, "cam-1", "lpr") is True


def test_appliance_upgrade_defers_and_preserves_on_only_with_a_valid_signed_grant(db, monkeypatch):
    """Blue's finding: an edge never holds camera_analytics_entitlements and has no grant at first boot. The upgrade
    records the pairs that were ON (cameras.<flag>=1) and writes explicit ON only once a valid grant covers them."""
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            conn.execute("UPDATE cameras SET lpr_enabled=1 WHERE id IN ('cam-1','cam-2')")
            conn.execute("UPDATE cameras SET ppe_enabled=1 WHERE id='cam-1'")
            conn.execute("UPDATE cameras SET facial_recognition_enabled=1 WHERE id='cam-3'")
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-3','facial_recognition',0,'x')")
            recorded = db_migrations.seed_explicit_analytics_operations(conn)
            assert recorded == 3                                 # cam-1 lpr+ppe, cam-2 lpr; cam-3 has an explicit OFF
            assert _ops(conn) == {("cam-3", "facial_recognition"): (0, None)}
            # no valid grant yet (first boot): nothing written, the list is kept
            assert db_migrations.complete_pending_analytics_seed(conn, {}, grant_held=False) == 0
            assert conn.execute("SELECT COUNT(*) AS n FROM analytics_upgrade_seed_pending").fetchone()["n"] == 3
            # the first valid grant covers cam-1 lpr only
            written = db_migrations.complete_pending_analytics_seed(conn, {"cam-1": frozenset({"lpr"})}, grant_held=True)
            assert written == 1
            ops = _ops(conn)
            assert ops[("cam-1", "lpr")][0] == 1 and ops[("cam-1", "lpr")][1].endswith(":deferred")
            assert ("cam-1", "ppe") not in ops and ("cam-2", "lpr") not in ops   # not granted -> stays OFF
            assert conn.execute("SELECT COUNT(*) AS n FROM analytics_upgrade_seed_pending").fetchone()["n"] == 0  # one-shot
            # later grants never seed again (lapsed analytics need the customer's explicit ON)
            assert db_migrations.complete_pending_analytics_seed(conn, {"cam-2": frozenset({"lpr"})}, grant_held=True) == 0


def test_blue_reproduction_local_edge_lpr_survives_the_upgrade_with_a_valid_grant(db, monkeypatch):
    """BLUE.md 22:45: SEEDED 0 / LPR off after upgrade. Now: recorded, then preserved at the first valid grant."""
    import product_mode
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(product_mode, "current_mode", lambda: "local")
    monkeypatch.setattr(appliance_entitlements, "camera_analytic_grants", lambda now=None: {"cam-1": frozenset({"lpr"})})
    monkeypatch.setattr(appliance_entitlements, "analytics_grant_held", lambda now=None: True)
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            conn.execute("UPDATE cameras SET lpr_enabled=1 WHERE id='cam-1'")
            assert db_migrations.seed_explicit_analytics_operations(conn) == 1
        recording_uploader._refresh_camera_map(prefer_local=True)
    assert recording_uploader._camera_identity(1)["lpr_enabled"] is True


@pytest.mark.parametrize("role", ["", "something-else"])
def test_an_unknown_runtime_role_preserves_nothing(db, monkeypatch, role):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", role)
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            _sub(conn, "lpr"); _assign(conn, "cam-1", "lpr")
            assert db_migrations.seed_explicit_analytics_operations(conn) == 0


def test_the_upgrade_migration_runs_once_and_a_fresh_install_seeds_nothing(tmp_path):
    path = tmp_path / "fresh.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        initialize_database()   # a restart
        with connection() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM camera_analytics_operations").fetchone()["n"] == 0
            marks = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE version=?",
                                 (db_migrations.SEED_EXPLICIT_ANALYTICS_OPERATIONS,)).fetchone()["n"]
            assert marks == 1
            # after the one-time migration, a later assignment never gets an implicit ON
            conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p','P','x')")
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('c','p','C','c@x.test','active','x')")
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s','c','S','x')")
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-9','c','s','N','configured',9,'x')")
            _assign(conn, "cam-9", "lpr")
    with override_target(sqlite_path=str(path)):
        initialize_database()   # restart after the assignment: the marker prevents a re-seed
        with connection() as conn:
            assert acc.operational_enabled(conn, "cam-9", "lpr") is False


# ---------------------------------------------------------------- Local mode: an assignment alone does not start it

def test_local_mode_requires_an_explicit_on_even_with_the_camera_flag_set(db, monkeypatch):
    import product_mode
    monkeypatch.setattr(product_mode, "current_mode", lambda: "local")
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            conn.execute("UPDATE cameras SET lpr_enabled=1,ppe_enabled=1 WHERE id='cam-1'")
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-1','lpr',1,'x')")
        recording_uploader._refresh_camera_map(prefer_local=True)
    entry = recording_uploader._camera_map[1]
    assert entry["lpr_enabled"] is True      # explicit ON (still subject to the signed entitlement on an edge)
    assert entry["ppe_enabled"] is False     # flag set but never turned ON -> OFF


def test_hybrid_mode_keeps_cloud_effective_flags_and_still_honours_a_local_off(db, monkeypatch):
    import product_mode
    monkeypatch.setattr(product_mode, "current_mode", lambda: "hybrid")
    monkeypatch.setattr(recording_uploader.appliance_config_cache, "get_or_fetch", lambda fetch: {"cameras": [
        {"id": "cam-1", "site_id": "s", "camera_number": 1, "lpr_enabled": True, "ppe_enabled": True}]})
    with override_target(sqlite_path=str(db)):
        with connection() as conn:
            conn.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-1','ppe',0,'x')")
        recording_uploader._refresh_camera_map()
    entry = recording_uploader._camera_map[1]
    assert entry["lpr_enabled"] is True and entry["ppe_enabled"] is False


# ---------------------------------------------------------------- pipeline gate on an appliance (signed entitlement)

def _put_identity(monkeypatch, **flags):
    identity = {"camera_id": "cam-1", "site_id": "site-1", "cloud_recording_mode": "motion", "analytics_lab_leases": {},
                **{f: False for f in PREMIUM_FIELDS}, **flags}
    monkeypatch.setitem(recording_uploader._camera_map, 1, identity)
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    return identity


def test_without_any_snapshot_every_premium_flag_is_off_but_camera_identity_survives(monkeypatch):
    monkeypatch.setattr(appliance_entitlements, "_read_state", lambda: {})
    _put_identity(monkeypatch, **{f: True for f in PREMIUM_FIELDS})
    gated = recording_uploader._camera_identity(1)
    assert all(gated[f] is False for f in PREMIUM_FIELDS)
    assert gated["camera_id"] == "cam-1" and gated["cloud_recording_mode"] == "motion"   # basic paths unaffected
    assert recording_uploader._camera_map[1]["lpr_enabled"] is True                       # the source map is not mutated


@pytest.mark.parametrize("role", ["cloud", "combined"])
def test_non_edge_roles_are_not_gated_by_the_appliance_snapshot(monkeypatch, role):
    monkeypatch.setattr(appliance_entitlements, "_read_state", lambda: {})
    _put_identity(monkeypatch, lpr_enabled=True)
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", role)
    assert recording_uploader._camera_identity(1)["lpr_enabled"] is True


def test_the_ai_activation_gate_sees_no_premium_analytic_without_entitlement(monkeypatch):
    import main
    monkeypatch.setattr(appliance_entitlements, "_read_state", lambda: {})
    _put_identity(monkeypatch, **{f: True for f in PREMIUM_FIELDS})
    assert main.effective_analytics(recording_uploader._camera_identity(1), 1) == []


# ---------------------------------------------------------------- end to end: real cloud route -> real appliance sync

def _cloud_lpr_purchase(cloud_db, *, assign=True, enable=None, subscription="active"):
    _exec(cloud_db, "INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) "
                    "VALUES('sub-lpr','cust-1','site-1','lpr',?,?)", (subscription, NOW))
    if assign:
        _exec(cloud_db, "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
                        "VALUES('cam-1','lpr','active',?,?)", (NOW, NOW))
        _exec(cloud_db, "UPDATE cameras SET lpr_enabled=1 WHERE id='cam-1'")
    if enable is not None:
        _exec(cloud_db, "INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) "
                        "VALUES('cam-1','lpr',?,?,'owner@example.test') ON CONFLICT(camera_id,analytic_key) DO UPDATE SET enabled=excluded.enabled",
              (1 if enable else 0, NOW))


def _edge_lpr(monkeypatch, *, edge_flag=True):
    """What the appliance's processing pipeline sees for camera 1's LPR (flag possibly stale/forced locally)."""
    _put_identity(monkeypatch, lpr_enabled=edge_flag)
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    return recording_uploader._camera_identity(1)["lpr_enabled"]


def _edge_camera_flag(edge_db):
    return bool(_rows(edge_db, "SELECT lpr_enabled FROM cameras WHERE id='cam-1'")[0]["lpr_enabled"])


def test_fresh_install_nothing_premium_is_authorized_or_running(appliance, cloud_db, edge_db, monkeypatch):
    _sync(edge_db)
    assert _doc()["camera_analytics"] == {}
    assert "camera_analytics" not in _cached()["snapshot"]          # the 24 h feature snapshot is back to its RC7 shape
    assert _edge_camera_flag(edge_db) is False
    assert _edge_lpr(monkeypatch) is False        # even a locally forced flag cannot run it


def test_purchase_and_assignment_unlock_but_do_not_turn_on(appliance, cloud_db, edge_db, monkeypatch):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=None)
    _sync(edge_db)
    assert _doc()["camera_analytics"] == {"cam-1": ["lpr"]}   # available (licensed)
    assert _edge_camera_flag(edge_db) is False                                 # but OFF until the customer turns it on
    assert _edge_lpr(monkeypatch, edge_flag=False) is False


def test_explicit_activation_turns_a_licensed_analytic_on_and_off_again(appliance, cloud_db, edge_db, monkeypatch):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    assert _edge_camera_flag(edge_db) is True
    assert _edge_lpr(monkeypatch) is True
    _cloud_lpr_purchase_off = _exec(cloud_db, "UPDATE camera_analytics_operations SET enabled=0 WHERE camera_id='cam-1' AND analytic_key='lpr'")
    _sync(edge_db)
    assert _edge_camera_flag(edge_db) is False


def test_enabled_without_a_license_never_runs(appliance, cloud_db, edge_db, monkeypatch):
    _exec(cloud_db, "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1','lpr','active',?,?)", (NOW, NOW))
    _exec(cloud_db, "UPDATE cameras SET lpr_enabled=1 WHERE id='cam-1'")
    _exec(cloud_db, "INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at) VALUES('cam-1','lpr',1,?)", (NOW,))
    _sync(edge_db)   # assignment + ON, but no subscription
    assert _doc()["camera_analytics"] == {}
    assert _edge_camera_flag(edge_db) is False and _edge_lpr(monkeypatch) is False


@pytest.mark.parametrize("ended", ["cancelled", "suspended"])
def test_revocation_stops_processing_at_the_next_sync_even_with_a_stale_local_flag(appliance, cloud_db, edge_db, monkeypatch, ended):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    assert _edge_lpr(monkeypatch) is True
    _exec(cloud_db, "UPDATE analytics_subscriptions SET status=? WHERE id='sub-lpr'", (ended,))
    _sync(edge_db)
    assert _doc()["camera_analytics"] == {}
    assert _edge_lpr(monkeypatch, edge_flag=True) is False
    # the customer's ON choice is preserved: re-subscribing restores it without a new activation
    _exec(cloud_db, "UPDATE analytics_subscriptions SET status='active' WHERE id='sub-lpr'")
    _sync(edge_db)
    assert _edge_lpr(monkeypatch) is True


def test_offline_use_is_authorized_until_the_analytics_grant_expires_then_stops(appliance, cloud_db, edge_db, monkeypatch):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    doc = _doc()
    now = datetime.now(timezone.utc)
    expired = _resign(doc, cloud_db, issued_at=(now - timedelta(hours=169)).isoformat(), expires_at=(now - timedelta(hours=1)).isoformat())
    appliance_entitlements._write_analytics_state(expired, now)
    assert _edge_lpr(monkeypatch) is False
    fresh = _resign(doc, cloud_db, issued_at=(now - timedelta(hours=20)).isoformat(), expires_at=(now + timedelta(hours=148)).isoformat())
    appliance_entitlements._write_analytics_state(fresh, now)
    assert _edge_lpr(monkeypatch) is True   # cloud unreachable, grant still valid (20 h after refresh) -> offline use


@pytest.mark.parametrize("variant", ["tampered", "other_appliance", "feature_snapshot_as_grant"])
def test_an_unverifiable_or_foreign_grant_denies_premium_processing(appliance, cloud_db, edge_db, monkeypatch, variant):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    doc = _doc()
    if variant == "tampered":
        bad = dict(doc); bad["camera_analytics"] = {"cam-1": ["lpr", "ppe", "facial_recognition"]}
    elif variant == "other_appliance":
        bad = _resign(doc, cloud_db, appliance_id="appl-other")
    else:
        bad = _cached()["snapshot"]          # a valid 24 h FEATURE snapshot is never an analytics grant (type)
    appliance_entitlements._write_analytics_state(bad, datetime.now(timezone.utc))
    assert _edge_lpr(monkeypatch) is False


def test_a_cloud_that_stops_sending_the_grant_revokes_at_the_next_sync(appliance, cloud_db, edge_db, monkeypatch):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    assert _edge_lpr(monkeypatch) is True
    appliance["rewrite"] = lambda response: {k: v for k, v in response.items() if k != "analytics_entitlements"}
    _sync(edge_db)
    assert _doc() is None and _edge_lpr(monkeypatch) is False


def test_a_grant_for_one_camera_does_not_license_another(appliance, cloud_db, edge_db, monkeypatch):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    monkeypatch.setitem(recording_uploader._camera_map, 2, {"camera_id": "cam-2", "site_id": "site-1", "lpr_enabled": True,
                                                            "analytics_lab_leases": {}})
    monkeypatch.setattr(recording_uploader, "RUNTIME_ROLE", "edge")
    assert recording_uploader._camera_identity(2)["lpr_enabled"] is False


def test_the_vms_turn_on_refuses_an_unlicensed_camera_on_an_appliance(appliance, cloud_db, edge_db, monkeypatch):
    from test_analytics_control_center import _call  # route-invocation helper
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    import partner_portal
    _sync(edge_db)   # nothing licensed
    _exec(edge_db, "INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','P','x')")
    _exec(edge_db, "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@x.test','active','x')")
    _exec(edge_db, "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                   "VALUES('owner','partner-1','owner@example.test','Owner','customer_owner','x',1,'cust-1','x','active','all')")
    _exec(edge_db, "INSERT OR IGNORE INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1','lpr','active','x','x')")
    _exec(edge_db, "INSERT OR IGNORE INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) VALUES('s','cust-1','site-1','lpr','active','x')")
    with override_target(sqlite_path=str(edge_db)):
        app = FastAPI(); acc.register_routes(app, lambda title, nav, content: HTMLResponse(content))
        cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)}
        refused = _call(app, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr", payload={"enabled": True}, cookies=cookie)
        assert refused.status_code == 409 and "isn't licensed" in refused.json()["detail"]
        assert _call(app, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr", payload={"enabled": False}, cookies=cookie).status_code == 200
