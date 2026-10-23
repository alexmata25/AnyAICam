"""Windows 1.3.3 D4 (Blue, Sandbox U132-L rerun on 906f7819, 2026-10-23): service-launcher.ps1 stops a VMS left over from an
earlier service run before it starts the VMS.

Seen in the Sandbox: a stop sent Ctrl+C, the launcher exited at once (code 0), WinSW did not stop its children, and the VMS
python survived as an orphan holding port 8000 with the old product mode. Every new VMS failed to bind ([Errno 10048]) and
the service restarted every ~45 s for 20+ minutes (no MediaMTX, frozen live playlist, empty offline live view).

stop-stale-vms.ps1 is EXECUTED here against a temporary install folder. Copies of the Microsoft-signed CMD.EXE / PING.EXE
stand in for the VMS python (its command line carries "uvicorn main:app"), the cloud agent and FFmpeg. Only the stale VMS,
its in-folder children and orphaned FFmpeg may be stopped; the agent, FFmpeg with a living parent and anything outside the
install folder must survive.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

WIN = Path(__file__).resolve().parents[1] / "windows"
SCRIPT = WIN / "stop-stale-vms.ps1"
LAUNCHER = (WIN / "service-launcher.ps1").read_text(encoding="utf-8-sig")
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
CMD, PING = SYSTEM32 / "cmd.exe", SYSTEM32 / "PING.EXE"


def alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return re.search(rf"\b{pid}\b", out) is not None


def pids_of(exe: Path):
    script = f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq '{exe}' }}).ProcessId -join ','"
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip()
    return [int(x) for x in out.split(",") if x.strip()]


class LauncherWiringTests(unittest.TestCase):
    def test_the_launcher_runs_the_check_right_before_it_starts_the_vms(self):
        check = LAUNCHER.index("stop-stale-vms.ps1")
        start = LAUNCHER.index("& (Join-Path $installRoot 'runtime\\python\\python.exe')")
        self.assertLess(check, start)
        self.assertIn("try { & (Join-Path $PSScriptRoot 'stop-stale-vms.ps1') -InstallRoot $installRoot } catch {", LAUNCHER)

    def test_setup_installs_it_next_to_the_launcher(self):
        self.assertIn('Source: "stop-stale-vms.ps1"; DestDir: "{app}\\service"; Flags: ignoreversion', ISS)


@unittest.skipUnless(os.name == "nt" and POWERSHELL and CMD.exists() and PING.exists(), "runs real processes on Windows")
class StaleVmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="aac-stale-")))  # long path, as Windows reports executables
        self.root = self.tmp / "AnyAiCam"
        self.procs, self.extra = [], []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        for pid in self.extra:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        time.sleep(0.5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy(self, source: Path, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def popen(self, argv):
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.procs.append(p)
        return p

    def run_script(self, root=None):
        return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPT), "-InstallRoot", str(root or self.root)],
                              capture_output=True, text=True, timeout=120)

    def test_d4_stale_vms_its_children_and_orphaned_ffmpeg_are_stopped_the_rest_survives(self):
        python = self.copy(CMD, self.root / "runtime" / "python" / "python.exe")
        ffmpeg = self.copy(PING, self.root / "runtime" / "tools" / "ffmpeg" / "ffmpeg.exe")
        orphan_ffmpeg = self.copy(PING, self.root / "runtime" / "tools" / "ffmpeg" / "ffprobe.exe")
        # the stale VMS: "uvicorn main:app" on its command line, running an in-folder FFmpeg child
        # (one command-line string: cmd /c strips the outer quotes, keeping the quoted path intact)
        stale = self.popen(f'"{python}" /c ""{ffmpeg}" -n 120 127.0.0.1 >nul & rem -m uvicorn main:app --host 0.0.0.0"')
        # the cloud agent: same python, other script -> must survive
        agent = self.popen([str(python), "/c", "ping -n 120 127.0.0.1 >nul & rem agent-main.py"])
        # FFmpeg whose parent (this test) is alive -> must survive
        kept_ffmpeg = self.popen([str(self.copy(PING, self.root / "runtime" / "tools" / "mediamtx" / "mediamtx.exe")), "-n", "120", "127.0.0.1"])
        # an orphan: started by a short-lived PowerShell that exits at once (its parent is gone)
        subprocess.run([POWERSHELL, "-NoProfile", "-Command",
                        f"Start-Process -FilePath '{orphan_ffmpeg}' -ArgumentList '-n','120','127.0.0.1' -WindowStyle Hidden"],
                       timeout=60, capture_output=True)
        # a look-alike install folder with the same prefix -> must survive
        sibling = self.copy(CMD, self.tmp / "AnyAiCam2" / "runtime" / "python" / "python.exe")
        other = self.popen([str(sibling), "/c", "ping -n 120 127.0.0.1 >nul & rem uvicorn main:app"])
        time.sleep(2)
        child_ffmpeg, orphan = pids_of(ffmpeg), pids_of(orphan_ffmpeg)
        self.assertEqual(len(child_ffmpeg), 1)
        self.assertEqual(len(orphan), 1)
        self.extra += orphan
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn(f"stopping stale VMS python.exe (pid {stale.pid})", out)
        self.assertIn(f"stopping left-over ffmpeg.exe (pid {child_ffmpeg[0]})", out)
        self.assertIn(f"stopping left-over ffprobe.exe (pid {orphan[0]})", out)
        self.assertIn("stopped 3 of 3 process(es)", out)
        stale.wait(timeout=20)
        self.assertFalse(alive(child_ffmpeg[0]))
        self.assertFalse(alive(orphan[0]))
        for survivor in (agent, kept_ffmpeg, other):
            self.assertIsNone(survivor.poll(), f"pid {survivor.pid} must survive")

    def test_nothing_to_do_prints_nothing_and_never_fails(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_missing_install_folder_never_fails(self):
        self.assertEqual(self.run_script(self.tmp / "does-not-exist").returncode, 0)


if __name__ == "__main__":
    unittest.main()
