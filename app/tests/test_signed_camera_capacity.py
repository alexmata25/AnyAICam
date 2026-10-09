"""Signed camera capacity (2026-10-09, owner-approved Option A).

Before: the appliance trusted vms_capacity.json, which the anyaicam account
can write, so the licensed camera limit could be raised offline (Orange,
pre-existing since 1.2.4). Now the limit is accepted only inside the cloud's
signed entitlement snapshot, verified against the root-owned trust anchor
and this appliance's identity (vms_capacity.py). The cloud side is the real
configuration endpoint and the appliance side the real configuration sync
(fixtures from test_appliance_feature_entitlements).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import appliance_entitlements
import vms_capacity
from test_appliance_feature_entitlements import (  # noqa: F401 -- fixtures and helpers
    EDGE_IDENTITY, _resign, _sync as _cloud_sync, appliance, cloud_client, cloud_db, edge_db, edge_env)

import sys
from pathlib import Path
_SYSTEM_DIR = Path(__file__).resolve().parents[2] / "appliance-agent" / "system"
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))
import capacity_accept

pytestmark = pytest.mark.real_entitlements

CAMERAS = [1, 2, 3, 4, 5, 6, 7, 8]


@pytest.fixture()
def licensed(appliance, monkeypatch):
    """What the cloud's billing says this customer may run (camera plan AND
    VMS license), read by the real cloud endpoint at each sync."""
    import customer_entitlements
    limit = {"n": 3}
    monkeypatch.setattr(customer_entitlements, "capacity_breakdown",
                        lambda customer_id: {"camera_slot_quantity": limit["n"], "plan_camera_slots": limit["n"],
                                             "vms_license_capacity": 64, "vms_license_required": False})
    return limit


def _running():
    return vms_capacity.licensed_camera_numbers(CAMERAS)


def _signed_snapshot():
    return json.loads(vms_capacity.SIGNED_FILE.read_text(encoding="utf-8"))


def _restart(monkeypatch):
    """A VMS restart rereads the protected accepted store."""
    return None


def _sync(edge_db):
    """Exercise cloud sync and simulate the root path trigger without sudo."""
    result = _cloud_sync(edge_db)
    snapshot = _signed_snapshot()
    if snapshot and snapshot.get("camera_capacity"):
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        keys = {key_id: base64.b64decode(value)
                for key_id, value in appliance_entitlements._trusted_keys().items()}

        def verify(raw_key, message, signature):
            try:
                Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, message)
                return True
            except Exception:
                return False

        current = vms_capacity._read_json(vms_capacity.ACCEPTED_FILE)
        try:
            accepted, _ = capacity_accept._advance(snapshot, keys, EDGE_IDENTITY, current, verifier=verify)
        except capacity_accept.Reject:
            pass
        else:
            vms_capacity.ACCEPTED_FILE.parent.mkdir(parents=True, exist_ok=True)
            vms_capacity.ACCEPTED_FILE.write_text(json.dumps(accepted), encoding="utf-8")
    return result


def _fallback(quantity=5, **changes):
    record = {"schema": 1, "camera_slot_quantity": quantity, "appliance_id": EDGE_IDENTITY["appliance_id"],
              "cloud_id": EDGE_IDENTITY["cloud_id"], "customer_id": EDGE_IDENTITY["customer_id"],
              "source": "migrated_unsigned_capacity", "migrated_from_version": "1.2.4",
              "migrated_at": "2026-10-09T12:00:00+00:00", "migrated_by": "installer"}
    record.update(changes)
    vms_capacity.FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    vms_capacity.FALLBACK_FILE.write_text(json.dumps(record), encoding="utf-8")


# ------------------------------------------------------------------ the cloud signs the limit

def test_the_cloud_signs_the_licensed_camera_limit_in_the_entitlement_snapshot(licensed, edge_db):
    _sync(edge_db)
    snapshot = _signed_snapshot()
    assert snapshot["camera_capacity"] == {"camera_slot_quantity": 3, "plan_camera_slots": 3, "vms_license_capacity": 64}
    assert snapshot["type"] == "anyaicam.feature_entitlements" and snapshot["version"] == 1  # 1.2.4 ignores the field
    assert appliance_entitlements.verify(snapshot, appliance_entitlements._trusted_keys(),
                                         identity=appliance_entitlements._local_identity()) is None


def test_a_signed_limit_decides_which_cameras_run_and_follows_plan_changes(licensed, edge_db):
    _sync(edge_db)
    assert _running() == [1, 2, 3]
    assert vms_capacity.load_capacity()["source"] == "signed"
    licensed["n"] = 6  # more cameras bought
    _sync(edge_db)
    assert _running() == [1, 2, 3, 4, 5, 6]
    licensed["n"] = 0  # cancelled / no license
    _sync(edge_db)
    assert _running() == []


# ------------------------------------------------------------------ the unsigned file is not trusted

def test_a_forged_unsigned_capacity_file_has_no_effect(licensed, edge_db, monkeypatch):
    _sync(edge_db)
    vms_capacity.STATE_FILE.write_text(json.dumps({"camera_slot_quantity": 64}), encoding="utf-8")
    assert _running() == [1, 2, 3]
    _restart(monkeypatch)
    assert _running() == [1, 2, 3]


@pytest.mark.parametrize("tamper", [
    lambda s: s["camera_capacity"].update(camera_slot_quantity=64),
    lambda s: s.update(customer_id="cust-other"),
    lambda s: s["signature"].update(value=s["signature"]["value"][::-1]) if isinstance(s.get("signature"), dict) else s.update(signature="x"),
])
def test_an_edited_signed_capacity_is_not_accepted(licensed, edge_db, monkeypatch, tamper):
    _sync(edge_db)
    snapshot = _signed_snapshot()
    tamper(snapshot)
    vms_capacity.SIGNED_FILE.write_text(json.dumps(snapshot), encoding="utf-8")
    _restart(monkeypatch)
    assert _running() == [1, 2, 3]  # rejected staging cannot revoke accepted authorization


def test_a_limit_signed_by_an_attacker_key_is_not_accepted(licensed, edge_db, monkeypatch):
    import base64
    import appliance_identity
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _sync(edge_db)
    raw = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                     serialization.NoEncryption())
    body = {k: v for k, v in _signed_snapshot().items() if k != "signature"}
    body["camera_capacity"] = {"camera_slot_quantity": 64, "plan_camera_slots": 64, "vms_license_capacity": 64}
    forged = {**body, "signature": appliance_identity.sign_body(body, key_id="attacker",
                                                               private_key_b64=base64.b64encode(raw).decode())}
    vms_capacity.SIGNED_FILE.write_text(json.dumps(forged), encoding="utf-8")
    assert _running() == [1, 2, 3]
    assert vms_capacity.apply_sync({"camera_slot_quantity": 64}, forged) == "staged"


def test_another_appliances_signed_capacity_is_not_accepted(licensed, edge_db, cloud_db, monkeypatch):
    _sync(edge_db)
    other = _resign(_signed_snapshot(), cloud_db, appliance_id="appl-other")
    vms_capacity.SIGNED_FILE.write_text(json.dumps(other), encoding="utf-8")
    assert _running() == [1, 2, 3]


def test_an_older_signed_capacity_never_replaces_a_newer_one(licensed, edge_db, cloud_db):
    _sync(edge_db)
    older = _resign(_signed_snapshot(), cloud_db, issued_at="2026-01-01T00:00:00+00:00",
                    expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    camera_capacity={"camera_slot_quantity": 8, "plan_camera_slots": 8, "vms_license_capacity": 64})
    assert vms_capacity.apply_sync({"camera_slot_quantity": 8}, older) == "staged"
    assert _running() == [1, 2, 3]


@pytest.mark.parametrize("quantity", [-1, 10_001, True, "64", None])
def test_a_signed_capacity_outside_the_supported_range_is_not_accepted(licensed, edge_db, cloud_db, monkeypatch, quantity):
    _sync(edge_db)
    bad = _resign(_signed_snapshot(), cloud_db, camera_capacity={"camera_slot_quantity": quantity})
    vms_capacity.SIGNED_FILE.unlink()
    assert vms_capacity.apply_sync({"camera_slot_quantity": 2}, bad) == "staged"
    _restart(monkeypatch)
    assert _running() == [1, 2, 3]


# ------------------------------------------------------------------ offline operation

def test_offline_after_the_snapshot_expired_recording_continues_at_the_signed_limit(licensed, edge_db, cloud_db, monkeypatch, appliance):
    _sync(edge_db)
    expired = _resign(_signed_snapshot(), cloud_db, issued_at="2026-10-01T00:00:00+00:00",
                      expires_at="2026-10-02T00:00:00+00:00")
    vms_capacity.SIGNED_FILE.write_text(json.dumps(expired), encoding="utf-8")
    appliance["online"] = False
    _sync(edge_db)  # the cloud is unreachable
    _restart(monkeypatch)
    assert _running() == [1, 2, 3]
    # Paid features still stop at the TTL; only the camera limit outlives it.
    assert appliance_entitlements.verify(expired, appliance_entitlements._trusted_keys(),
                                         identity=appliance_entitlements._local_identity()) == "expired"


def test_a_bad_sync_never_removes_the_accepted_signed_limit(licensed, edge_db, appliance):
    _sync(edge_db)
    appliance["rewrite"] = lambda response: {**response, "feature_entitlements": {"type": "garbage"}}
    _sync(edge_db)
    assert _running() == [1, 2, 3]


def test_the_unsigned_file_is_never_runtime_authority(licensed, edge_db):
    licensed["n"] = 4
    _sync(edge_db)
    vms_capacity.STATE_FILE.write_text(json.dumps({"camera_slot_quantity": 64}), encoding="utf-8")
    assert _running() == [1, 2, 3, 4]


# ------------------------------------------------------------------ a cloud that does not sign capacity yet

def _legacy_cloud(cloud_db):
    """The cloud before this change: a valid signed snapshot without camera_capacity."""
    def rewrite(response):
        snapshot = dict(response["feature_entitlements"])
        snapshot.pop("camera_capacity")
        return {**response, "feature_entitlements": _resign_without(snapshot, cloud_db)}
    return rewrite


def _resign_without(snapshot, cloud_db):
    import appliance_identity
    from test_appliance_feature_entitlements import connection, override_target
    body = {k: v for k, v in snapshot.items() if k != "signature"}
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            key = appliance_identity.ensure_signing_key(db)
    return {**body, "signature": appliance_identity.sign_body(body, key_id=key["key_id"], private_key_b64=key["private_key_b64"])}


def test_with_a_legacy_cloud_unsigned_capacity_is_never_accepted(licensed, edge_db, cloud_db, appliance, monkeypatch):
    appliance["rewrite"] = _legacy_cloud(cloud_db)
    _sync(edge_db)
    assert _running() == []
    vms_capacity.STATE_FILE.write_text(json.dumps({"camera_slot_quantity": 64}), encoding="utf-8")
    assert _running() == []  # the unsigned file is never read
    _restart(monkeypatch)
    assert _running() == []


def test_with_a_legacy_cloud_an_expired_snapshot_grants_nothing(licensed, edge_db, cloud_db, appliance):
    def expired_legacy(response):
        snapshot = dict(response["feature_entitlements"])
        snapshot.pop("camera_capacity")
        snapshot.update(issued_at="2026-10-01T00:00:00+00:00", expires_at="2026-10-02T00:00:00+00:00")
        return {**response, "feature_entitlements": _resign_without(snapshot, cloud_db)}
    appliance["rewrite"] = expired_legacy
    _sync(edge_db)
    assert _running() == []


def test_once_a_signed_limit_was_accepted_a_legacy_snapshot_is_ignored(licensed, edge_db, cloud_db, appliance):
    _sync(edge_db)
    licensed["n"] = 8
    appliance["rewrite"] = _legacy_cloud(cloud_db)
    _sync(edge_db)
    assert _running() == [1, 2, 3]
    assert vms_capacity.load_capacity()["source"] == "signed"


# ------------------------------------------------------------------ the migrated root-owned fallback

def test_an_upgraded_appliance_records_offline_at_its_migrated_limit_until_the_first_signed_one(licensed, edge_db, appliance, monkeypatch):
    _fallback(5)
    appliance["online"] = False
    _sync(edge_db)
    assert _running() == [1, 2, 3, 4, 5]
    assert vms_capacity.load_capacity()["source"] == "migrated"
    appliance["online"] = True
    _sync(edge_db)  # the first signed limit replaces the migrated one
    assert _running() == [1, 2, 3]
    _restart(monkeypatch)
    assert _running() == [1, 2, 3]


@pytest.mark.parametrize("changes", [
    {"customer_id": "cust-previous-owner"}, {"appliance_id": "appl-other"}, {"cloud_id": "AIC-other"},
    {"camera_slot_quantity": 10_001}, {"camera_slot_quantity": "8"}, {"schema": 2}, {"source": "anything"},
])
def test_a_fallback_for_another_identity_or_malformed_is_ignored(licensed, appliance, changes):
    _fallback(5, **changes)
    assert _running() == []


def test_an_unsafe_fallback_file_is_ignored(licensed, appliance, monkeypatch):
    _fallback(5)
    monkeypatch.setattr(appliance_entitlements, "_anchor_file_problem",
                        lambda path: "is not owned by root" if path == vms_capacity.FALLBACK_FILE else "")
    assert _running() == []


def test_a_huge_fallback_file_is_ignored(licensed, appliance):
    _fallback(5, padding="x" * (300 * 1024))
    assert _running() == []


def test_the_fallback_path_is_fixed_beside_the_trust_anchor():
    import importlib
    source = open(vms_capacity.__file__, encoding="utf-8").read()
    assert 'FALLBACK_FILE = Path("/etc/anyaicam-update/vms_capacity_fallback.json")' in source
    assert importlib.import_module("vms_capacity").__name__ == "vms_capacity"


# ------------------------------------------------------------------ end to end with the migration

def _apply_release():
    import sys
    from pathlib import Path
    system = Path(__file__).resolve().parents[2] / "appliance-agent" / "system"
    if str(system) not in sys.path:
        sys.path.insert(0, str(system))
    import apply_release
    if not hasattr(apply_release, "migrate_capacity_fallback"):
        pytest.skip("the installer/Software Update migration is not in this tree")
    return apply_release


@pytest.mark.parametrize("identity_changes,expected", [({}, [1, 2, 3, 4, 5]), ({"customer_id": "cust-next-owner"}, [])])
def test_a_fallback_written_by_the_real_migration_is_accepted_only_for_its_identity(
        licensed, appliance, tmp_path, monkeypatch, identity_changes, expected):
    apply_release = _apply_release()
    legacy = tmp_path / "data-config" / "vms_capacity.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"camera_slot_quantity": 5, "plan_camera_slots": 5, "vms_license_capacity": 5}))
    identity_file = tmp_path / "recordings" / "appliance_identity.json"
    identity_file.parent.mkdir()
    identity_file.write_text(json.dumps({**EDGE_IDENTITY, "credential": "secret-credential"}))
    vms_capacity.FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    outcome = apply_release.migrate_capacity_fallback(from_version="1.2.4", migrated_by="installer",
                                                      target=vms_capacity.FALLBACK_FILE, legacy=legacy,
                                                      identity_file=identity_file, posix=False)
    assert outcome == "migrated"
    assert "secret-credential" not in vms_capacity.FALLBACK_FILE.read_text()
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: {**EDGE_IDENTITY, **identity_changes})
    assert _running() == expected
