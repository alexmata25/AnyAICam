"""First-install hardening (owner 2026-10-18): a standard user can pre-create %ProgramData%\\AnyAiCam before the first
install and leave user-writable entries anywhere in it -- recordings, hls, logs, mediamtx, not only the secret folders --
then later plant a junction for the SYSTEM service to write through. install-runtime.ps1 must decide BEFORE touching
the root whether it was already a genuine (admin-owned, protected, nobody-else-may-write) data root, and otherwise
repair the whole tree with the link-refusing per-entry walk.

The ACL policy is tested on in-memory security descriptors (no administrator rights needed); the file-system tests use
real folders and real junctions (mklink /J)."""
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

SYSTEM, ADMINS, USERS, AUTH_USERS = "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545", "S-1-5-11"


def _ps(body: str):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", f". '{GUARD}'\n{body}"],
                          capture_output=True, text=True, timeout=120)


def _acl_script(owner: str, protected: bool, rules: list[tuple[str, str, str]]) -> str:
    """PowerShell that builds an in-memory DirectorySecurity and prints Clean/Trusted."""
    lines = [
        "$acl = New-Object System.Security.AccessControl.DirectorySecurity",
        f"$acl.SetOwner((New-Object Security.Principal.SecurityIdentifier '{owner}'))",
        f"$acl.SetAccessRuleProtection(${'true' if protected else 'false'}, $false)",
    ]
    for sid, rights, kind in rules:
        lines.append(
            "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("
            f"(New-Object Security.Principal.SecurityIdentifier '{sid}'), "
            f"[System.Security.AccessControl.FileSystemRights]'{rights}', 'ContainerInherit,ObjectInherit', 'None', '{kind}')))")
    lines.append("'clean=' + (Test-AnyAiCamAclClean $acl) + ' trusted=' + (Test-AnyAiCamAclTrusted $acl)")
    return "\n".join(lines)


GENUINE = [(SYSTEM, "FullControl", "Allow"), (ADMINS, "FullControl", "Allow")]


@windows_only
@pytest.mark.parametrize("owner,protected,rules,expected", [
    (ADMINS, True, GENUINE, "clean=True trusted=True"),
    (SYSTEM, True, GENUINE + [(USERS, "ReadAndExecute", "Allow")], "clean=True trusted=True"),  # read-only is fine
    (ADMINS, False, GENUINE, "clean=True trusted=False"),                 # a root must be protected
    (USERS, True, GENUINE, "clean=False trusted=False"),                  # foreign owner can rewrite the ACL
    (ADMINS, True, GENUINE + [(USERS, "Modify", "Allow")], "clean=False trusted=False"),
    (ADMINS, True, GENUINE + [(AUTH_USERS, "Write", "Allow")], "clean=False trusted=False"),
    (ADMINS, True, GENUINE + [(USERS, "AppendData", "Allow")], "clean=False trusted=False"),   # create sub-folders
    (ADMINS, True, GENUINE + [(USERS, "Write", "Deny")], "clean=True trusted=True"),           # deny never widens
])
def test_the_acl_policy(owner, protected, rules, expected):
    result = _ps(_acl_script(owner, protected, rules))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == expected


@windows_only
def test_a_folder_a_standard_user_created_is_not_a_trusted_root(tmp_path):
    """tmp_path is created and owned by the (non-elevated) test user -- exactly what a pre-planted root looks like."""
    planted = tmp_path / "AnyAiCam"
    (planted / "recordings").mkdir(parents=True)
    result = _ps(f"Test-AnyAiCamDataRootTrusted '{planted}'")
    assert result.returncode == 0 and result.stdout.strip() == "False"


@windows_only
def test_a_junction_or_a_missing_root_is_never_trusted(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "AnyAiCam"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    assert _ps(f"Test-AnyAiCamDataRootTrusted '{link}'").stdout.strip() == "False"
    assert _ps(f"Test-AnyAiCamDataRootTrusted '{tmp_path / 'absent'}'").stdout.strip() == "False"


@windows_only
def test_the_whole_tree_repair_refuses_a_planted_recordings_junction_before_any_acl_change(tmp_path):
    """A junction named like one of the non-secret folders: the walk must stop on it, never hand it to icacls and never
    touch what it points at."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "system.dat").write_text("do not touch", encoding="utf-8")
    root = tmp_path / "AnyAiCam"
    root.mkdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "recordings"), str(outside)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    result = _ps(f"Set-AnyAiCamDescendantAcl '{root}'")
    assert result.returncode != 0
    assert "Refusing to repair permissions through link" in result.stderr and "recordings" in result.stderr
    assert (outside / "system.dat").read_text(encoding="utf-8") == "do not touch"


def _runtime() -> str:
    return (WIN / "install-runtime.ps1").read_text(encoding="utf-8-sig")


def test_the_trust_decision_is_taken_before_the_root_is_created_or_changed():
    text = _runtime()
    decide = text.index("$rootWasTrusted = Test-AnyAiCamDataRootTrusted $DataRoot")
    assert decide < text.index("New-Item -ItemType Directory -Path $DataRoot -Force")
    assert decide < text.index("& icacls.exe $DataRoot /setowner '*S-1-5-32-544' /C /Q")
    assert decide < text.index("& icacls.exe $DataRoot /inheritance:r /grant:r")


def test_an_untrusted_root_repairs_the_whole_tree_including_recordings_hls_logs_and_mediamtx():
    text = _runtime()
    whole = text.index("if (-not $repairedLegacyTree -and -not $rootWasTrusted) {")
    block = text[whole:text.index("}", whole)]
    assert "Set-AnyAiCamDescendantAcl $DataRoot" in block  # every entry under the root, not a folder list
    # ...and only after the root is restricted and checked for links, and before the final link check
    assert text.index("& icacls.exe $DataRoot /inheritance:r /grant:r") < whole
    assert whole < text.rindex("Assert-AnyAiCamDataRootHasNoLinks $DataRoot")
    # the standard folders it must cover are created before it runs
    for folder in ("recordings", "hls", "logs", "mediamtx"):
        assert f"'{folder}'" in text[:whole]


def test_the_entry_repair_only_skips_icacls_for_already_clean_entries_and_still_refuses_links():
    guard = GUARD.read_text(encoding="utf-8-sig")
    entry = guard[guard.index("function Set-AnyAiCamEntryAcl"):guard.index("function Set-AnyAiCamDescendantAcl")]
    assert entry.index("ReparsePoint") < entry.index("Test-AnyAiCamAclClean")  # links refused first
    assert "if (-not $clean) {" in entry and "/setowner '*S-1-5-32-544'" in entry and "/reset" in entry
    assert "EnumerateFileSystemInfos()" in entry  # always descends, clean or not
    assert "catch { $clean = $false }" in entry  # an unreadable ACL is repaired, never trusted


# Codex RC6 review: an adversarial case for EACH non-secret data subtree, not only recordings.
SUBTREES = ("recordings", "hls", "logs", "mediamtx")


@windows_only
@pytest.mark.parametrize("subtree", SUBTREES)
def test_a_junction_planted_as_any_data_subtree_is_refused_before_any_acl_change(tmp_path, subtree):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "system.dat").write_text("do not touch", encoding="utf-8")
    root = tmp_path / "AnyAiCam"
    root.mkdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / subtree), str(outside)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    result = _ps(f"Set-AnyAiCamDescendantAcl '{root}'")
    assert result.returncode != 0 and "Refusing to repair permissions through link" in result.stderr and subtree in result.stderr
    assert (outside / "system.dat").read_text(encoding="utf-8") == "do not touch"


@windows_only
@pytest.mark.parametrize("subtree", SUBTREES)
def test_a_junction_planted_deep_inside_any_data_subtree_is_found_without_traversal(tmp_path, subtree):
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    root = tmp_path / "AnyAiCam"
    nested = root / subtree / "camera1" / "2026-10-18"
    nested.mkdir(parents=True)
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(nested / "planted"), str(outside)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    found = _ps(f"Get-AnyAiCamDataRootLink '{root}'")
    assert found.returncode == 0 and found.stdout.strip() == str(nested / "planted")
    refused = _ps(f"Assert-AnyAiCamDataRootHasNoLinks '{root}'")
    assert refused.returncode != 0 and "is a link" in refused.stderr
