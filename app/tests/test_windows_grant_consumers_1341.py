"""LIC-W1 correction: what Setup's trust helper writes is what every Windows reader of a cached grant obeys.

Each test installs through appliance-agent/system/trust_install_windows.py, then asks the readers the VMS really uses
(vms_capacity, appliance_entitlements), with no reader stubbed. Every registry here is SYNTHETIC."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import appliance_entitlements as ae
import local_customer_access
import vms_capacity
from test_entitlement_trust_1271 import (  # noqa: F401
    IDENTITY, accept, analytics_doc, capacity_snapshot, edge, feature_snapshot, record, registry)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "appliance-agent" / "system"))
import capacity_accept
import trust_install_windows as helper

pytestmark = pytest.mark.real_entitlements
REAL_ANCHOR_CHECK = ae._anchor_file_problem


def store(edge):
    return {"root": edge.dir.parent, "trust": ae.TRUST_ANCHOR_FILE, "accepted": vms_capacity.ACCEPTED_FILE,
            "registry": ae.TRUST_REGISTRY_FILE, "trust_state": ae.TRUST_STATE_FILE}


def restart():
    """The 10-second analytics cache is in-process, and Setup stops the VMS before it changes a trust file."""
    ae._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})


def setup_runs(edge, document):
    data = capacity_accept.canonical_trust_registry(document)
    keyset = helper._release_checks().entitlement_keyset_projection(data)
    outcome = helper._install_from_paths(store(edge), data, hashlib.sha256(data).hexdigest(), keyset,
                                         acl_problem=lambda *_a, **_k: "")
    ae.TRUST_ANCHOR_FILE.write_bytes(keyset)
    restart()
    return outcome


def holds_every_grant(edge):
    setup_runs(edge, registry(edge, [(edge.k1, "active", {})], 1))
    accept(edge, capacity_snapshot(edge, edge.k1, 4))
    assert ae.store_snapshot(feature_snapshot(edge, edge.k1)) == "stored"
    assert ae.store_analytics_grant(analytics_doc(edge, edge.k1), now=edge.now) == "stored"
    assert_every_grant(edge, True)


def assert_every_grant(edge, held):
    assert (vms_capacity.load_capacity() == record(4)) is held
    assert vms_capacity.licensed_camera_numbers([1, 2, 3, 4, 5]) == ([1, 2, 3, 4] if held else [])
    assert ae.feature_allowed("talk_down") is held
    assert ae.camera_analytic_allowed("cam-1", "lpr") is held
    assert (ae.camera_analytic_grants(now=edge.now) == {"cam-1": frozenset({"lpr"})}) is held
    assert ae.analytics_grant_held(now=edge.now) is held


def retiring(edge):
    return registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                           (edge.k2, "active", {"changed_generation": 2})], 2)


def revoking(edge):
    return registry(edge, [(edge.k1, "revoked", {"changed_generation": 3}),
                           (edge.k2, "active", {"changed_generation": 2})], 3)


def test_a_retired_signer_keeps_every_cached_grant_after_setup(edge):
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    assert_every_grant(edge, True)
    assert local_customer_access._trusted_keys().keys() == {edge.k2.key_id}  # new documents: active keys only


def test_a_revocation_stops_every_cached_grant_after_setup(edge):
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    setup_runs(edge, revoking(edge))
    assert_every_grant(edge, False)
    assert vms_capacity.capacity_status()["reason"] == "revoked"


def test_a_flat_keyset_restored_by_an_older_setup_never_reenables_a_revoked_key(edge):
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    setup_runs(edge, revoking(edge))
    flat = {edge.k1.key_id: edge.k1.public_b64, edge.k2.key_id: edge.k2.public_b64}
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": flat}), encoding="utf-8")
    assert_every_grant(edge, False)


@pytest.mark.parametrize("removed", [("registry",), ("registry", "trust_state")])
def test_deleting_the_trust_files_never_falls_back_to_the_flat_keyset(edge, removed):
    holds_every_grant(edge)
    for name in removed:
        store(edge)[name].unlink()
    restart()
    assert_every_grant(edge, False)


def test_an_accepted_file_that_fails_trust_never_falls_through_to_the_fallback(edge):
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    setup_runs(edge, revoking(edge))
    vms_capacity.FALLBACK_FILE.write_text(json.dumps({
        "schema": 1, "source": "migrated_unsigned_capacity", "camera_slot_quantity": 100, **IDENTITY}), encoding="utf-8")
    assert vms_capacity.load_capacity() is None


def test_a_signer_only_the_feature_and_analytics_grants_used_fails_closed(edge):
    """Setup checks the capacity grant's signer only. A first install that omits a signer only these grants used
    installs, and they stop, which is the safe direction: a new sync restores them, and they would expire anyway."""
    flat = {edge.k1.key_id: edge.k1.public_b64, edge.k2.key_id: edge.k2.public_b64}
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": flat}), encoding="utf-8")
    accept(edge, capacity_snapshot(edge, edge.k1, 4))
    assert ae.store_snapshot(feature_snapshot(edge, edge.k2)) == "stored"
    assert ae.store_analytics_grant(analytics_doc(edge, edge.k2), now=edge.now) == "stored"
    assert ae.feature_allowed("talk_down") is True
    setup_runs(edge, registry(edge, [(edge.k1, "active", {})], 1))
    assert vms_capacity.load_capacity() == record(4)
    assert ae.feature_allowed("talk_down") is False
    assert ae.camera_analytic_grants(now=edge.now) == {}


@pytest.mark.skipif(os.name != "nt", reason="needs Windows")
@pytest.mark.parametrize("name, reason", [("registry", "registry_unsafe"), ("trust_state", "trust_state_unsafe")])
def test_a_hard_linked_trust_file_fails_every_reader_closed(edge, monkeypatch, tmp_path, name, reason):
    holds_every_grant(edge)
    import platform_paths
    monkeypatch.setattr(ae, "_anchor_file_problem", REAL_ANCHOR_CHECK)
    monkeypatch.setattr(platform_paths, "_windows_sddl", lambda _path: "")
    monkeypatch.setattr(platform_paths, "sddl_write_problem", lambda _sddl: "")
    restart()
    assert_every_grant(edge, True)
    os.link(store(edge)[name], tmp_path / "second-name")
    restart()
    assert_every_grant(edge, False)
    assert vms_capacity.capacity_status()["reason"] == reason
