"""Windows 1.3.2 (Blue): offline premium analytics on top of Red's SHARED signed-entitlement contract.

Owner design (2026-10-19): premium analytics get a separate offline authorization window of 168 h; Talk Down and
Voice Call keep their existing 24 h snapshot expiry; the global entitlement TTL is not changed. The shared contract is
Red's ae23d024 (RED-ANALYTICS-OFFLINE-168H-DESIGN-20261019): a separately signed ``anyaicam.analytics_entitlements``
document, stored with an HMAC (keyed from ANYAICAM_APP_SECRETS) and a clock high-water in
``appliance_entitlements.ANALYTICS_STATE_FILE`` -- on Windows %ProgramData%\\AnyAiCam\\config. Blue does NOT implement a
Windows-only rule. This file uses Red's real fixtures (real cloud configuration route, real appliance sync, real
signatures) and pins the owner's offline cases end to end: restart, corrupt/missing/tampered state (denies premium
analytics, never the camera), renewal keeping the customer's OFF, the 24 h / 167 h 59 m / 168 h boundaries, Talk Down
and Voice Call at 24 h, and replay of an older grant (by file or by sync) after a revocation.

The four former strict-xfails (pending the 168 h document and replay protection) now pass on ae23d024 and are plain
tests.
"""
import json
from datetime import datetime, timedelta

import pytest

import appliance_entitlements
from test_premium_analytics_default_off import (  # noqa: F401 -- fixtures and helpers (Red's shared suite)
    _cached, _cloud_lpr_purchase, _doc, _edge_camera_flag, _edge_lpr, _exec, _fresh_grant_cache, _resign, _sync,
    appliance, appliance_cloud, cloud_client, cloud_db, edge_camera_sync, edge_db, edge_env,
    isolated_product_mode_state, prices)


def _licensed_and_on(cloud_db, edge_db):
    """LPR bought, assigned and switched ON; one sync. Returns (analytics document, its issued_at)."""
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    document = _doc()
    assert document is not None and document["camera_analytics"] == {"cam-1": ["lpr"]}
    return document, datetime.fromisoformat(document["issued_at"])


def _lpr_granted_at(when):
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    return "lpr" in appliance_entitlements.camera_analytic_grants(now=when).get("cam-1", frozenset())


# ---------------------------------------------------------------- restart and stored-state integrity
def test_a_verified_grant_survives_a_service_restart(appliance, cloud_db, edge_db, monkeypatch):
    _licensed_and_on(cloud_db, edge_db)
    assert _edge_lpr(monkeypatch) is True
    stored = appliance_entitlements.ANALYTICS_STATE_FILE.read_bytes()
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})   # new process
    assert _edge_lpr(monkeypatch) is True                      # re-verified from the persisted, signed, MAC'd document
    assert json.loads(appliance_entitlements.ANALYTICS_STATE_FILE.read_bytes())["grant"] == json.loads(stored)["grant"]


@pytest.mark.parametrize("damage", ["garbage", "truncated", "deleted", "mac_tampered", "unsigned_copy", "secret_missing"])
def test_corrupt_or_missing_state_denies_premium_but_never_the_camera(appliance, cloud_db, edge_db, monkeypatch, damage):
    import recording_uploader
    _licensed_and_on(cloud_db, edge_db)
    path = appliance_entitlements.ANALYTICS_STATE_FILE
    state = json.loads(path.read_text(encoding="utf-8"))
    if damage == "garbage":
        path.write_bytes(b"\x00\xffnot json")
    elif damage == "truncated":
        path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    elif damage == "deleted":
        path.unlink()
    elif damage == "mac_tampered":                             # an unprivileged edit that widens the grant
        state["grant"]["camera_analytics"] = {"cam-1": ["lpr", "facial_recognition"]}
        path.write_text(json.dumps(state), encoding="utf-8")
    elif damage == "unsigned_copy":                            # re-MAC'd with the real secret, but the signature removed
        grant = {k: v for k, v in state["grant"].items() if k != "signature"}
        appliance_entitlements._write_analytics_state(grant, datetime.fromisoformat(state["high_water"]))
    else:                                                      # the service started without vms.env's secret
        monkeypatch.delenv("ANYAICAM_APP_SECRETS")
    assert _edge_lpr(monkeypatch) is False
    identity = recording_uploader._camera_identity(1)
    assert identity is not None and identity.get("camera_id") == "cam-1"      # recording / live view / basic motion


def test_renewal_keeps_a_customer_off_choice_off(appliance, cloud_db, edge_db, monkeypatch):
    _licensed_and_on(cloud_db, edge_db)
    _exec(cloud_db, "UPDATE camera_analytics_operations SET enabled=0 WHERE camera_id='cam-1' AND analytic_key='lpr'")
    _sync(edge_db)
    assert _edge_camera_flag(edge_db) is False
    _sync(edge_db)                                             # a renewal (fresh document, still licensed)
    assert _doc()["camera_analytics"] == {"cam-1": ["lpr"]}
    assert _edge_camera_flag(edge_db) is False                 # licensed, but the customer's OFF is kept


# ---------------------------------------------------------------- boundaries (owner: 24 h, 167 h 59 m, 168 h, beyond)
@pytest.mark.parametrize("after", [timedelta(hours=168), timedelta(hours=168, minutes=1), timedelta(days=30)])
def test_premium_analytics_stop_at_168_hours_and_later(appliance, cloud_db, edge_db, after):
    _, issued = _licensed_and_on(cloud_db, edge_db)
    assert _lpr_granted_at(issued + after) is False


@pytest.mark.parametrize("after", [timedelta(hours=24), timedelta(hours=24, minutes=1), timedelta(hours=167, minutes=59)])
def test_premium_analytics_stay_authorized_offline_within_168_hours(appliance, cloud_db, edge_db, after):
    _, issued = _licensed_and_on(cloud_db, edge_db)
    assert _lpr_granted_at(issued + after) is True


def test_talk_down_and_voice_call_keep_their_24_hour_snapshot_expiry(appliance, cloud_db, edge_db):
    _licensed_and_on(cloud_db, edge_db)
    snapshot = _cached()["snapshot"]
    issued = datetime.fromisoformat(snapshot["issued_at"])
    assert "camera_analytics" not in snapshot                  # premium analytics are NOT carried by the 24 h snapshot
    keys, identity = appliance_entitlements._trusted_keys(), appliance_entitlements._local_identity()
    assert appliance_entitlements.verify(snapshot, keys, identity=identity, now=issued + timedelta(hours=23, minutes=59)) is None
    assert appliance_entitlements.verify(snapshot, keys, identity=identity, now=issued + timedelta(hours=24, minutes=1)) is not None


def test_after_24_hours_paid_features_stop_while_premium_analytics_continue(appliance, cloud_db, edge_db):
    _, issued = _licensed_and_on(cloud_db, edge_db)
    snapshot = _cached()["snapshot"]
    later = issued + timedelta(hours=25)
    keys, identity = appliance_entitlements._trusted_keys(), appliance_entitlements._local_identity()
    assert appliance_entitlements.verify(snapshot, keys, identity=identity, now=later) is not None   # Talk Down / Voice Call
    assert _lpr_granted_at(later) is True                                                       # premium analytics


def test_a_clock_set_back_on_the_pc_denies_premium_analytics(appliance, cloud_db, edge_db):
    """Windows: any administrator can change the system clock; turning it back must not extend the window."""
    _, issued = _licensed_and_on(cloud_db, edge_db)
    assert _lpr_granted_at(issued + timedelta(hours=100)) is True       # high-water advances to +100 h
    assert _lpr_granted_at(issued + timedelta(hours=1)) is False        # clock rolled back 99 h


# ---------------------------------------------------------------- replay (owner: reject where feasible)
def _revoke_lpr(cloud_db, edge_db, monkeypatch):
    older, _ = _licensed_and_on(cloud_db, edge_db)
    _exec(cloud_db, "UPDATE analytics_subscriptions SET status='cancelled' WHERE id='sub-lpr'")
    _sync(edge_db)                                             # the newer document revokes LPR
    assert "lpr" not in _doc()["camera_analytics"].get("cam-1", [])
    assert _edge_lpr(monkeypatch) is False
    return older


def test_an_older_grant_file_put_back_after_a_revocation_is_refused(appliance, cloud_db, edge_db, monkeypatch):
    older = _revoke_lpr(cloud_db, edge_db, monkeypatch)
    # a copy of the older signed document dropped into the state file without the appliance's MAC
    appliance_entitlements.ANALYTICS_STATE_FILE.write_text(
        json.dumps({"grant": older, "high_water": older["issued_at"], "mac": "0" * 64}), encoding="utf-8")
    assert _edge_lpr(monkeypatch) is False


def test_an_older_grant_delivered_by_a_later_sync_clears_instead_of_restoring(appliance, cloud_db, edge_db, monkeypatch):
    older = _revoke_lpr(cloud_db, edge_db, monkeypatch)
    assert appliance_entitlements.store_analytics_grant(older) == "cleared:older_than_stored"   # Green F1
    assert _doc() is None
    assert _edge_lpr(monkeypatch) is False
