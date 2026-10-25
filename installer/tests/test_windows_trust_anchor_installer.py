"""Requirement 9 (1.2.7), Windows installer step 2: the entitlement keyset reaches a protected trust folder.

The keyset (cloud entitlement-signing PUBLIC keys) is a required, verified build input (build.ps1 ->
prepare-keyset.py, the same validator as the Linux release build), ships in Setup, and install-runtime.ps1 installs it
into %ProgramData%\\AnyAiCam\\update owned by Administrators with SYSTEM/Administrators-only access -- what
app/platform_paths.windows_write_problem() requires before the VMS trusts it. The permission steps themselves need
Administrator rights and are exercised in the Windows Sandbox validation; here the helper runs for real and the
scripts are checked for the required steps in the right order."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
HELPER = WIN / "prepare-keyset.py"
KEY = base64.b64encode(bytes(range(32))).decode()


def _canonical(keys):
    return (json.dumps({"keys": dict(sorted(keys.items()))}, sort_keys=True, indent=2) + "\n").encode("ascii")


def _run(tmp_path, document: bytes, sha: str):
    source, output = tmp_path / "in.json", tmp_path / "out" / "keys.json"
    source.write_bytes(document)
    result = subprocess.run([sys.executable, str(HELPER), str(source), sha, str(output)], capture_output=True, text=True)
    return result, output


def test_a_pinned_public_keyset_is_written_in_canonical_form(tmp_path):
    canonical = _canonical({"k-2026": KEY})
    result, output = _run(tmp_path, json.dumps({"keys": {"k-2026": KEY}}).encode(), hashlib.sha256(canonical).hexdigest())
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == canonical


def test_a_keyset_that_does_not_match_its_pin_is_refused(tmp_path):
    result, output = _run(tmp_path, json.dumps({"keys": {"k-2026": KEY}}).encode(), "0" * 64)
    assert result.returncode != 0 and "refusing to package an unverified trust anchor" in (result.stderr + result.stdout)
    assert not output.exists()


@pytest.mark.parametrize("document", [
    b'{"keys": {"k": "-----BEGIN PRIVATE KEY-----"}}',
    b'{"keys": {}}',
    b'{"keys": {"k": "c2hvcnQ="}}',
    b'{"keys": {"k": "' + KEY.encode() + b'"}, "extra": 1}',
])
def test_private_malformed_or_empty_keysets_are_refused(tmp_path, document):
    result, output = _run(tmp_path, document, "a" * 64)
    assert result.returncode != 0 and not output.exists()


def _text(name):
    return (WIN / name).read_text(encoding="utf-8-sig")


def test_build_requires_the_keyset_and_its_pin_and_hands_both_to_setup():
    build = _text("build.ps1")
    assert "[Parameter(Mandatory=$true)][string]$EntitlementKeyset," in build
    assert "[Parameter(Mandatory=$true)][string]$EntitlementKeysetSha256," in build
    assert "prepare-keyset.py" in build and '/DKeysetSha256=$EntitlementKeysetSha256' in build
    assert "[string]$PythonExe = ''" in build
    assert "& $PythonExe (Join-Path $PSScriptRoot 'prepare-keyset.py')" in build
    assert "Python 3.12.10" in build
    assert build.index("prepare-keyset.py") < build.index("& $iscc")
    iss = _text("AnyAiCam-VMS.iss")
    assert "#error KeysetSha256 is required" in iss
    assert 'Source: "output\\entitlement-signing-public-keys.json"; DestDir: "{tmp}"' in iss
    assert '-EntitlementKeysetSha256 "{#KeysetSha256}"' in iss


def test_install_creates_a_protected_trust_folder_and_verifies_before_writing():
    script = _text("install-runtime.ps1")
    block = script[script.index("$updateRoot = Join-Path $DataRoot 'update'"):script.index("Entitlement keyset installed")]
    order = ["Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $updateRoot -Force)",
             "Contains('PRIVATE')", "Get-FileHash -Algorithm SHA256 -LiteralPath $EntitlementKeyset",
             "WriteAllBytes($keysetTemp", "Move-Item -LiteralPath $keysetTemp", "$keysetTarget /setowner '*S-1-5-32-544'",
             "$keysetTarget /reset"]
    positions = [block.index(step) for step in order]
    assert positions == sorted(positions), "trust-folder steps out of order"
    assert "entitlement_signing_keys.json" in block
    guard = (ROOT / "installer" / "windows" / "data-root-guard.ps1").read_text(encoding="utf-8-sig")
    assert guard.count("if ($LASTEXITCODE -ne 0) { throw") >= 2  # entry ACL failures stop setup


def test_the_vms_reads_the_keyset_from_the_folder_the_installer_writes():
    sys.path.insert(0, str(ROOT / "app"))
    platform_paths = __import__("platform_paths")
    script = _text("install-runtime.ps1")
    assert "Join-Path $DataRoot 'update'" in script and "'entitlement_signing_keys.json'" in script
    iss = _text("AnyAiCam-VMS.iss")
    assert "-DataRoot \"' + ExpandConstant('{commonappdata}\\AnyAiCam')" in iss   # DataRoot = %ProgramData%\AnyAiCam
    if platform_paths.WINDOWS:
        assert str(platform_paths.UPDATE_DIR).lower().endswith(r"anyaicam\update")


def test_windows_never_turns_the_user_writable_1_3_capacity_into_a_trusted_fallback():
    """Step 3 (req 9): 1.3.x kept unsigned capacity under C:\opt (user-modifiable): no migration, by design."""
    script = _text("install-runtime.ps1")
    block = script[script.index("Camera capacity fallback (Linux installer step 15)"):]
    block = block[:block.index("$values = [ordered]@{}")]
    assert "vms_capacity_fallback.json" not in block and "migrate_capacity_fallback" not in block
    assert "is not carried over" in block and "first signed capacity" in block
    assert "Join-Path $env:SystemDrive 'opt\\anyaicam\\data\\config\\vms_capacity.json'" in block
