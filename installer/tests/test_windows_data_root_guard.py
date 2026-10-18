"""Codex 1.2.7 Windows review (Windows-only blocker): a standard user may pre-create %ProgramData%\\AnyAiCam and plant a
junction or symbolic link before the elevated installer runs recursive takeown /R and icacls /T over it. The guard
(installer/windows/data-root-guard.ps1) must find any link WITHOUT following it, and install-runtime.ps1 must restrict
the root and refuse links before any recursive operation. Runs the real guard in Windows PowerShell against real
junctions (mklink /J needs no administrator rights)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
GUARD = WIN / "data-root-guard.ps1"
POWERSHELL = shutil.which("powershell")
windows_only = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="needs Windows PowerShell")


def _ps(body: str):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", f". '{GUARD}'\n{body}"],
                          capture_output=True, text=True, timeout=120)


def _junction(link: Path, target: Path) -> None:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@windows_only
def test_a_tree_without_links_is_accepted(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "vms.env").write_text("X=1\n", encoding="utf-8")
    (tmp_path / "recordings" / "camera1").mkdir(parents=True)
    result = _ps(f"Get-AnyAiCamDataRootLink '{tmp_path}'; Assert-AnyAiCamDataRootHasNoLinks '{tmp_path}'; 'ok'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok"


@windows_only
def test_a_planted_junction_is_found_and_never_traversed(tmp_path):
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "victim.txt").write_text("system file", encoding="utf-8")
    root = tmp_path / "AnyAiCam"
    (root / "recordings").mkdir(parents=True)
    _junction(root / "recordings" / "planted", outside)
    found = _ps(f"Get-AnyAiCamDataRootLink '{root}'")
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip() == str(root / "recordings" / "planted")  # the link itself, not anything behind it
    refused = _ps(f"Assert-AnyAiCamDataRootHasNoLinks '{root}'")
    assert refused.returncode != 0 and "is a link" in refused.stderr
    assert (outside / "deep" / "victim.txt").read_text(encoding="utf-8") == "system file"


@windows_only
def test_the_data_root_itself_being_a_junction_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _junction(tmp_path / "AnyAiCam", real)
    result = _ps(f"Get-AnyAiCamDataRootLink '{tmp_path / 'AnyAiCam'}'")
    assert result.stdout.strip() == str(tmp_path / "AnyAiCam")


@windows_only
def test_a_missing_data_root_is_not_an_error(tmp_path):
    result = _ps(f"Assert-AnyAiCamDataRootHasNoLinks '{tmp_path / 'absent'}'; 'ok'")
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def _runtime() -> str:
    return (WIN / "install-runtime.ps1").read_text(encoding="utf-8-sig")


def test_install_runtime_restricts_the_root_and_refuses_links_before_any_recursive_operation():
    text = _runtime()
    load = text.index(". (Join-Path $PSScriptRoot 'data-root-guard.ps1')")
    first_check = text.index("Assert-AnyAiCamDataRootHasNoLinks $DataRoot")
    create_root = text.index("New-Item -ItemType Directory -Path $DataRoot -Force")
    owner = text.index("& icacls.exe $DataRoot /setowner '*S-1-5-32-544' /C /Q")
    restrict = text.index("& icacls.exe $DataRoot /inheritance:r /grant:r")
    make_children = text.index("foreach ($directory in @('config', 'database', 'recordings', 'hls', 'logs', 'mediamtx'))")
    reset_tree = text.index("Set-AnyAiCamDescendantAcl $DataRoot")
    secret = text.index("foreach ($secretFolder in @('config', 'database', 'agent'))")
    assert load < first_check < create_root < owner < restrict < make_children < reset_tree < secret
    # No child under DataRoot is created until the root is owned/restricted and the tree scan passes.
    assert make_children < text.index("$environmentFile = Join-Path $DataRoot 'config\\vms.env'")
    # Repairs use the checked, one-entry-at-a-time walker instead of recursive ACL tools.
    assert "takeown.exe" not in text and "/reset /T" not in text and "/setowner '*S-1-5-32-544' /T" not in text
    helper = (WIN / "data-root-guard.ps1").read_text(encoding="utf-8-sig")
    link_check = helper.index("if ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint)")
    set_owner = helper.index("& icacls.exe $Entry.FullName /setowner")
    reset_entry = helper.index("& icacls.exe $Entry.FullName /reset")
    enumerate_children = helper.index("$Entry.EnumerateFileSystemInfos()")
    assert link_check < set_owner < reset_entry < enumerate_children
    # and a second check once the secret folders are Administrators-only
    assert text.count("Assert-AnyAiCamDataRootHasNoLinks $DataRoot") == 4
    assert text.rindex("Assert-AnyAiCamDataRootHasNoLinks $DataRoot") > secret


def test_the_setup_ships_the_guard_beside_install_runtime():
    iss = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8-sig")
    assert 'Source: "data-root-guard.ps1"; DestDir: "{app}' + chr(92) + 'installer"; Flags: ignoreversion' in iss
