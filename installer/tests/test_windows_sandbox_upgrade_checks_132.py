"""1.3.2 Sandbox acceptance (2026-10-21, U131-L on 17cf93c): the upgrade checks follow what each earlier release left.
1.3.0 created a permanent claim code (retired by the upgrade); 1.3.1 already deleted it, so a 1.3.1 -> 1.3.2 upgrade
is instead checked to start with no claim code and to keep the appliance identity 1.3.1 created."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"


def _validator() -> str:
    return (WIN / "sandbox" / "validate-in-sandbox.ps1").read_text(encoding="utf-8-sig")


def test_only_a_1_3_0_predecessor_must_have_created_a_permanent_claim_code():
    script = _validator()
    gate = script.index("if ($previous.Name -like 'AnyAiCam-VMS-Setup-1.3.0-*') {")
    assert gate < script.index("Check 'previous release created its permanent claim code'") < script.index("} else {", gate)


def test_a_later_predecessor_has_no_claim_code_and_its_identity_is_kept():
    script = _validator()
    later = script.split("if ($previous.Name -like 'AnyAiCam-VMS-Setup-1.3.0-*') {", 1)[1].split("} else {", 1)[1]
    assert "if (Test-Path -LiteralPath $oldLabel) { throw 'claim-label.txt present' }" in later
    assert "$script:previousIdentity = $id" in later
    assert 'Check "upgrade keeps the $previousRelease appliance identity"' in script
    assert script.index("if ($previousIdentity) {") > script.index("$script:previousIdentity = $id")


def test_1_3_1_itself_deletes_the_permanent_claim_code():
    # the evidence for the gate: 1.3.1 (7c66928) cloud-link.ps1 removes claim-label.txt and label_claim.json
    try:
        source = subprocess.run(["git", "-C", str(ROOT), "show", "7c66928:installer/windows/cloud-link.ps1"],
                                capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        import pytest
        pytest.skip("1.3.1 source (7c66928) not in this checkout")
    assert "foreach ($retired in @((Join-Path $labelDir 'claim-label.txt'), (Join-Path $config 'label_claim.json'))) {" in source


def test_the_release_name_is_taken_from_the_setup_file_name():
    command = "'AnyAiCam-VMS-Setup-1.3.1-7c66928.exe' -replace '^AnyAiCam-VMS-Setup-([0-9.]+)-.*$', '$1'"
    assert "$script:previousRelease = ($previous.Name -replace '^AnyAiCam-VMS-Setup-([0-9.]+)-.*$', '$1')" in _validator()
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    except OSError:
        return
    assert out == "1.3.1"
