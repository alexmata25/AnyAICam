"""Requirement 9 (Windows installer): a Windows VMS must find and trust its signing keyset / VMS capacity.

Before: the trust files were fixed /etc/anyaicam-update paths and the Windows check was skipped, so a Windows install
trusted nothing and licensed 0 cameras. Now they live in %ProgramData%\\AnyAiCam\\update (known-folder API, never the
environment) and must be owned by and writable only by SYSTEM / Administrators -- the Windows counterpart of the Linux
root-owned, not-group/other-writable rule."""
import os
from pathlib import Path

import pytest

import appliance_entitlements
import platform_paths
import vms_capacity

SAFE = "O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"


# ---------------------------------------------------------------- the SDDL rule (pure; runs on every platform)
@pytest.mark.parametrize("sddl", [
    SAFE,
    "O:BAG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",                         # no read for users at all
    "O:S-1-5-18G:SYD:PAI(A;;FA;;;S-1-5-18)(A;;FA;;;S-1-5-32-544)(A;;FR;;;WD)",  # numeric SIDs; everyone read-only
    "O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICIIO;FA;;;CO)",                        # inherit-only CREATOR OWNER: not this object
])
def test_only_system_and_administrators_writing_is_safe(sddl):
    assert platform_paths.sddl_write_problem(sddl) == ""


@pytest.mark.parametrize("sddl,who", [
    ("O:SYG:SYD:PAI(A;;FA;;;SY)(A;;FW;;;BU)", "BU"),                   # Users may write
    ("O:SYG:SYD:PAI(A;;FA;;;SY)(A;;FA;;;WD)", "WD"),                   # Everyone full control
    ("O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x1301bf;;;AU)", "AU"),              # Authenticated Users modify (hex mask)
    ("O:SYG:SYD:PAI(A;;FA;;;SY)(A;;WD;;;S-1-5-21-1-2-3-1001)", "S-1-5-21-1-2-3-1001"),  # a user may rewrite the ACL
    ("O:SYG:SYD:PAI(A;;FA;;;SY)(A;;GW;;;BU)", "BU"),                   # generic write
])
def test_anyone_else_able_to_modify_is_refused(sddl, who):
    assert platform_paths.sddl_write_problem(sddl) == f"is writable by {who}"


def test_a_user_owned_or_unprotected_entry_is_refused():
    assert "owned by S-1-5-21-1-2-3-1001" in platform_paths.sddl_write_problem(
        "O:S-1-5-21-1-2-3-1001G:SYD:PAI(A;;FA;;;SY)")
    assert "no access list" in platform_paths.sddl_write_problem("O:SYG:SY")
    assert "writable by" in platform_paths.sddl_write_problem("O:SYG:SYD:(A;;0xZZ;;;BU)")  # unparseable mask: unsafe


# ---------------------------------------------------------------- locations
@pytest.mark.skipif(os.name == "nt", reason="Linux layout")
def test_linux_locations_are_unchanged():
    assert platform_paths.UPDATE_DIR == Path("/etc/anyaicam-update")
    assert platform_paths.CONFIG_DIR == Path("/opt/anyaicam/data/config")
    assert vms_capacity._DEFAULT_ACCEPTED_FILE == Path("/etc/anyaicam-update/vms_capacity_accepted.json")


@pytest.mark.skipif(os.name != "nt", reason="needs Windows")
def test_windows_locations_use_programdata_from_the_known_folder_api_not_the_environment(monkeypatch):
    real = platform_paths._windows_program_data()
    monkeypatch.setenv("ProgramData", r"C:\attacker\controlled")
    assert platform_paths._windows_program_data() == real            # the environment variable is ignored
    assert real.exists() and str(real).lower().endswith("programdata")
    # (conftest redirects the module attributes to a temp trust anchor for every test; check the platform layout)
    assert platform_paths.UPDATE_DIR == real / "AnyAiCam" / "update"
    assert platform_paths.CONFIG_DIR == real / "AnyAiCam" / "config"
    assert vms_capacity._DEFAULT_ACCEPTED_FILE.parent == platform_paths.UPDATE_DIR


@pytest.mark.skipif(os.name != "nt", reason="needs Windows")
def test_a_real_user_writable_file_is_refused_on_windows(tmp_path):
    """A file an ordinary user created (owner = that user, user may write) must never be trusted."""
    candidate = tmp_path / "entitlement_signing_keys.json"
    candidate.write_text('{"keys": {}}', encoding="utf-8")
    problem = platform_paths.windows_write_problem(candidate)
    assert problem and ("owned by" in problem or "writable by" in problem)


# ---------------------------------------------------------------- the readers use it on Windows
def test_the_keyset_check_applies_the_windows_rule_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(appliance_entitlements.os, "name", "nt")
    verdicts = {tmp_path: "", tmp_path / "keys.json": "is writable by BU"}
    monkeypatch.setattr(platform_paths, "windows_write_problem", lambda path: verdicts[path])
    assert appliance_entitlements._anchor_file_problem(tmp_path / "keys.json") == f"{tmp_path / 'keys.json'} is writable by BU"
    verdicts[tmp_path / "keys.json"] = ""
    assert appliance_entitlements._anchor_file_problem(tmp_path / "keys.json") == ""


def test_an_unsafe_windows_capacity_store_is_never_used(monkeypatch, tmp_path):
    accepted = tmp_path / "vms_capacity_accepted.json"
    accepted.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vms_capacity, "ACCEPTED_FILE", accepted)
    monkeypatch.setattr(vms_capacity, "_DEFAULT_ACCEPTED_FILE", accepted)
    monkeypatch.setattr(vms_capacity.os, "name", "nt")
    monkeypatch.setattr(platform_paths, "windows_write_problem", lambda path: "is writable by BU" if path == tmp_path else "")
    assert vms_capacity._accepted_signed() == (True, None)   # present but unusable: fails closed


def test_a_windows_fallback_that_users_could_write_is_ignored(monkeypatch, tmp_path):
    """Step 3 (req 9): the capacity fallback is read only under the SYSTEM/Administrators-only rule on Windows."""
    fallback = tmp_path / "vms_capacity_fallback.json"
    fallback.write_text('{"schema": 1, "source": "migrated_unsigned_capacity", "camera_slot_quantity": 64}', encoding="utf-8")
    monkeypatch.setattr(vms_capacity, "FALLBACK_FILE", fallback)
    monkeypatch.setattr(appliance_entitlements.os, "name", "nt")
    monkeypatch.setattr(platform_paths, "windows_write_problem", lambda path: "is writable by BU")
    assert vms_capacity._fallback_capacity() is None
