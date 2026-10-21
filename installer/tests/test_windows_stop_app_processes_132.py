"""Windows 1.3.2 (Blue, Sandbox acceptance P-H 2026-10-21): processes still running from the install folder are stopped
before Setup replaces files and before the uninstaller removes them.

In Hybrid the VMS runs MediaMTX (WebRTC live view) as a child process; stopping the WinSW service did not end it, so a
reinstall/upgrade failed with "The process cannot access the file ...\\mediamtx.exe" (setup exit 20).
stop-app-processes.ps1 is EXECUTED here: Microsoft-signed PING.EXE copies run from a fake install folder and from a
look-alike sibling folder; only the one inside the install folder may be stopped.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

WIN = Path(__file__).resolve().parents[1] / "windows"
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
SCRIPT = WIN / "stop-app-processes.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
PING = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE"


@unittest.skipUnless(os.name == "nt" and POWERSHELL and PING.exists(), "runs real processes on Windows")
class StopAppProcessesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aac-stop-"))
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_copy(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / "mediamtx.exe"            # named like the real culprit
        shutil.copy2(PING, exe)
        p = subprocess.Popen([str(exe), "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def run_script(self, root: Path):
        return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPT), "-InstallRoot", str(root)], capture_output=True, text=True, timeout=120)

    def test_only_processes_inside_the_install_folder_are_stopped(self):
        root = self.tmp / "AnyAiCam"
        inside = self.start_copy(root / "runtime" / "tools" / "mediamtx")
        sibling = self.start_copy(self.tmp / "AnyAiCam2" / "runtime")          # same prefix, different folder
        system = subprocess.Popen([str(PING), "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(system)
        time.sleep(1.5)
        result = self.run_script(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Stopping mediamtx.exe (pid {inside.pid})", result.stdout)
        self.assertIn("AnyAiCam folder processes stopped: 1 of 1.", result.stdout)
        inside.wait(timeout=20)
        self.assertIsNotNone(inside.poll())
        self.assertIsNone(sibling.poll(), "a process in a look-alike folder must not be touched")
        self.assertIsNone(system.poll(), "a system process must not be touched")

    def test_nothing_running_is_a_no_op(self):
        result = self.run_script(self.tmp / "AnyAiCam")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AnyAiCam folder processes stopped: 0 of 0.", result.stdout)


class WiringTests(unittest.TestCase):
    def test_setup_stops_folder_processes_after_the_services_and_before_any_file(self):
        prepare = ISS.split("function PrepareToInstall(var NeedsRestart: Boolean): String;", 1)[1].split("\nend;\n", 1)[0]
        stop = prepare.index("ExtractTemporaryFile('stop-app-processes.ps1');")
        self.assertLess(prepare.rindex("'uninstall'"), stop)                   # after both services are removed
        self.assertIn('-InstallRoot "\' + ExpandConstant(\'{app}\') + \'"\'', prepare)
        self.assertLess(prepare.index("Result := ProductModeProblem();"), stop)  # a refused setup still changes nothing

    def test_the_uninstaller_stops_them_after_removing_the_services(self):
        run = ISS.split("[UninstallRun]", 1)[1].split("\n[", 1)[0]
        self.assertLess(run.index('RunOnceId: "RemoveAnyAiCamVMS"'), run.index('RunOnceId: "StopAnyAiCamFolderProcesses"'))
        self.assertIn('-File ""{app}\\installer\\stop-app-processes.ps1"" -InstallRoot ""{app}""', run)

    def test_the_script_is_packaged_for_both(self):
        self.assertIn('Source: "stop-app-processes.ps1"; Flags: dontcopy', ISS)
        self.assertIn('Source: "stop-app-processes.ps1"; DestDir: "{app}\\installer"; Flags: ignoreversion', ISS)

    def test_the_script_never_touches_the_caller_or_the_uninstaller(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for guard in ("$_.ProcessId -ne $self", "$_.ProcessId -ne $parent", "$_.Name -notlike 'unins*'",
                      ".TrimEnd('\\') + '\\'", "StartsWith($root, [StringComparison]::OrdinalIgnoreCase)"):
            self.assertIn(guard, text)


if __name__ == "__main__":
    unittest.main()
