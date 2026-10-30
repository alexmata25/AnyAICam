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


FUNCTIONAL = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "functional-in-sandbox.ps1").read_text(encoding="utf-8-sig")


def test_the_vms_process_helper_is_defined_before_the_1341_gate_checks_use_it():
    # F-L on 46713ee: the LocalSystem Tesseract/SAPI (A1/A3) and Job Object (A4) checks called VmsProcess before the
    # script had reached its definition ("The term 'VmsProcess' is not recognized").
    definition = FUNCTIONAL.index("function VmsProcess {")
    first_use = FUNCTIONAL.index("VmsProcess |")
    assert definition < first_use
    assert FUNCTIONAL.count("function VmsProcess {") == 1


def test_motion_freshness_is_sampled_right_after_motion_with_the_unchanged_10_second_rule():
    # F-L on 46713ee: 224270c had moved PlaylistFresh 'motion' behind the WinSW stop/start test (age 25.5 s).
    motion = FUNCTIONAL.index("Check 'motion (basic, default ON): the moving box produces motion events'")
    fresh = FUNCTIONAL.index("PlaylistFresh 'motion'")
    clip = FUNCTIONAL.index("Check 'recording: a motion event produces a finished playable event clip'")
    shutdown = FUNCTIONAL.index("Check 'shutdown: WinSW stop during delayed event finalization")
    assert motion < fresh < clip < shutdown
    assert FUNCTIONAL.count("PlaylistFresh 'motion'") == 1
    assert "if ($a -ge 10) { throw \"age $a s\" }" in FUNCTIONAL  # criterion untouched


def test_live_view_must_resume_on_its_own_after_the_service_restart():
    shutdown = FUNCTIONAL.index("Check 'shutdown: WinSW stop during delayed event finalization")
    resume = FUNCTIONAL.index("Check 'live view resumes after the service restart")
    assert shutdown < resume
    block = FUNCTIONAL[resume:FUNCTIONAL.index("MotionReaderRtsp 'motion'", resume)]
    assert "$started.AddSeconds(60)" in block and "$written -gt $started" in block  # only a write by the NEW VMS counts
    assert "if ($age -ge 10)" in block


def test_moq_check_never_passes_vacuously_on_an_empty_socket_table():
    moq = FUNCTIONAL.split("function MoqCheck([string]$Label) {", 1)[1].split("\n}\n", 1)[0]
    blind = moq.split("(SocketTableBlind)", 1)[1].split("return ", 1)[0]
    for probe in ("TcpAccepts 8889", "TcpAccepts 8892", "UdpPortFree $port", "UdpPortFree 8189"):
        assert probe in blind, probe
    assert "foreach ($port in 8892, 8893)" in blind and "throw ($problems -join '; ')" in blind
    helpers = FUNCTIONAL.split("function SocketTableBlind {", 1)[1].split("function MoqCheck", 1)[0]
    assert "ExclusiveAddressUse = $true" in helpers and "$cim.Count -eq 0 -and $net.Count -eq 0" in helpers


def test_port_8000_is_not_tested_never_passed_when_the_socket_table_is_empty():
    head = SCRIPT.split("$tableListeners = ", 1)[1].split("Check 'listening on the local network (0.0.0.0:8000)' {", 1)[0]
    assert "if ($tableListeners -eq 0) {" in head and "NotTested 'listening on the local network (0.0.0.0:8000)'" in head
    assert "Check '" not in head
