"""LIC-W1 revision 2, after Codex FIX REQUIRED: no licensing path follows a second file name, a revocation reaches cached
grants at once, and an older setup can't restore what a revocation removed. Every registry here is SYNTHETIC."""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

import appliance_entitlements as ae
import vms_capacity
from test_entitlement_trust_1271 import (  # noqa: F401
    IDENTITY, accept, analytics_doc, capacity_snapshot, edge, feature_snapshot, record, registry)
from test_windows_grant_consumers_1341 import (
    assert_every_grant, holds_every_grant, restart, retiring, revoking, setup_runs, store)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "appliance-agent" / "system"))
import capacity_accept_windows as accept_base
import trust_install_windows as helper

pytestmark = pytest.mark.real_entitlements


def revoked(edge):
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    setup_runs(edge, revoking(edge))


# ---- Finding 1: hard links

@pytest.mark.parametrize("name", ["feature", "analytics"])
def test_a_second_name_on_a_state_file_is_refused(edge, tmp_path, name):
    holds_every_grant(edge)
    target = {"feature": ae.STATE_FILE, "analytics": ae.ANALYTICS_STATE_FILE}[name]
    os.link(target, tmp_path / "second-name")
    restart()
    if name == "feature":
        assert ae.feature_allowed("talk_down") is False
    else:
        assert ae.camera_analytic_grants(now=edge.now) == {}


def test_the_check_is_on_the_open_handle_not_only_the_path(edge, tmp_path, monkeypatch):
    """The race the first pass left: a name appears after the path check, before the open."""
    holds_every_grant(edge)
    monkeypatch.setattr(ae, "_anchor_file_problem", lambda _path: "")
    os.link(ae.TRUST_REGISTRY_FILE, tmp_path / "second-name")
    data, problem = ae._read_trust_file(ae.TRUST_REGISTRY_FILE)
    assert data is None and problem


def test_a_planted_temporary_name_is_never_written_through(edge, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    os.link(victim, ae.STATE_FILE.with_suffix(".tmp"))
    os.link(victim, ae.ANALYTICS_STATE_FILE.with_suffix(".tmp"))
    holds_every_grant(edge)
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_saving_state_replaces_a_hard_linked_name_instead_of_writing_through_it(edge, tmp_path):
    holds_every_grant(edge)
    other = tmp_path / "second-name"
    os.link(ae.STATE_FILE, other)
    before = other.read_bytes()
    assert ae._write_state({"replaced": True}) is True
    assert other.read_bytes() == before and os.stat(ae.STATE_FILE).st_nlink == 1


def test_a_failed_write_leaves_no_temporary_and_the_old_state(edge, monkeypatch):
    holds_every_grant(edge)
    before = ae.STATE_FILE.read_bytes()

    def refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ae.os, "replace", refuse)
    assert ae._write_state({"replaced": True}) is False
    assert ae.STATE_FILE.read_bytes() == before
    assert not list(ae.STATE_FILE.parent.glob(f".{ae.STATE_FILE.name}.*"))


def test_a_second_name_on_the_accepted_file_keeps_the_rollback_floor(edge, tmp_path, monkeypatch):
    holds_every_grant(edge)
    os.link(vms_capacity.ACCEPTED_FILE, tmp_path / "second-name")
    assert ae._accepted_trust_generation() >= 1
    ae.TRUST_REGISTRY_FILE.unlink()
    ae.TRUST_STATE_FILE.unlink()
    restart()
    assert ae.feature_allowed("talk_down") is False


@pytest.mark.parametrize("reader", ["json", "bytes"])
def test_setup_helpers_refuse_a_second_name_on_the_open_handle(tmp_path, reader):
    path = tmp_path / "trust.json"
    path.write_text("{}", encoding="utf-8")
    os.link(path, tmp_path / "second-name")
    with pytest.raises(accept_base.Reject, match="hard_linked_trust_file"):
        accept_base._json_file(path, 1024) if reader == "json" else helper._read(path, 1024)


def test_a_single_name_file_still_reads(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    assert accept_base._json_file(path, 1024) == {"ok": True}
    assert helper._read(path, 1024) == b'{"ok": true}'


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_a_saved_state_keeps_its_file_mode(edge):
    holds_every_grant(edge)
    os.chmod(ae.STATE_FILE, 0o640)
    ae._write_state({"replaced": True})
    assert stat.S_IMODE(os.stat(ae.STATE_FILE).st_mode) == 0o640


# ---- Finding 2: cached grants

def test_a_revocation_reaches_the_grant_cache_inside_its_ten_seconds(edge):
    """No restart between the trust change and the read: a running process must not keep a revoked grant."""
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True
    data = revoking(edge)
    registry_path = ae.TRUST_REGISTRY_FILE
    held = dict(ae._grant_cache)
    import capacity_accept
    registry_path.write_bytes(capacity_accept.canonical_trust_registry(data))
    ae._grant_cache.update(held)
    assert ae._grant_cache["at"] is not None and time.monotonic() - ae._grant_cache["at"] < ae._GRANT_CACHE_SECONDS
    assert ae.camera_analytic_allowed("cam-1", "lpr") is False


@pytest.mark.parametrize("name", ["registry", "trust_state", "trust"])
def test_any_trust_file_change_drops_the_cache(edge, name):
    holds_every_grant(edge)
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True
    before = ae._trust_fingerprint()
    path = store(edge)[name]
    path.write_bytes(path.read_bytes() + b"\n")
    assert ae._trust_fingerprint() != before


def test_the_cache_still_answers_when_nothing_changed(edge, monkeypatch):
    holds_every_grant(edge)
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True
    monkeypatch.setattr(ae, "_grants_from_state", lambda _now: pytest.fail("cache was not used"))
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_adding_a_trust_hard_link_invalidates_cached_analytics_immediately(edge, tmp_path):
    holds_every_grant(edge)
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True
    before = dict(ae._grant_cache)
    assert before["grants"] is not None
    os.link(ae.TRUST_REGISTRY_FILE, tmp_path / "registry-second-name")
    assert ae._trust_fingerprint() != before["trust"]
    assert ae.camera_analytic_allowed("cam-1", "lpr") is False


def test_revocation_stops_grants_with_no_cloud_and_no_clock_change(edge):
    """Offline operation: the installed registry alone decides."""
    revoked(edge)
    assert_every_grant(edge, False)


def test_a_retired_signer_keeps_offline_grants(edge):
    """Legitimate customers keep working through a normal rotation."""
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    assert_every_grant(edge, True)


@pytest.mark.skipif(os.name != "nt", reason="the fallback rule is Windows-only")
@pytest.mark.parametrize("trust", ["registry", "trust_state"])
def test_a_planted_fallback_is_ignored_once_trust_files_exist(edge, trust):
    revoked(edge)
    vms_capacity.ACCEPTED_FILE.unlink()
    if trust == "trust_state":
        ae.TRUST_REGISTRY_FILE.unlink()
    vms_capacity.FALLBACK_FILE.write_text(json.dumps({
        "schema": 1, "source": "migrated_unsigned_capacity", "camera_slot_quantity": 100, **IDENTITY}), encoding="utf-8")
    assert vms_capacity._fallback_capacity() is None
    assert vms_capacity.load_capacity() is None


# ---- Finding 3: older setups. Trust files survive them, so the next current run must find the revocation intact.

def older_setup_overwrites_the_keyset(edge):
    flat = {edge.k1.key_id: edge.k1.public_b64, edge.k2.key_id: edge.k2.public_b64}
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": flat}), encoding="utf-8")
    restart()


def test_an_older_setup_then_a_current_runtime_keeps_the_revocation(edge):
    revoked(edge)
    older_setup_overwrites_the_keyset(edge)
    assert_every_grant(edge, False)
    assert vms_capacity.capacity_status()["reason"] in {"revoked", "unknown_key", "registry_rollback"}


def test_upgrading_after_an_older_setup_never_restores_a_revoked_grant(edge):
    revoked(edge)
    older_setup_overwrites_the_keyset(edge)
    setup_runs(edge, revoking(edge))
    assert_every_grant(edge, False)


@pytest.mark.parametrize("generation", [1, 2])
def test_upgrading_after_an_older_setup_refuses_an_older_registry(edge, generation):
    revoked(edge)
    older_setup_overwrites_the_keyset(edge)
    stale = {1: registry(edge, [(edge.k1, "active", {})], 1), 2: retiring(edge)}[generation]
    with pytest.raises(helper.Reject):
        setup_runs(edge, stale)
    assert_every_grant(edge, False)


def test_a_deleted_registry_after_an_older_setup_stays_failed_closed(edge):
    revoked(edge)
    older_setup_overwrites_the_keyset(edge)
    ae.TRUST_REGISTRY_FILE.unlink()
    restart()
    assert_every_grant(edge, False)


@pytest.mark.parametrize("contents", ["{", "[]", '{"trust_generation": "1"}'])
def test_present_malformed_accepted_record_keeps_rollback_floor(edge, contents):
    holds_every_grant(edge)
    vms_capacity.ACCEPTED_FILE.write_text(contents, encoding="utf-8")
    ae.TRUST_REGISTRY_FILE.unlink()
    ae.TRUST_STATE_FILE.unlink()
    restart()
    assert ae._accepted_trust_generation() >= 1
    assert ae._trusted_keys() == {}
    assert ae.feature_allowed("talk_down") is False


def test_oversized_parseable_accepted_record_keeps_rollback_floor(edge):
    holds_every_grant(edge)
    payload = b'{"trust_generation":0}' + b" " * (512 * 1024)
    assert len(payload) > 512 * 1024
    vms_capacity.ACCEPTED_FILE.write_bytes(payload)
    assert ae._accepted_trust_generation() >= 1


def test_unreadable_accepted_record_keeps_rollback_floor(edge, monkeypatch):
    holds_every_grant(edge)
    accepted = vms_capacity.ACCEPTED_FILE
    real_read = ae._read_single_name

    def unreadable(path, limit):
        if path == accepted:
            raise PermissionError("synthetic read denial")
        return real_read(path, limit)

    monkeypatch.setattr(ae, "_read_single_name", unreadable)
    ae.TRUST_REGISTRY_FILE.unlink()
    ae.TRUST_STATE_FILE.unlink()
    restart()
    assert ae._accepted_trust_generation() >= 1
    assert ae._trusted_keys() == {}
    assert ae.feature_allowed("talk_down") is False


def test_genuinely_missing_accepted_record_retains_legacy_floor(edge):
    vms_capacity.ACCEPTED_FILE.unlink(missing_ok=True)
    assert ae._accepted_trust_generation() == 0


@pytest.mark.parametrize("contents", ['{"trust_generation": 0}', '{"schema": 2, "accepted_key": "legacy"}'])
def test_valid_legacy_accepted_record_retains_legacy_floor(edge, contents):
    vms_capacity.ACCEPTED_FILE.write_text(contents, encoding="utf-8")
    assert ae._accepted_trust_generation() == 0


def test_a_fresh_grant_from_an_active_key_recovers_after_the_upgrade(edge):
    """The compatibility half: a paying customer returns to service with a new signed grant."""
    revoked(edge)
    older_setup_overwrites_the_keyset(edge)
    setup_runs(edge, revoking(edge))
    accept(edge, capacity_snapshot(edge, edge.k2, 4))
    assert vms_capacity.load_capacity() == record(4)
