"""LIC-W1 revision 3, after the independent Codex review of R0 77070be (2026-11-06, P2).

1. Only a genuine legacy accepted record keeps the zero rollback floor: an exact integer zero (or the historical
   omission of both signer and trust_generation), signer metadata consistent with the snapshot and accepted key, and
   real historical fields. False, 0.0, a generation missing beside signer metadata, and placeholder strings fail closed.
2. The accepted record is part of the grant-cache trust fingerprint, so a change to it never waits for the TTL.
Every registry here is SYNTHETIC."""
import copy
import json
import time

import pytest

import appliance_entitlements as ae
import vms_capacity
from test_entitlement_trust_1271 import edge  # noqa: F401
from test_windows_grant_consumers_1341 import holds_every_grant, retiring, setup_runs

pytestmark = pytest.mark.real_entitlements


def _accepted() -> dict:
    return json.loads(vms_capacity.ACCEPTED_FILE.read_text(encoding="utf-8"))


def _write(state: dict) -> None:
    vms_capacity.ACCEPTED_FILE.write_text(json.dumps(state), encoding="utf-8")


def _legacy_with_signer(state: dict) -> dict:
    legacy = copy.deepcopy(state)
    legacy["trust_generation"] = 0
    return legacy


def _historical(state: dict) -> dict:
    legacy = copy.deepcopy(state)
    legacy.pop("signer", None)
    legacy.pop("trust_generation", None)
    return legacy


# ---- 1. legacy recognition

@pytest.mark.parametrize("shape", ["historical", "zero_with_signer", "additive_key"])
def test_a_genuine_legacy_record_keeps_the_zero_floor(edge, shape):
    holds_every_grant(edge)
    state = _accepted()
    assert "signer" in state and "trust_generation" in state, "fixture no longer writes option A metadata"
    legacy = {"historical": _historical, "zero_with_signer": _legacy_with_signer,
              "additive_key": lambda s: dict(_historical(s), future_note="ignored")}[shape](state)
    _write(legacy)
    assert ae._accepted_trust_generation() == 0


def _break(state: dict, how: str) -> dict:
    broken = _legacy_with_signer(state)
    if how == "generation_false":
        broken["trust_generation"] = False
    elif how == "generation_float_zero":
        broken["trust_generation"] = 0.0
    elif how == "signer_without_generation":
        broken.pop("trust_generation")
    elif how == "generation_without_signer":
        broken.pop("signer")
    elif how == "signer_key_id_mismatch":
        broken["signer"]["key_id"] = broken["signer"]["key_id"] + "-other"
    elif how == "signer_key_hash_mismatch":
        broken["signer"]["public_key_sha256"] = "0" * 64
    elif how == "signer_extra_field":
        broken["signer"]["note"] = "x"
    elif how == "schema_float":
        broken["schema"] = 2.0
    elif how == "accepted_key_placeholder":
        broken["accepted_key"] = ""
    elif how == "accepted_key_wrong_length":
        broken["accepted_key"] = "AAAA"
    elif how == "key_id_placeholder":
        broken["snapshot"]["signature"]["key_id"] = ""
    elif how == "issued_at_placeholder":
        broken["high_water"]["issued_at"] = "x"
    elif how == "digest_placeholder":
        broken["high_water"]["digest"] = ""
    elif how == "accepted_at_placeholder":
        broken["accepted_at"] = "x"
    elif how == "source_not_cloud":
        broken["source"] = "local"
    return broken


@pytest.mark.parametrize("how", [
    "generation_false", "generation_float_zero", "signer_without_generation", "generation_without_signer",
    "signer_key_id_mismatch", "signer_key_hash_mismatch", "signer_extra_field", "schema_float",
    "accepted_key_placeholder", "accepted_key_wrong_length", "key_id_placeholder", "issued_at_placeholder",
    "digest_placeholder", "accepted_at_placeholder", "source_not_cloud",
])
def test_a_counterfeit_legacy_record_keeps_the_rollback_floor(edge, how):
    holds_every_grant(edge)
    _write(_break(_accepted(), how))
    assert ae._accepted_trust_generation() >= 1


def test_a_counterfeit_legacy_record_never_reopens_the_flat_keyset(edge):
    holds_every_grant(edge)
    _write(_break(_accepted(), "generation_false"))
    ae.TRUST_REGISTRY_FILE.unlink()
    ae.TRUST_STATE_FILE.unlink()
    assert ae._trusted_keys() == {}
    assert ae.feature_allowed("talk_down") is False


# ---- 2. the accepted record is in the cache fingerprint

def test_changing_the_accepted_record_changes_the_trust_fingerprint(edge):
    holds_every_grant(edge)
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True
    before = ae._trust_fingerprint()
    vms_capacity.ACCEPTED_FILE.write_bytes(vms_capacity.ACCEPTED_FILE.read_bytes() + b"\n")
    assert ae._trust_fingerprint() != before


def test_a_raised_rollback_floor_reaches_the_grant_cache_inside_its_ten_seconds(edge):
    """No restart: a cached grant must not outlive an accepted-record change that puts the floor above the registry."""
    holds_every_grant(edge)
    setup_runs(edge, retiring(edge))
    assert ae.camera_analytic_allowed("cam-1", "lpr") is True
    held = dict(ae._grant_cache)
    state = _accepted()
    state["trust_generation"] = 10_000
    _write(state)
    ae._grant_cache.update(held)
    assert ae._grant_cache["at"] is not None and time.monotonic() - ae._grant_cache["at"] < ae._GRANT_CACHE_SECONDS
    assert ae.camera_analytic_allowed("cam-1", "lpr") is False
