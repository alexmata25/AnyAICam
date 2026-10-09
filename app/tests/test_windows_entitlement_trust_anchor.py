"""Windows entitlement trust anchor (app/windows_trust_anchor.py, 2026-10-08).

On native Windows the keyset path was C:\\etc\\anyaicam-update\\..., which a
standard user may create, and the POSIX-only ownership check passed every
non-POSIX system: a user could plant a keyset and sign their own Talk Down /
AAC Voice Call grants. These tests prove a planted, foreign-owned,
user-writable or redirected keyset is never trusted, that the installer's
ACL is accepted, and that Linux is unchanged. Windows-only cases read real
ACLs and skip elsewhere; nothing here needs administrator rights.
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import appliance_entitlements
import windows_trust_anchor as w

ON_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="reads real Windows ACLs")

TRUSTED_FULL = [(0, 0x03, 0x001F01FF, w.SYSTEM_SID), (0, 0x03, 0x001F01FF, w.ADMINISTRATORS_SID)]
USERS = "S-1-5-32-545"
EVERYONE = "S-1-1-0"
READ_EXECUTE = 0x001200A9


# ------------------------------------------------------------------ the ACL rules
def test_the_installers_acl_is_accepted():
    assert w.acl_problem(w.ADMINISTRATORS_SID, TRUSTED_FULL) == ""
    assert w.acl_problem(w.SYSTEM_SID, TRUSTED_FULL + [(0, 0, READ_EXECUTE, USERS)]) == ""   # read-only users
    assert w.acl_problem(w.TRUSTED_INSTALLER_SID, TRUSTED_FULL) == ""


def test_a_foreign_owner_is_rejected():
    assert "is owned by S-1-5-21-1-2-3-1001" in w.acl_problem("S-1-5-21-1-2-3-1001", TRUSTED_FULL)


def test_a_null_dacl_is_rejected():
    assert "no access list" in w.acl_problem(w.ADMINISTRATORS_SID, None)


@pytest.mark.parametrize("right", [w.FILE_WRITE_DATA, w.FILE_APPEND_DATA, w.FILE_WRITE_EA, w.FILE_DELETE_CHILD,
                                   w.FILE_WRITE_ATTRIBUTES, w.DELETE, w.WRITE_DAC, w.WRITE_OWNER,
                                   w.GENERIC_WRITE, w.GENERIC_ALL, w.MAXIMUM_ALLOWED])
@pytest.mark.parametrize("sid", [USERS, EVERYONE, "S-1-5-11", "S-1-5-21-1-2-3-1001"])
def test_any_change_right_for_another_account_is_rejected(right, sid):
    problem = w.acl_problem(w.ADMINISTRATORS_SID, TRUSTED_FULL + [(0, 0, READ_EXECUTE | right, sid)])
    assert f"lets {sid} change it" in problem


def test_deny_and_inherit_only_entries_do_not_count_but_unknown_allow_entries_fail_closed():
    assert w.acl_problem(w.ADMINISTRATORS_SID, TRUSTED_FULL + [(1, 0, 0, "")]) == ""                      # deny
    assert w.acl_problem(w.ADMINISTRATORS_SID, TRUSTED_FULL + [(0, w.INHERIT_ONLY_ACE, 0x1F01FF, USERS)]) == ""
    assert "cannot be checked" in w.acl_problem(w.ADMINISTRATORS_SID, TRUSTED_FULL + [(5, 0, -1, "")])  # object ACE


def test_the_owner_rights_sid_is_the_trusted_owner():
    assert w.acl_problem(w.ADMINISTRATORS_SID, TRUSTED_FULL + [(0, 0, 0x1F01FF, w.OWNER_RIGHTS_SID)]) == ""


# ------------------------------------------------------------------ the folder chain
def _chain(root: Path) -> Path:
    keyset = root / "AnyAiCam" / "trust" / w.KEYSET_NAME
    keyset.parent.mkdir(parents=True)
    keyset.write_text('{"keys": {}}')
    return keyset


def test_every_level_of_the_chain_is_checked(tmp_path):
    keyset = _chain(tmp_path)
    seen = []

    def read_security(path):
        seen.append(Path(path))
        return w.ADMINISTRATORS_SID, TRUSTED_FULL

    assert w.anchor_problem(keyset, read_security=read_security) == ""
    assert seen == [keyset.parent.parent, keyset.parent, keyset]


def test_one_bad_level_rejects_the_keyset(tmp_path):
    keyset = _chain(tmp_path)
    for bad in (keyset.parent.parent, keyset.parent, keyset):
        def read_security(path, bad=bad):
            return ("S-1-5-21-1-2-3-1001", TRUSTED_FULL) if Path(path) == bad else (w.ADMINISTRATORS_SID, TRUSTED_FULL)
        assert w.anchor_problem(keyset, read_security=read_security).startswith(str(bad))


def test_missing_wrong_kind_and_unreadable_fail_closed(tmp_path):
    keyset = _chain(tmp_path)
    ok = lambda path: (w.ADMINISTRATORS_SID, TRUSTED_FULL)  # noqa: E731
    assert "is missing" in w.anchor_problem(tmp_path / "nowhere" / "trust" / w.KEYSET_NAME, read_security=ok)
    keyset.unlink(); keyset.mkdir()
    assert "is not a regular file" in w.anchor_problem(keyset, read_security=ok)
    keyset.rmdir(); keyset.write_text("{}")

    def unreadable(path):
        raise w.AnchorUncheckable("access denied")
    assert "cannot be checked" in w.anchor_problem(keyset, read_security=unreadable)


# ------------------------------------------------------------------ real Windows objects
@windows_only
def test_real_acls_system_files_pass_and_user_writable_folders_fail():
    assert w.entry_problem(Path(os.environ["SystemRoot"]) / "System32", folder=True) == ""
    assert w.entry_problem(Path(os.environ["SystemRoot"]) / "System32" / "kernel32.dll", folder=False) == ""
    assert "lets S-1-5-32-545 change it" in w.entry_problem(w.program_data(), folder=True)   # users may create folders


@windows_only
def test_the_real_location_comes_from_the_known_folder_not_the_environment(monkeypatch):
    real = w.anchor_file()
    monkeypatch.setenv("ProgramData", r"C:\Users\Public\fake")
    assert w.anchor_file() == real
    assert real.parts[-3:] == ("AnyAiCam", "trust", w.KEYSET_NAME)


@windows_only
def test_a_keyset_planted_by_a_standard_user_is_rejected(tmp_path):
    # Created by this (non-elevated) test user, like a planted C:\etc\... or a
    # folder made in ProgramData before the setup ran.
    assert "is owned by" in w.anchor_problem(_chain(tmp_path))


def _icacls(*args):
    subprocess.run(["icacls", *map(str, args)], check=True, capture_output=True)


@pytest.fixture()
def protected_chain(tmp_path, monkeypatch):
    """The installer's exact ACL (SYSTEM + Administrators, protected) on a
    real folder chain. Owner changes need elevation, so this test user stays
    the owner and is treated as trusted here; it can still read and, as the
    owner, restore the ACL for cleanup."""
    me = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout.strip().split(",")[-1].strip('"')
    monkeypatch.setattr(w, "TRUSTED_SIDS", w.TRUSTED_SIDS | {me})
    keyset = _chain(tmp_path)
    # Bottom-up: once a parent is protected, this non-elevated user could no
    # longer open what is below it to change it.
    for path, folder in ((keyset, False), (keyset.parent, True), (keyset.parent.parent, True)):
        # SYSTEM + Administrators as the installer sets them, plus this test
        # user (trusted here, standing in for the service account that reads
        # it; otherwise it could not even open the protected folders).
        grants = ([f"*{sid}:(OI)(CI)F" for sid in ("S-1-5-18", "S-1-5-32-544", me)] if folder
                  else [f"*{sid}:F" for sid in ("S-1-5-18", "S-1-5-32-544", me)])
        _icacls(path, "/inheritance:r", "/grant:r", *grants)
    yield keyset, me
    for path in (keyset.parent.parent, keyset.parent, keyset):  # top-down to restore
        subprocess.run(["icacls", str(path), "/grant", f"*{me}:(OI)(CI)F" if path.is_dir() else f"*{me}:F"], capture_output=True)


@windows_only
def test_the_installers_real_acl_is_accepted(protected_chain):
    keyset, _ = protected_chain
    assert w.anchor_problem(keyset) == ""


@windows_only
def test_a_users_write_grant_on_the_real_file_is_rejected(protected_chain):
    keyset, _ = protected_chain
    _icacls(keyset, "/grant", "*S-1-5-32-545:(W)")
    assert "lets S-1-5-32-545 change it" in w.anchor_problem(keyset)


@windows_only
def test_a_delete_or_permission_grant_on_the_real_folder_is_rejected(protected_chain):
    keyset, _ = protected_chain
    _icacls(keyset.parent, "/grant", "*S-1-5-32-545:(DE)")
    assert "lets S-1-5-32-545 change it" in w.anchor_problem(keyset)
    _icacls(keyset.parent, "/remove:g", "*S-1-5-32-545")
    _icacls(keyset.parent, "/grant", "*S-1-1-0:(WDAC)")
    assert "lets S-1-1-0 change it" in w.anchor_problem(keyset)


@windows_only
def test_a_redirected_trust_folder_is_rejected(tmp_path):
    keyset = _chain(tmp_path / "real")
    redirected = tmp_path / "AnyAiCam"
    redirected.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(redirected / "trust"), str(keyset.parent)], check=True, capture_output=True)
    problem = w.anchor_problem(redirected / "trust" / w.KEYSET_NAME, read_security=lambda path: (w.ADMINISTRATORS_SID, TRUSTED_FULL))
    assert "reparse point" in problem


# ------------------------------------------------------------------ appliance_entitlements wiring
def test_other_non_posix_platforms_fail_closed(monkeypatch, tmp_path):
    fake_os = types.SimpleNamespace(name="java", lstat=os.lstat, environ=os.environ)
    monkeypatch.setattr(appliance_entitlements, "os", fake_os)
    assert "cannot be checked on this platform" in appliance_entitlements._anchor_file_problem(tmp_path / "keys.json")


@windows_only
def test_windows_uses_the_windows_check_and_location():
    assert appliance_entitlements._anchor_file_problem.__code__.co_names.count("anchor_problem") >= 1
    import importlib
    fresh = importlib.reload(appliance_entitlements)
    try:
        assert fresh.TRUST_ANCHOR_FILE == w.anchor_file()
    finally:
        importlib.reload(appliance_entitlements)


def test_the_linux_anchor_and_check_are_unchanged():
    import inspect
    source = inspect.getsource(appliance_entitlements)
    assert 'TRUST_ANCHOR_FILE = Path("/etc/anyaicam-update/entitlement_signing_keys.json")' in source
    assert 'if os.name == "nt":' in source
    assert "for candidate in (path.parent, path):" in source  # the POSIX checks themselves
    assert "import windows_trust_anchor" not in source.split('if os.name == "nt":')[0]  # only under nt


# ------------------------------------------------------------------ forged grants end to end
IDENTITY = {"appliance_id": "appl-win", "cloud_id": "AIC-WIN", "customer_id": "cust-win"}


def _forged_snapshot():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import appliance_identity
    private = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(private.private_bytes_raw()).decode()
    public_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    now = datetime.now(timezone.utc)
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
            "features": {"talk_down": True, "voice_call": True},
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}
    return public_b64, {**body, "signature": appliance_identity.sign_body(body, key_id="attacker", private_key_b64=private_b64)}


@windows_only
@pytest.mark.real_entitlements
def test_a_planted_keyset_cannot_enable_paid_features(monkeypatch):
    public_b64, snapshot = _forged_snapshot()
    anchor = appliance_entitlements.TRUST_ANCHOR_FILE  # a temp path the test user owns (conftest)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(json.dumps({"keys": {"attacker": public_b64}}), encoding="utf-8")
    appliance_entitlements.STATE_FILE.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: dict(IDENTITY))
    assert appliance_entitlements._trusted_keys() == {}
    assert appliance_entitlements.feature_allowed("talk_down") is False
    assert appliance_entitlements.feature_allowed("voice_call") is False
    # Control: only the trust-anchor check stops it -- the same forged files
    # would grant both features if the anchor were (wrongly) accepted.
    monkeypatch.setattr(appliance_entitlements, "_anchor_file_problem", lambda path: "")
    assert appliance_entitlements.feature_allowed("talk_down") is True
