"""TRUST-1 + G2 on the VMS side (Linux 1.2.7.1).

The VMS resolves the signer of every accepted capacity / cached feature / analytics grant against the CURRENT protected
trust registry (/etc/anyaicam-update/entitlement_trust_registry.json + entitlement_trust_state.json, installer/applier
delivered only) and validates already-accepted capacity in accepted mode (no future/expiry check). Every fail-closed
outcome carries an explicit diagnostic (vms_capacity.capacity_status). Green ids (GREEN-1271-TEST-DEFINITIONS.md 1b, 9)
are named in each docstring. Keys are fresh in-process Ed25519 keys; the clock is injected.
"""
import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import appliance_entitlements as ae
import appliance_identity
import vms_capacity

SYSTEM_DIR = Path(__file__).resolve().parents[2] / "appliance-agent" / "system"
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))
import capacity_accept  # noqa: E402

pytestmark = pytest.mark.real_entitlements

IDENTITY = {"appliance_id": "appl-trust-vms", "cloud_id": "AIC-trust-vms", "customer_id": "cust-trust-vms"}
PURPOSES = ["analytics", "capacity", "features", "local_access"]
APP_DIR = Path(__file__).resolve().parents[1]


class Key:
    def __init__(self, key_id):
        self.key_id = key_id
        self.private = Ed25519PrivateKey.generate()
        self.raw = self.private.public_key().public_bytes_raw()
        self.public_b64 = base64.b64encode(self.raw).decode()
        self.private_b64 = base64.b64encode(self.private.private_bytes_raw()).decode()

    def sign(self, body):
        return {**body, "signature": appliance_identity.sign_body(body, key_id=self.key_id,
                                                                  private_key_b64=self.private_b64)}


def verify_any(key, message, signature):
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(signature, message)
        return True
    except Exception:
        return False


@pytest.fixture
def edge(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    trust_dir = tmp_path / "trust-anchor"
    trust_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setenv("ANYAICAM_APP_SECRETS", "appliance-secret-for-trust-tests")
    monkeypatch.setattr(ae, "TRUST_ANCHOR_FILE", trust_dir / "entitlement_signing_keys.json")
    monkeypatch.setattr(ae, "TRUST_REGISTRY_FILE", trust_dir / "entitlement_trust_registry.json")
    monkeypatch.setattr(ae, "TRUST_STATE_FILE", trust_dir / "entitlement_trust_state.json")
    monkeypatch.setattr(ae, "ANALYTICS_STATE_FILE", tmp_path / "analytics_entitlements.json")
    monkeypatch.setattr(ae, "STATE_FILE", tmp_path / "feature_entitlements.json")
    monkeypatch.setattr(ae, "_anchor_file_problem", lambda _path: "")
    monkeypatch.setattr(ae, "_local_identity", lambda: dict(IDENTITY))
    monkeypatch.setattr(vms_capacity, "ACCEPTED_FILE", trust_dir / "vms_capacity_accepted.json")
    monkeypatch.setattr(vms_capacity, "FALLBACK_FILE", trust_dir / "vms_capacity_fallback.json")
    monkeypatch.setattr(vms_capacity, "SIGNED_FILE", tmp_path / "vms_capacity_signed.json")
    ae._grant_cache.update({"at": None, "mtime": None, "grants": None, "expires": None})
    vms_capacity._reset_status_for_tests()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "cam1-event.mp4").write_bytes(b"\x00event-clip" * 512)
    (recordings / "index.json").write_text('{"events": 1}', encoding="utf-8")

    class Edge:
        pass
    edge = Edge()
    edge.now, edge.dir, edge.recordings = now, trust_dir, recordings
    edge.k1, edge.k2, edge.lab = Key("anyaicam-identity-k1"), Key("anyaicam-identity-k2"), Key("lab-signer-1")
    edge.recordings_before = {p.name: p.read_bytes() for p in recordings.iterdir()}
    return edge


def registry(edge, entries, generation):
    keys = {}
    for key, state, options in entries:
        entry = {"public_key": key.public_b64, "state": state, "purposes": sorted(options.get("purposes", PURPOSES)),
                 "environment": options.get("environment", "production"),
                 "changed_generation": options.get("changed_generation", 1)}
        if "appliance_ids" in options:
            entry["appliance_ids"] = options["appliance_ids"]
        keys[key.key_id] = entry
    return {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": "production",
            "generation": generation, "issued_at": edge.now.isoformat(), "keys": keys}


def install(edge, document):
    """What installer step 13 / the applier do: registry first, then the monotonic state."""
    data = capacity_accept.canonical_trust_registry(document)
    prior = json.loads(ae.TRUST_STATE_FILE.read_text()) if ae.TRUST_STATE_FILE.exists() else None
    state = capacity_accept.next_trust_state(capacity_accept.parse_trust_registry(document),
                                             hashlib.sha256(data).hexdigest(), prior)
    ae.TRUST_REGISTRY_FILE.write_bytes(data)
    ae.TRUST_STATE_FILE.write_text(json.dumps(state, sort_keys=True) + "\n")
    return data


def capacity_snapshot(edge, key, quantity, *, issued=None, hours=12):
    issued = issued or edge.now
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
            "features": {"talk_down": True, "voice_call": False},
            "camera_capacity": {"camera_slot_quantity": quantity, "plan_camera_slots": quantity,
                                "vms_license_capacity": quantity},
            "issued_at": issued.isoformat(), "expires_at": (issued + timedelta(hours=hours)).isoformat()}
    return key.sign(body)


def accept(edge, snapshot, *, trust=None, current=None):
    trust = trust if trust is not None else ae._trust_registry()
    state, outcome = capacity_accept._advance(snapshot, trust, IDENTITY, current, now=edge.now, verifier=verify_any)
    vms_capacity.ACCEPTED_FILE.write_text(json.dumps(state), encoding="utf-8")
    return state


def record(quantity):
    return {"camera_slot_quantity": quantity, "plan_camera_slots": quantity, "vms_license_capacity": quantity,
            "source": "signed"}


def assert_recordings_untouched(edge):
    assert {p.name: p.read_bytes() for p in edge.recordings.iterdir()} == edge.recordings_before


# ----------------------------------------------------------------------------------------------- TRUST-1
def test_u_t1_revoked_signer_stops_capacity_retired_signer_keeps_it(edge):
    """U-T1 / T-INV-3 / T-E1: revoke K -> load_capacity() None (recordings untouched); retire K -> capacity."""
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    accept(edge, capacity_snapshot(edge, edge.k1, 4))
    assert vms_capacity.load_capacity() == record(4)
    install(edge, registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                                  (edge.k2, "active", {"changed_generation": 2})], 2))
    assert vms_capacity.load_capacity() == record(4)
    install(edge, registry(edge, [(edge.k1, "revoked", {"changed_generation": 3}),
                                  (edge.k2, "active", {"changed_generation": 2})], 3))
    accepted_before = vms_capacity.ACCEPTED_FILE.read_bytes()
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.licensed_camera_numbers([1, 2, 3]) == []
    status = vms_capacity.capacity_status()
    assert (status["state"], status["reason"]) == ("fail_closed", "revoked")
    assert vms_capacity.ACCEPTED_FILE.read_bytes() == accepted_before  # kept for the anti-replay floor
    assert_recordings_untouched(edge)


def test_u_t1_a_bound_key_dropped_from_the_registry_counts_as_revoked(edge):
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    accept(edge, capacity_snapshot(edge, edge.k1, 4))
    install(edge, registry(edge, [(edge.k2, "active", {"changed_generation": 2})], 2))
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "revoked"


def test_u_t2_an_unknown_signer_is_refused_even_with_its_key_embedded(edge):
    """U-T2 / T-INV-1: accepted_key in the file never confers trust."""
    accept(edge, capacity_snapshot(edge, edge.k1, 4), trust={edge.k1.key_id: edge.k1.raw})
    install(edge, registry(edge, [(edge.k2, "active", {})], 1))
    assert json.loads(vms_capacity.ACCEPTED_FILE.read_text())["accepted_key"] == edge.k1.public_b64
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "unknown_key"


def test_u_t3_a_lower_registry_generation_on_disk_is_registry_rollback_and_reinstall_recovers(edge):
    """U-T3 / T-E3 (read side): the older generation is never trusted; the explicit diagnostic is registry_rollback;
    re-installing the current generation restores capacity."""
    gen1 = registry(edge, [(edge.k1, "active", {})], 1)
    install(edge, gen1)
    accept(edge, capacity_snapshot(edge, edge.k1, 4))
    gen2 = registry(edge, [(edge.k1, "active", {}), (edge.k2, "active", {"changed_generation": 2})], 2)
    install(edge, gen2)
    ae.TRUST_REGISTRY_FILE.write_bytes(capacity_accept.canonical_trust_registry(gen1))  # rollback by file copy
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "registry_rollback"
    install(edge, gen2)  # the installer --repair / applier path
    assert vms_capacity.load_capacity() == record(4)
    assert_recordings_untouched(edge)


def test_u_t4_rotation_keeps_the_old_grant_until_the_new_key_replaces_it(edge):
    """U-T4 / T-E2."""
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    current = accept(edge, capacity_snapshot(edge, edge.k1, 4))
    install(edge, registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                                  (edge.k2, "active", {"changed_generation": 2})], 2))
    assert vms_capacity.load_capacity() == record(4)
    with pytest.raises(capacity_accept.Reject, match="retired_cannot_sign"):
        accept(edge, capacity_snapshot(edge, edge.k1, 9, issued=edge.now + timedelta(seconds=1)), current=current)
    edge.now += timedelta(minutes=1)
    accept(edge, capacity_snapshot(edge, edge.k2, 6), current=current)
    assert vms_capacity.load_capacity() == record(6)


def test_u_t5_a_lab_signer_never_authorizes_capacity_even_on_disk_and_allow_listed(edge):
    """U-T5 / T-E4."""
    install(edge, registry(edge, [(edge.k1, "active", {}),
                                  (edge.lab, "active", {"environment": "lab",
                                                        "appliance_ids": [IDENTITY["appliance_id"]]})], 1))
    accept(edge, capacity_snapshot(edge, edge.lab, 64), trust={edge.lab.key_id: edge.lab.raw})  # forced onto disk
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "scope_denied"
    assert edge.lab.key_id not in ae.trusted_keys_for("capacity")


# ----------------------------------------------------------------------------------------------- G2
@pytest.mark.parametrize("behind", [timedelta(hours=24), timedelta(days=30), timedelta(days=365)])
def test_u_g1_clock_rollback_keeps_accepted_capacity(edge, behind):
    """U-G1 / G-INV-1 / G-E1: no bad_time on accepted state; the condition is reported, not enforced."""
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    accept(edge, capacity_snapshot(edge, edge.k1, 3))
    then = edge.now - behind
    assert vms_capacity.load_capacity(now=then) == record(3)
    assert vms_capacity.licensed_camera_numbers([5, 1, 2, 9], now=then) == [1, 2, 5]
    assert vms_capacity.camera_licensed(2, [1, 2, 5, 9], now=then) is True
    status = vms_capacity.capacity_status(now=then)
    assert status["state"] == "licensed" and status["clock_behind_trusted_time"] is True
    assert vms_capacity.capacity_status(now=edge.now)["clock_behind_trusted_time"] is False


def test_g2_legacy_install_without_registry_also_survives_clock_rollback(edge):
    """Existing (pre-registry, legacy flat keyset) installs get G2 too."""
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": {edge.k1.key_id: edge.k1.public_b64}}))
    accept(edge, capacity_snapshot(edge, edge.k1, 3))
    assert vms_capacity.load_capacity(now=edge.now - timedelta(days=30)) == record(3)


def test_u_g5_editing_the_accepted_file_fails_closed_with_a_reason(edge):
    """U-G5."""
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    state = accept(edge, capacity_snapshot(edge, edge.k1, 3))
    for edit in (lambda s: s["snapshot"]["camera_capacity"].update(camera_slot_quantity=64),
                 lambda s: s["signer"].update(key_id=edge.k2.key_id),
                 lambda s: s.update(accepted_key=edge.k2.public_b64)):
        changed = json.loads(json.dumps(state))
        edit(changed)
        vms_capacity.ACCEPTED_FILE.write_text(json.dumps(changed))
        assert vms_capacity.load_capacity() is None
        assert vms_capacity.capacity_status()["reason"]


# ----------------------------------------------------------------------------------------------- Green 9
def test_green_9_registry_deleted_after_install_fails_closed_never_legacy(edge):
    """Green 9.1/9.2/9.5: generation >= 1 then registry deleted -> registry_rollback (never the flat keyset, never
    accepted_key); re-installing the current generation restores capacity."""
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": {edge.k1.key_id: edge.k1.public_b64}}))
    current = registry(edge, [(edge.k1, "active", {})], 1)
    install(edge, current)
    accept(edge, capacity_snapshot(edge, edge.k1, 2))
    ae.TRUST_REGISTRY_FILE.unlink()
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "registry_rollback"
    ae.TRUST_STATE_FILE.unlink()  # both gone: the accepted state's own trust_generation still refuses legacy
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "registry_rollback"
    assert ae.trusted_keys_for("features") == {}
    install(edge, current)
    assert vms_capacity.load_capacity() == record(2)


def test_green_9_6_restoring_only_the_accepted_state_fails_closed_with_a_diagnostic(edge):
    """Backups must keep registry + trust state + accepted state together."""
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    saved = json.dumps(accept(edge, capacity_snapshot(edge, edge.k1, 2)))
    # A different appliance (or a reinstalled one) whose registry does not carry k1:
    ae.TRUST_REGISTRY_FILE.unlink()
    ae.TRUST_STATE_FILE.unlink()
    install(edge, registry(edge, [(edge.k2, "active", {})], 1))
    vms_capacity.ACCEPTED_FILE.write_text(saved)
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "unknown_key"


def test_green_9_3_legacy_mode_resolves_the_signer_in_the_flat_keyset_never_accepted_key(edge):
    accept(edge, capacity_snapshot(edge, edge.k1, 2), trust={edge.k1.key_id: edge.k1.raw})
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": {edge.k2.key_id: edge.k2.public_b64}}))
    assert ae._trust_registry().mode == "legacy"
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "unknown_key"
    ae.TRUST_ANCHOR_FILE.unlink()
    assert vms_capacity.load_capacity() is None
    assert vms_capacity.capacity_status()["reason"] == "no_trust_anchor"
    ae.TRUST_ANCHOR_FILE.write_text(json.dumps({"keys": {edge.k1.key_id: edge.k1.public_b64}}))
    assert vms_capacity.load_capacity() == record(2)  # existing installs keep working


def test_green_9_4_a_registry_or_keyset_from_cloud_sync_is_ignored(edge):
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    before = (ae.TRUST_REGISTRY_FILE.read_bytes(), ae.TRUST_STATE_FILE.read_bytes())
    rogue_registry = registry(edge, [(edge.k2, "active", {})], 99)
    snapshot = edge.k2.sign({"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
                             "features": {"talk_down": True, "voice_call": True},
                             "camera_capacity": {"camera_slot_quantity": 64},
                             "entitlement_trust_registry": rogue_registry,
                             "public_keys": {edge.k2.key_id: edge.k2.public_b64},
                             "issued_at": edge.now.isoformat(),
                             "expires_at": (edge.now + timedelta(hours=1)).isoformat()})
    assert ae.store_snapshot(snapshot).startswith("rejected:")
    assert vms_capacity.apply_sync({"camera_slot_quantity": 64}, snapshot) == "staged"
    assert (ae.TRUST_REGISTRY_FILE.read_bytes(), ae.TRUST_STATE_FILE.read_bytes()) == before
    assert edge.k2.key_id not in ae.trusted_keys_for("features")
    # No VMS module writes the registry or the trust state (only installer step 13, the root applier, the root
    # acceptor and the Windows runtime installer do).
    for source in APP_DIR.glob("*.py"):
        text = source.read_text(encoding="utf-8", errors="replace")
        for marker in ("TRUST_REGISTRY_FILE.write", "TRUST_STATE_FILE.write", "entitlement_trust_registry.json\").write"):
            assert marker not in text, source.name


def test_green_9_5_every_fail_closed_change_is_logged_once(edge, caplog):
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    accept(edge, capacity_snapshot(edge, edge.k1, 2))
    caplog.set_level(logging.INFO, logger="anyaicam.vms_capacity")
    for _ in range(5):
        assert vms_capacity.load_capacity() == record(2)
    install(edge, registry(edge, [(edge.k1, "revoked", {"changed_generation": 2}),
                                  (edge.k2, "active", {"changed_generation": 2})], 2))
    for _ in range(5):
        assert vms_capacity.load_capacity() is None
    failures = [r for r in caplog.records if "fail_closed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].levelno >= logging.WARNING
    assert "reason=revoked" in failures[0].getMessage()


# ----------------------------------------------------------------------------------------------- other purposes
def feature_snapshot(edge, key, *, issued=None, talk_down=True):
    issued = issued or edge.now
    return key.sign({"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
                     "features": {"talk_down": talk_down, "voice_call": False},
                     "issued_at": issued.isoformat(), "expires_at": (issued + timedelta(hours=24)).isoformat()})


def test_features_retired_cached_until_expiry_new_refused_revoked_denied(edge):
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    assert ae.store_snapshot(feature_snapshot(edge, edge.k1)) == "stored"
    install(edge, registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                                  (edge.k2, "active", {"changed_generation": 2})], 2))
    assert ae.feature_allowed("talk_down") is True  # cached, verify-only
    assert ae.store_snapshot(feature_snapshot(edge, edge.k1)) == "rejected:retired_cannot_sign"
    assert ae.store_snapshot(feature_snapshot(edge, edge.k2)) == "stored"
    install(edge, registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                                  (edge.k2, "revoked", {"changed_generation": 3})], 3))
    assert ae.feature_allowed("talk_down") is False


def test_features_need_the_features_purpose(edge):
    install(edge, registry(edge, [(edge.k1, "active", {"purposes": ["capacity"]})], 1))
    assert ae.store_snapshot(feature_snapshot(edge, edge.k1)) == "rejected:purpose_denied"


def analytics_doc(edge, key, *, issued=None):
    issued = issued or edge.now
    return key.sign({"type": "anyaicam.analytics_entitlements", "version": 1, **IDENTITY,
                     "camera_analytics": {"cam-1": ["lpr"]}, "issued_at": issued.isoformat(),
                     "expires_at": (issued + timedelta(hours=168)).isoformat()})


def test_analytics_retired_valid_until_expiry_revoked_denied(edge):
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    assert ae.store_analytics_grant(analytics_doc(edge, edge.k1), now=edge.now) == "stored"
    install(edge, registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                                  (edge.k2, "active", {"changed_generation": 2})], 2))
    assert ae.camera_analytic_grants(now=edge.now) == {"cam-1": frozenset({"lpr"})}
    assert ae.analytics_grant_held(now=edge.now) is True
    assert ae.store_analytics_grant(analytics_doc(edge, edge.k1), now=edge.now) == \
        "cleared:rejected:retired_cannot_sign"
    assert ae.store_analytics_grant(analytics_doc(edge, edge.k2), now=edge.now) == "stored"
    install(edge, registry(edge, [(edge.k1, "retired", {"changed_generation": 2}),
                                  (edge.k2, "revoked", {"changed_generation": 3})], 3))
    assert ae.camera_analytic_grants(now=edge.now) == {}


def test_analytics_lab_signer_only_through_the_appliance_allow_list(edge):
    """Closes N2b: an other-environment signer works for analytics only when this appliance is allow-listed."""
    install(edge, registry(edge, [(edge.k1, "active", {}),
                                  (edge.lab, "active", {"environment": "lab", "purposes": ["analytics"],
                                                        "appliance_ids": ["another-appliance"]})], 1))
    assert ae.store_analytics_grant(analytics_doc(edge, edge.lab), now=edge.now) == "cleared:rejected:scope_denied"
    install(edge, registry(edge, [(edge.k1, "active", {}),
                                  (edge.lab, "active", {"environment": "lab", "purposes": ["analytics"],
                                                        "appliance_ids": [IDENTITY["appliance_id"]],
                                                        "changed_generation": 2})], 2))
    assert ae.store_analytics_grant(analytics_doc(edge, edge.lab), now=edge.now) == "stored"


def test_local_access_uses_active_local_access_keys_only(edge):
    install(edge, registry(edge, [(edge.k1, "retired", {}), (edge.k2, "active", {"purposes": ["capacity"]}),
                                  (edge.lab, "active", {"purposes": ["local_access"]})], 1))
    assert set(ae.trusted_keys_for("local_access")) == {edge.lab.key_id}
    import local_customer_access
    assert set(local_customer_access._trusted_keys()) == {edge.lab.key_id}


# ----------------------------------------------------------------------------------------------- compatibility
def test_option_a_state_is_still_readable_by_a_127_reader(edge):
    """Rollback safety: 1.2.7 parses schema {1,2} and verifies the snapshot with accepted_key."""
    install(edge, registry(edge, [(edge.k1, "active", {})], 1))
    state = accept(edge, capacity_snapshot(edge, edge.k1, 3))
    assert state["schema"] == 2 and state["signer"]["key_id"] == edge.k1.key_id and state["trust_generation"] == 1
    body = {k: v for k, v in state["snapshot"].items() if k != "signature"}
    appliance_identity.verify_signed_body(body, state["snapshot"]["signature"],
                                          public_keys={state["snapshot"]["signature"]["key_id"]: state["accepted_key"]})


def test_the_registry_and_state_paths_are_fixed_beside_the_trust_anchor():
    source = (APP_DIR / "appliance_entitlements.py").read_text(encoding="utf-8")
    assert 'TRUST_REGISTRY_FILE = _UPDATE_DIR / "entitlement_trust_registry.json"' in source
    assert 'TRUST_STATE_FILE = _UPDATE_DIR / "entitlement_trust_state.json"' in source
