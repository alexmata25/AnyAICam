"""Windows 1.3.4 CPU safeguard (Blue, 2026-10-27): one Job Object around the VMS service.

* Packaging: the .iss ships vms-job-limits.ps1 next to the launcher; the launcher applies it after the stale-VMS check
  and before Python starts, inside try/catch (fail-open: the VMS must start and record even if Windows refuses).
* Settings (vms.env): CPU cap default 80 %, 0/100 = off, out-of-range -> default; memory limit default off, at least
  1024 MB when set.
* Behaviour, EXECUTED with real PowerShell and the real Windows job API: the limits Windows reports back are the ones
  asked for, and a process started afterwards (as Python is) is inside the same job.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
SCRIPT = WIN / "vms-job-limits.ps1"
LAUNCHER = (WIN / "service-launcher.ps1").read_text(encoding="utf-8")
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _ps(body: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="aac-job-") as folder:
        script = Path(folder) / "t.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n. '" + str(SCRIPT).replace("'", "''") + "'\n" + body, encoding="utf-8")
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True, timeout=120)


class PackagingTests(unittest.TestCase):
    def test_setup_ships_it_next_to_the_launcher(self):
        self.assertIn('Source: "vms-job-limits.ps1"; DestDir: "{app}\\service"; Flags: ignoreversion', ISS)

    def test_the_launcher_applies_it_fail_open_before_python_starts(self):
        line = next(l for l in LAUNCHER.splitlines() if "vms-job-limits.ps1" in l and l.startswith("try {"))
        self.assertTrue(line.startswith("try { . (Join-Path $PSScriptRoot 'vms-job-limits.ps1'); Write-Output (Set-AnyAiCamVmsJobLimits $settings) } catch {"))
        self.assertLess(LAUNCHER.index("stop-stale-vms.ps1"), LAUNCHER.index("vms-job-limits.ps1"))
        self.assertLess(LAUNCHER.index("vms-job-limits.ps1"), LAUNCHER.index("-m uvicorn main:app"))


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class SettingsTests(unittest.TestCase):
    CASES = [
        ({}, (80, 0)),
        ({"ANYAICAM_VMS_CPU_LIMIT_PERCENT": "50"}, (50, 0)),
        ({"ANYAICAM_VMS_CPU_LIMIT_PERCENT": "0"}, (0, 0)),
        ({"ANYAICAM_VMS_CPU_LIMIT_PERCENT": "100"}, (0, 0)),
        ({"ANYAICAM_VMS_CPU_LIMIT_PERCENT": "5"}, (80, 0)),
        ({"ANYAICAM_VMS_CPU_LIMIT_PERCENT": "abc"}, (80, 0)),
        ({"ANYAICAM_VMS_MEMORY_LIMIT_MB": "4096"}, (80, 4096)),
        ({"ANYAICAM_VMS_MEMORY_LIMIT_MB": "200"}, (80, 1024)),
    ]

    def test_vms_env_values(self):
        body = ""
        for settings, _ in self.CASES:
            table = "@{" + "; ".join(f"'{k}' = '{v}'" for k, v in settings.items()) + "}"
            body += f"$l = Get-AnyAiCamVmsJobSettings {table}; Write-Output \"$($l.CpuPercent),$($l.MemoryMB)\"\n"
        result = _ps(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = [tuple(int(v) for v in line.split(",")) for line in result.stdout.split()]
        self.assertEqual(got, [expected for _, expected in self.CASES])

    def test_off_means_no_job(self):
        result = _ps("Write-Output (Set-AnyAiCamVmsJobLimits @{ 'ANYAICAM_VMS_CPU_LIMIT_PERCENT' = '0' })\n"
                     "Write-Output \"job=$($null -eq $script:AnyAiCamVmsJob)\"\n")
        self.assertIn("resource limits off", result.stdout)
        self.assertIn("job=True", result.stdout)


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class JobExecutionTests(unittest.TestCase):
    def test_windows_carries_the_limits_and_a_later_child_is_inside_the_job(self):
        body = r"""
$line = Set-AnyAiCamVmsJobLimits @{ 'ANYAICAM_VMS_CPU_LIMIT_PERCENT' = '50'; 'ANYAICAM_VMS_MEMORY_LIMIT_MB' = '4096' }
Write-Output $line
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class JobProbe { [DllImport("kernel32.dll", SetLastError=true)] public static extern bool IsProcessInJob(IntPtr p, IntPtr j, out bool r); }
'@
$child = Start-Process -FilePath "$env:SystemRoot\System32\PING.EXE" -ArgumentList '-n','3','127.0.0.1' -WindowStyle Hidden -PassThru
$inside = $false
[void][JobProbe]::IsProcessInJob($child.Handle, $script:AnyAiCamVmsJob, [ref]$inside)
Write-Output "child-in-job=$inside"
$child.WaitForExit()
"""
        result = _ps(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resource limits applied (cpu=50;memoryMB=4096)", result.stdout)
        self.assertIn("child-in-job=True", result.stdout)

    def test_a_refusal_is_reported_and_never_stops_the_launcher(self):
        body = ("function Initialize-AnyAiCamJobApi { throw 'job API refused' }\n"
                "Write-Output (Set-AnyAiCamVmsJobLimits @{})\nWrite-Output 'launcher-continues'\n")
        result = _ps(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resource limits NOT applied, starting without them: job API refused", result.stdout)
        self.assertIn("launcher-continues", result.stdout)


if __name__ == "__main__":
    unittest.main()
