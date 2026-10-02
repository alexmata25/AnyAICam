"""Local <-> Hybrid transitions are acknowledged and retried (2026-10-02).

The cloud queued one restart_vms when it reported a new mode and recorded
the mode as applied at that moment -- no acknowledgment, no retry. A
restart that ran before the VMS had persisted the new mode (or a restart
command that expired) left the appliance running the old mode for good.
The VMS now reports the mode its process is running (?running_mode=); the
cloud stores it as product_mode_applied and re-queues the restart, backed
off, until running == desired.
"""
import importlib
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_product_mode_cloud_config import (  # noqa: F401 -- fixtures and helpers
    _appliance_auth_headers,
    _pending_restart_commands,
    _seed_appliance,
    _seed_tenant,
    client,
    db_path,
)


def _hybrid(db_path):
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)


def _poll(client, running=None):
    path = "/api/appliance/configuration" + (f"?running_mode={running}" if running else "")
    return client.get(path, headers=_appliance_auth_headers("appl-1", "test-credential"))


def _applied(db_path):
    con = sqlite3.connect(db_path)
    return con.execute("SELECT product_mode_applied FROM appliances WHERE id='appl-1'").fetchone()[0]


def _finish_restarts(db_path, age_seconds=0):
    con = sqlite3.connect(db_path)
    created = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    con.execute("UPDATE appliance_commands SET status='completed', created_at=? WHERE command='restart_vms'", (created,))
    con.commit()


def _setup(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-ACK1", "test-credential")
    _hybrid(db_path)


def test_the_running_mode_is_recorded_as_the_acknowledgment(client, db_path):
    _setup(client, db_path)
    _poll(client, "hybrid")
    assert _applied(db_path) == "hybrid"


def test_a_restart_that_did_not_take_effect_is_retried_after_the_backoff(client, db_path):
    _setup(client, db_path)
    _poll(client)                                    # first queue (existing transition logic, from an earlier "local" baseline or none)
    _finish_restarts(db_path)                        # that restart ran -- before the VMS had persisted "hybrid"
    _poll(client, "local")                           # the VMS is still running local
    assert [c for c in _pending_restart_commands(db_path, "appl-1") if c["status"] == "pending"] == []  # within the backoff
    _finish_restarts(db_path, age_seconds=700)        # backoff elapsed
    _poll(client, "local")
    retried = [c for c in _pending_restart_commands(db_path, "appl-1") if c["status"] == "pending"]
    assert len(retried) == 1 and "running local -> hybrid (retry)" in json.loads(retried[0]["payload_json"])["reason"]
    _poll(client, "local")                           # a pending restart is never doubled
    assert len([c for c in _pending_restart_commands(db_path, "appl-1") if c["status"] == "pending"]) == 1


def test_once_the_vms_runs_the_desired_mode_nothing_more_is_queued(client, db_path):
    _setup(client, db_path)
    _poll(client)
    _finish_restarts(db_path, age_seconds=700)
    before = len(_pending_restart_commands(db_path, "appl-1"))
    _poll(client, "hybrid")
    assert len(_pending_restart_commands(db_path, "appl-1")) == before and _applied(db_path) == "hybrid"


def test_an_unknown_running_mode_is_ignored(client, db_path):
    _setup(client, db_path)
    _poll(client, "banana")
    assert _applied(db_path) is None


# ------------------------------------------------------------------ the VMS side

def test_the_vms_reports_the_mode_it_started_in_not_the_persisted_one(tmp_path, monkeypatch):
    """Governed flags are read once at start, so the process's start mode --
    not the file it just persisted -- is what its workers follow."""
    import product_mode as pm
    monkeypatch.delenv("ANYAICAM_PRODUCT_MODE", raising=False)
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "product_mode.json")
    monkeypatch.setattr(pm, "RUNNING_MODE", "local")
    assert pm.persist_mode("hybrid") is True
    assert pm.running_mode() == "local" and pm.current_mode() == "hybrid"


def test_after_a_restart_the_workers_follow_the_new_mode_with_unchanged_configuration(tmp_path, monkeypatch):
    """No env/Compose change: the restarted process reads the persisted mode,
    reports it, and every governed flag follows it."""
    import product_mode as pm
    state = tmp_path / "product_mode.json"
    monkeypatch.setenv("ANYAICAM_PRODUCT_MODE_STATE_FILE", str(state))
    for flag in list(pm.FLAG_REGISTRY):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.delenv("ANYAICAM_PRODUCT_MODE", raising=False)
    monkeypatch.delenv("ANYAICAM_PRODUCT_MODE_BOOTSTRAP", raising=False)
    state.write_text(json.dumps({"mode": "local"}))
    reloaded = importlib.reload(pm)
    monkeypatch.setattr(reloaded, "STATE_FILE", state)
    local_flags = {flag: reloaded.resolve_cloud_flag(flag) for flag in reloaded.FLAG_REGISTRY}
    assert reloaded.running_mode() == "local" and not any(local_flags.values())
    reloaded.persist_mode("hybrid")
    restarted = importlib.reload(pm)                 # the restart
    monkeypatch.setattr(restarted, "STATE_FILE", state)
    hybrid_flags = {flag: restarted.resolve_cloud_flag(flag) for flag in restarted.FLAG_REGISTRY}
    assert restarted.running_mode() == "hybrid" and all(hybrid_flags.values())
    importlib.reload(pm)


def test_the_edge_sync_sends_its_running_mode(monkeypatch):
    import edge_camera_sync
    seen = []
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr("appliance_activation.load_persisted_identity",
                        lambda: {"appliance_id": "a", "cloud_id": "c", "credential": "k", "customer_id": "x", "site_id": "s"})
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: seen.append(path) or None)
    monkeypatch.setattr(edge_camera_sync.product_mode, "RUNNING_MODE", "hybrid")
    edge_camera_sync.sync_provisioned_cameras()
    assert seen and seen[0] == "/api/appliance/configuration?running_mode=hybrid"
