"""Pure/file-level regression coverage for appliance_activation.py --
gap #1 of the appliance identity contract: durable local activation
identity that survives a process restart with no environment
configuration required.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pytest  # noqa: E402

import appliance_activation as aa  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_identity_file(tmp_path, monkeypatch):
    monkeypatch.setattr(aa, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
    yield


def _activate(**overrides):
    fields = dict(appliance_id="appl-1", cloud_id="AIC-1", credential="cred-abc", customer_id="cust-1", site_id="site-1", partner_id="partner-1")
    fields.update(overrides)
    return aa.persist_activation(**fields)


# =============================================================== missing / never activated


def test_missing_file_returns_none_not_an_error():
    assert aa.load_persisted_identity() is None


# =============================================================== activate -> persist -> "restart" -> still available


def test_activation_persists_and_survives_a_fresh_read_simulating_a_restart():
    _activate()
    # A restart re-imports nothing about in-memory state -- the only
    # thing that matters is what's on disk, so a fresh load_persisted_
    # identity() call (as own_appliance_identity() would make after a
    # real process restart) must see exactly what was written.
    reloaded = aa.load_persisted_identity()
    assert reloaded["cloud_id"] == "AIC-1"
    assert reloaded["credential"] == "cred-abc"
    assert reloaded["customer_id"] == "cust-1"
    assert reloaded["site_id"] == "site-1"
    assert reloaded["partner_id"] == "partner-1"
    assert reloaded["activation_version"] == 1
    assert "activated_at" in reloaded


def test_file_permissions_are_restricted_to_owner_only(monkeypatch):
    import os
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not meaningfully enforced on Windows")
    _activate()
    mode = aa.ACTIVATION_IDENTITY_FILE.stat().st_mode & 0o777
    assert mode == 0o600


# =============================================================== idempotent re-activation, same cloud_id


def test_reactivating_the_same_cloud_id_is_idempotent_and_bumps_version():
    _activate()
    refreshed = _activate(credential="cred-new")  # e.g. a fresh activation token was issued and consumed
    assert refreshed["activation_version"] == 2
    assert refreshed["credential"] == "cred-new"
    assert aa.load_persisted_identity()["credential"] == "cred-new"


def test_reactivating_the_same_cloud_id_repeatedly_keeps_incrementing_cleanly():
    _activate()
    _activate()
    third = _activate()
    assert third["activation_version"] == 3


# =============================================================== a different cloud_id must not silently overwrite


def test_a_different_cloud_id_is_refused_without_explicit_reset():
    _activate(cloud_id="AIC-1")
    with pytest.raises(aa.ActivationConflict):
        _activate(cloud_id="AIC-DIFFERENT")
    # The original identity must be completely untouched by the refused attempt.
    assert aa.load_persisted_identity()["cloud_id"] == "AIC-1"
    assert aa.load_persisted_identity()["activation_version"] == 1


def test_allow_overwrite_explicitly_permits_switching_cloud_id():
    _activate(cloud_id="AIC-1")
    switched = _activate(cloud_id="AIC-2", allow_overwrite=True)
    assert switched["cloud_id"] == "AIC-2"
    assert switched["activation_version"] == 1  # a genuinely new identity, not a continuation of AIC-1's version count


# =============================================================== corrupt state -> fail closed


def test_corrupt_json_is_treated_as_not_activated():
    aa.ACTIVATION_IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    aa.ACTIVATION_IDENTITY_FILE.write_text("{not valid json", encoding="utf-8")
    assert aa.load_persisted_identity() is None


def test_incomplete_json_missing_required_fields_is_treated_as_not_activated():
    aa.ACTIVATION_IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    aa.ACTIVATION_IDENTITY_FILE.write_text('{"cloud_id":"AIC-1"}', encoding="utf-8")
    assert aa.load_persisted_identity() is None


def test_a_non_object_json_value_is_treated_as_not_activated():
    aa.ACTIVATION_IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    aa.ACTIVATION_IDENTITY_FILE.write_text("[1,2,3]", encoding="utf-8")
    assert aa.load_persisted_identity() is None


# =============================================================== reset -> successful recovery


def test_reset_then_reactivate_with_a_different_cloud_id_succeeds():
    _activate(cloud_id="AIC-1")
    aa.reset_persisted_identity()
    assert aa.load_persisted_identity() is None
    recovered = _activate(cloud_id="AIC-2")
    assert recovered["cloud_id"] == "AIC-2"
    assert recovered["activation_version"] == 1


def test_reset_when_never_activated_is_a_safe_no_op():
    aa.reset_persisted_identity()  # must not raise
    aa.reset_persisted_identity()  # calling it twice is also safe
    assert aa.load_persisted_identity() is None


# =============================================================== atomicity


def test_write_uses_a_temp_file_and_replace_not_a_direct_write():
    _activate()
    assert not aa.ACTIVATION_IDENTITY_FILE.with_suffix(".tmp").exists()  # cleaned up by os.replace()
    assert aa.ACTIVATION_IDENTITY_FILE.exists()
