"""Windows 1.3.2 (Blue): offline premium analytics on top of Red's SHARED signed-entitlement contract.

Owner design (2026-10-19): premium analytics get a separate offline authorization window of 168 h; Talk Down and
Voice Call keep their existing 24 h snapshot expiry; the global entitlement TTL is not changed. The shared contract is
Red's (claude/red-127-analytics-default-off, 96d57666 + the 168 h follow-up). Blue does NOT implement a Windows-only
rule. This file uses Red's real fixtures (real cloud configuration route, real appliance sync, real signatures):

* passes now -- behaviour that must hold today and after the 168 h window lands: a verified grant survives a
  service restart (the cached snapshot lives in the protected %ProgramData%\\AnyAiCam\\config on Windows); corrupt or
  missing state denies premium analytics but never the camera itself (recording, live view, basic motion); renewal
  keeps the customer's OFF choice; 168 h and later is always denied; Talk Down / Voice Call keep the 24 h boundary.
* strict xfail -- what the owner's design requires but the shared contract does not provide yet (the 168 h analytics
  window; replay protection). When Red's contract lands these XPASS and fail the run, so the markers must be removed
  in the same integration -- they cannot silently stay pending.
"""
import json
from datetime import datetime, timedelta

import pytest

import appliance_entitlements
from test_premium_analytics_default_off import (  # noqa: F401 -- fixtures and helpers (Red's shared suite)
    _cached, _cloud_lpr_purchase, _edge_camera_flag, _edge_lpr, _exec, _fresh_grant_cache, _resign, _sync,
    appliance, appliance_cloud, cloud_client, cloud_db, edge_camera_sync, edge_db, edge_env,
    isolated_product_mode_state, prices)

PENDING_168H = pytest.mark.xfail(strict=True, reason="pending Red's separate signed anyaicam.analytics_entitlements document "
                                                      "(RED-ANALYTICS-OFFLINE-168H-DESIGN-20261019); adapt + remove when merged")
PENDING_REPLAY = pytest.mark.xfail(strict=True, reason="pending Red's design (an older analytics document clears the "
                                                        "grant; MAC'd high-water); adapt + remove when merged")


def _licensed_and_on(cloud_db, edge_db):
    _cloud_lpr_purchase(cloud_db, assign=True, enable=True)
    _sync(edge_db)
    snapshot = _cached()["snapshot"]
    return snapshot, datetime.fromisoformat(snapshot["issued_at"])


def _lpr_granted_at(when):
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    return "lpr" in appliance_entitlements.camera_analytic_grants(now=when).get("cam-1", frozenset())


# ---------------------------------------------------------------- restart and stored-state integrity (passes now)
def test_a_verified_grant_survives_a_service_restart(appliance, cloud_db, edge_db, monkeypatch):
    _licensed_and_on(cloud_db, edge_db)
    assert _edge_lpr(monkeypatch) is True
    stored = appliance_entitlements.STATE_FILE.read_bytes()
    appliance_entitlements._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    assert appliance_entitlements.STATE_FILE.read_bytes() == stored
    assert _edge_lpr(monkeypatch) is True                      # re-verified from the persisted, signed snapshot


@pytest.mark.parametrize("damage", ["garbage", "truncated", "deleted", "unsigned_copy"])
def test_corrupt_or_missing_state_denies_premium_but_never_the_camera(appliance, cloud_db, edge_db, monkeypatch, damage):
    import recording_uploader
    snapshot, _ = _licensed_and_on(cloud_db, edge_db)
    path = appliance_entitlements.STATE_FILE
    if damage == "garbage":
        path.write_bytes(b"\x00\xffnot json")
    elif damage == "truncated":
        path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    elif damage == "deleted":
        path.unlink()
    else:
        path.write_text(json.dumps({"snapshot": {k: v for k, v in snapshot.items() if k != "signature"}}), encoding="utf-8")
    assert _edge_lpr(monkeypatch) is False
    identity = recording_uploader._camera_identity(1)
    assert identity is not None and identity.get("camera_id") == "cam-1"      # recording / live view / basic motion


def test_renewal_keeps_a_customer_off_choice_off(appliance, cloud_db, edge_db, monkeypatch):
    _licensed_and_on(cloud_db, edge_db)
    _exec(cloud_db, "UPDATE camera_analytics_operations SET enabled=0 WHERE camera_id='cam-1' AND analytic_key='lpr'")
    _sync(edge_db)
    assert _edge_camera_flag(edge_db) is False
    _sync(edge_db)                                             # a renewal (fresh snapshot, still licensed)
    assert _cached()["snapshot"]["camera_analytics"] == {"cam-1": ["lpr"]}
    assert _edge_camera_flag(edge_db) is False                 # licensed, but the customer's OFF is kept


# ---------------------------------------------------------------- boundaries (owner: 24 h, 167 h 59 m, 168 h, beyond)
@pytest.mark.parametrize("after", [timedelta(hours=168), timedelta(hours=168, minutes=1), timedelta(days=30)])
def test_premium_analytics_stop_at_168_hours_and_later(appliance, cloud_db, edge_db, after):
    _, issued = _licensed_and_on(cloud_db, edge_db)
    assert _lpr_granted_at(issued + after) is False


@PENDING_168H
@pytest.mark.parametrize("after", [timedelta(hours=24), timedelta(hours=167, minutes=59)])
def test_premium_analytics_stay_authorized_offline_within_168_hours(appliance, cloud_db, edge_db, after):
    _, issued = _licensed_and_on(cloud_db, edge_db)
    assert _lpr_granted_at(issued + after) is True


def test_talk_down_and_voice_call_keep_their_24_hour_snapshot_expiry(appliance, cloud_db, edge_db):
    snapshot, issued = _licensed_and_on(cloud_db, edge_db)
    keys, identity = appliance_entitlements._trusted_keys(), appliance_entitlements._local_identity()
    assert appliance_entitlements.verify(snapshot, keys, identity=identity, now=issued + timedelta(hours=23, minutes=59)) is None
    assert appliance_entitlements.verify(snapshot, keys, identity=identity, now=issued + timedelta(hours=24, minutes=1)) is not None


@PENDING_168H
def test_after_24_hours_paid_features_stop_while_premium_analytics_continue(appliance, cloud_db, edge_db):
    snapshot, issued = _licensed_and_on(cloud_db, edge_db)
    later = issued + timedelta(hours=25)
    keys, identity = appliance_entitlements._trusted_keys(), appliance_entitlements._local_identity()
    assert appliance_entitlements.verify(snapshot, keys, identity=identity, now=later) is not None   # Talk Down / Voice Call
    assert _lpr_granted_at(later) is True                                                       # premium analytics


# ---------------------------------------------------------------- replay (owner: reject where feasible)
@PENDING_REPLAY
def test_an_older_snapshot_put_back_after_a_revocation_is_refused(appliance, cloud_db, edge_db, monkeypatch):
    older, _ = _licensed_and_on(cloud_db, edge_db)
    _exec(cloud_db, "UPDATE analytics_subscriptions SET status='cancelled' WHERE id='sub-lpr'")
    _sync(edge_db)                                             # the newer snapshot revokes LPR
    assert _edge_lpr(monkeypatch) is False
    appliance_entitlements.STATE_FILE.write_text(json.dumps({"snapshot": older}), encoding="utf-8")
    assert _edge_lpr(monkeypatch) is False                     # the older, still-unexpired grant must not come back
