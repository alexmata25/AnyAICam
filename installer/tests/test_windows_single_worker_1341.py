"""Windows 1.3.4.1 R1 (Codex revision review P1, owner 2026-11-06): the VMS service runs exactly one worker process.

users.json (and the VMS's other JSON stores) are serialized by in-process locks. Before, service-launcher.ps1 copied
every vms.env line into the environment and started uvicorn without --workers, and uvicorn starts one worker per
WEB_CONCURRENCY when --workers is not given: a WEB_CONCURRENCY=4 line in vms.env would have started four processes,
each with its own lock. The launcher now pins --workers 1 and WEB_CONCURRENCY=1 after reading vms.env. It is run here
for real, up to the point where it would start Python, against a temporary ProgramData.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
PYTHON_LINE = re.compile(r"^& \(Join-Path \$installRoot 'runtime\\python\\python\.exe'\).*$", re.M)


def launcher():
    return (WIN / "service-launcher.ps1").read_text(encoding="utf-8")


class StaticTests(unittest.TestCase):
    def test_uvicorn_is_started_with_exactly_one_worker(self):
        lines = PYTHON_LINE.findall(launcher())
        self.assertEqual(len(lines), 1)
        self.assertIn("-m uvicorn main:app", lines[0])
        self.assertTrue(lines[0].rstrip().endswith("--workers 1"), lines[0])

    def test_web_concurrency_is_pinned_after_vms_env_is_read(self):
        text = launcher()
        pin = "$env:WEB_CONCURRENCY = '1'"
        self.assertIn(pin, text)
        self.assertLess(text.index("foreach ($line in Get-Content -LiteralPath $environmentFile)"), text.index(pin))
        self.assertLess(text.index(pin), PYTHON_LINE.search(text).start())


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell")
class LauncherRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-single-worker-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = self.tmp / "Program Files" / "AnyAiCam"
        (self.install / "service").mkdir(parents=True)
        (self.install / "app").mkdir()
        self.program_data = self.tmp / "ProgramData"
        (self.program_data / "AnyAiCam" / "config").mkdir(parents=True)

    def run_launcher(self, vms_env):
        (self.program_data / "AnyAiCam" / "config" / "vms.env").write_text(vms_env, encoding="utf-8")
        text = launcher()
        command = PYTHON_LINE.search(text).group(0)
        # Prints what Python would have been started with instead of starting it.
        text = text.replace(command, "Write-Output \"WEB_CONCURRENCY=$env:WEB_CONCURRENCY\"")
        script = self.install / "service" / "service-launcher.ps1"
        script.write_text(text, encoding="utf-8")
        env = {**os.environ, "ProgramData": str(self.program_data)}
        env.pop("WEB_CONCURRENCY", None)
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(script)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_vms_env_worker_count_is_ignored_and_logged(self):
        output = self.run_launcher("WEB_CONCURRENCY=4\nANYAICAM_HTTP_PORT=8000\n")
        self.assertIn("WEB_CONCURRENCY=1", output.splitlines())
        self.assertIn("WEB_CONCURRENCY=4 in vms.env ignored; the VMS runs one worker.", output)

    def test_without_a_vms_env_worker_count_one_worker_is_used_silently(self):
        output = self.run_launcher("ANYAICAM_HTTP_PORT=8000\n")
        self.assertIn("WEB_CONCURRENCY=1", output.splitlines())
        self.assertNotIn("ignored", output)


if __name__ == "__main__":
    unittest.main()
