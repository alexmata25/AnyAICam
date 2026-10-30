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
    assert helper.index("$process.WaitForExit(30000)") < helper.index("$process.WaitForExit()")
    assert "if ($process.ExitCode -ne 0)" in helper


def test_the_port_check_waits_and_cross_checks_netstat_but_still_requires_all_interfaces():
    block = SCRIPT.split("Check 'listening on the local network (0.0.0.0:8000)' {", 1)[1].split("\n}\n", 1)[0]
    assert "foreach ($try in 1..30)" in block and "Start-Sleep 2" in block
    assert "Get-NetTCPConnection -LocalPort 8000 -State Listen" in block and "netstat -ano -p tcp" in block
    assert "'^TCP 0\\.0\\.0\\.0:8000 '" in block  # a loopback-only listener is still a failure
    assert 'throw "listening on' in block and "nothing listens on port 8000" in block


def test_smart_app_control_evidence_is_exported_before_the_summary():
    # Owner 2026-10-29: capture exact Smart App Control evidence. The Sandbox runs SAC in evaluation mode (state 2), so
    # Code Integrity audit events name every file that enforced SAC would block.
    export = SCRIPT.split("function Export-CodeIntegrityEvidence {", 1)[1].split("\nfunction ", 1)[0]
    assert "Microsoft-Windows-CodeIntegrity/Operational" in export
    for event_id in ("3076", "3077", "3091", "3092"):
        assert event_id in export
    assert "codeintegrity-events.txt" in export and "codeintegrity-files.txt" in export
    summary = SCRIPT.split("function WriteAcceptanceSummary {", 1)[1]
    assert summary.index("Export-CodeIntegrityEvidence") < summary.index("$failed = ")
    import re
    message = (r"Code Integrity determined that a process (\Device\HarddiskVolume3\Program Files\AnyAiCam\runtime\python\python.exe) "
               r"attempted to load \Device\HarddiskVolume3\Program Files\AnyAiCam\runtime\python\Lib\site-packages\cv2\cv2.pyd "
               r"that did not meet the Enterprise signing level requirements.")
    pattern = re.search(r"-match '([^']+)'", export.split("$files = ", 1)[1]).group(1).replace("(?is)", "")
    assert re.search(pattern, message, re.I | re.S).group(1).endswith("cv2.pyd")


MAKE_WSB = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "make-wsb.ps1").read_text(encoding="utf-8-sig")
ENFORCE = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "enforce-sac.ps1").read_text(encoding="utf-8-sig")


def test_enforced_smart_app_control_runs_turn_it_on_inside_the_sandbox_first():
    # Since 2026-10-23 the Dell's Sandbox starts in SAC evaluation mode (state 2), where nothing is blocked.
    assert "[switch]$EnforceSmartAppControl" in MAKE_WSB
    assert "enforce_smart_app_control = $true" in MAKE_WSB and "'enforce-sac.ps1'" in MAKE_WSB
    command = MAKE_WSB.split("<Command>", 1)[1].split("</Command>", 1)[0]
    assert command.index("enforce-sac.ps1") < command.index("validate-in-sandbox.ps1")
    assert "-Value 1 -Type DWord" in ENFORCE and "CiTool.exe') --refresh" in ENFORCE and "SAC-STATE" in ENFORCE
    check = SCRIPT.split("if ($acceptanceSettings.enforce_smart_app_control) {", 1)[1].split("\n}\n", 1)[0]
    assert "if ($state -ne 1) { throw" in check and "not enforced-SAC evidence" in check


def test_without_the_switch_the_sandbox_command_is_unchanged():
    command = MAKE_WSB.split("<Command>", 1)[1].split("</Command>", 1)[0]
    assert "$(if ($EnforceSmartAppControl)" in command
    assert r"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Validation\scripts\$(if ($Functional)" in command
