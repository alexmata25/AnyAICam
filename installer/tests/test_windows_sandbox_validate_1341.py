"""1.3.4.1 Sandbox acceptance (2026-10-29, P-L on 46713ee): validate-in-sandbox.ps1 read the setup version from the
file name with a three-part pattern only, so the four-part 1.3.4.1 setup failed "setup identity" before any real check.
The setup version has three or four parts (the downgrade guard and make-wsb.ps1 accept both)."""
import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "validate-in-sandbox.ps1").read_text(encoding="utf-8-sig")


def _version_pattern() -> str:
    line = next(l for l in SCRIPT.splitlines() if l.startswith("$expectedVersion = "))
    return re.search(r"-match '([^']+)'", line).group(1)


def test_three_and_four_part_setup_versions_are_read_from_the_setup_name():
    pattern = _version_pattern()
    for name, version in (("AnyAiCam-VMS-Setup-1.3.3-b8c7a7e.exe", "1.3.3"),
                          ("AnyAiCam-VMS-Setup-1.3.4.1-46713ee.exe", "1.3.4.1")):
        match = re.search(pattern, name)
        assert match and match.group(1) == version, name


def test_a_malformed_setup_name_still_yields_no_version():
    assert not re.search(_version_pattern(), "AnyAiCam-VMS-Setup-latest-46713ee.exe")


HARNESS = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "acceptance-127.ps1").read_text(encoding="utf-8-sig")


def test_the_python_helper_reads_a_real_exit_code_under_windows_powershell_5_1():
    # P-L on 46713ee: "python exit :" with correct output on every capacity/mode check. PowerShell 5.1 leaves ExitCode
    # empty for a -PassThru process whose handle was never opened (reproduced on the Dell: '' vs 3).
    helper = HARNESS.split("function AppPython([string]$Code) {", 1)[1].split("\nfunction ", 1)[0]
    start = helper.index("Start-Process -FilePath $py")
    assert start < helper.index("$null = $process.Handle") < helper.index("$process.WaitForExit(30000)")
    assert helper.index("$process.WaitForExit(30000)") < helper.index("$process.WaitForExit()\n".replace("\n", "")) or "$process.WaitForExit()" in helper
    assert "if ($process.ExitCode -ne 0)" in helper


def test_the_port_check_waits_for_the_listener_instead_of_sampling_once():
    line = next(l for l in SCRIPT.splitlines() if l.startswith("Check 'listening on the local network (0.0.0.0:8000)'"))
    assert "foreach ($try in 1..30)" in line and "Start-Sleep 2" in line and "'0.0.0.0'" in line
