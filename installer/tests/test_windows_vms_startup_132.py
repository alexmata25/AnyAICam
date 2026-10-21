"""Windows 1.3.2 (Blue): the VMS really starts with the Windows service launcher's environment.

Opt-in (it imports torch/ultralytics and starts uvicorn, ~1.5 GB RAM): set ANYAICAM_WINDOWS_RUNTIME_PYTHON to a Windows
Python 3.12 with the installer's pinned packages (for example an extracted python-3.12.10-embed-amd64.zip plus
vendor\\wheels, the same steps as install-runtime.ps1). The environment is the one service-launcher.ps1 really builds,
against a temporary ProgramData, with a vms.env shaped like install-runtime.ps1's. No credential exists and
ANYAICAM_CLOUD_URL points at a closed loopback port, so nothing can reach the AnyAiCam cloud.

Checks: /health answers; /version reports the installed version and build; the VMS created its database under the
temporary ProgramData (never C:\\var or C:\\opt); it stops cleanly.
"""
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
RUNTIME = os.environ.get("ANYAICAM_WINDOWS_RUNTIME_PYTHON", "")
PYTHON_LINE = re.compile(r"^& \(Join-Path \$installRoot 'runtime\\python\\python\.exe'\).*$", re.M)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(os.name == "nt" and POWERSHELL and RUNTIME and Path(RUNTIME).is_file(),
                     "opt-in: set ANYAICAM_WINDOWS_RUNTIME_PYTHON to a Windows runtime with the pinned packages")
class VmsStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-startup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.program_data = self.tmp / "ProgramData"
        config = self.program_data / "AnyAiCam" / "config"
        config.mkdir(parents=True)
        self.port = free_port()
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.commit = commit
        (config / "vms.env").write_text("\n".join([
            "# shaped like install-runtime.ps1's vms.env",
            f"ANYAICAM_APP_SECRETS={secrets.token_hex(32)}",
            f"ANYAICAM_CAMERA_CREDENTIAL_KEY={secrets.token_urlsafe(32)}",
            "PEOPLE_COUNTING_ENABLED=true", "CUSTOMER_ANALYTICS_RULES_ENABLED=true",
            "ANYAICAM_RUNTIME_ROLE=edge", "ANYAICAM_ENV=production", "ANYAICAM_FORCE_HTTPS=false",
            "ANYAICAM_SECURE_COOKIES=false", f"ANYAICAM_PUBLIC_URL=http://127.0.0.1:{self.port}",
            f"ANYAICAM_VMS_COMMIT={commit}", f"ANYAICAM_BUILD_ID={commit}", "ANYAICAM_PRODUCT_MODE_BOOTSTRAP=local",
            "ANYAICAM_VERSION=1.3.2", f"ANYAICAM_HTTP_PORT={self.port}", "ANYAICAM_BIND_HOST=127.0.0.1",
            "ANYAICAM_CLOUD_URL=http://127.0.0.1:9", ""]), encoding="utf-8")

    def launcher_environment(self):
        """The environment service-launcher.ps1 builds (run for real up to where it would start Python)."""
        install = self.tmp / "install"
        (install / "service").mkdir(parents=True)
        (install / "app").mkdir()
        text = PYTHON_LINE.sub("Get-ChildItem env: | ForEach-Object { \"$($_.Name)=$($_.Value)\" }",
                               (WIN / "service-launcher.ps1").read_text(encoding="utf-8"))
        script = install / "service" / "service-launcher.ps1"
        script.write_text(text, encoding="utf-8")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(script)], capture_output=True, text=True, timeout=120,
                                env={**os.environ, "ProgramData": str(self.program_data)})
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        env["ANYAICAM_STATIC_FOLDER"] = str(ROOT / "app" / "static")      # the source tree stands in for {app}\app
        return env

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")

    def test_the_vms_starts_with_the_launcher_environment_and_keeps_data_in_programdata(self):
        env = self.launcher_environment()
        drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        posix_roots = [drive / "var" / "lib" / "anyaicam", drive / "opt" / "anyaicam", drive / "opt" / "anyaicam-ppe-model",
                       drive / "app"]
        existed = {path: path.exists() for path in posix_roots}
        self.assertEqual(env["ANYAICAM_STATE_DIR"], str(self.program_data / "AnyAiCam" / "agent"))
        log = open(self.tmp / "vms.log", "w", encoding="utf-8")
        self.addCleanup(log.close)
        process = subprocess.Popen([RUNTIME, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(self.port)],
                                   cwd=str(ROOT / "app"), env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline, status = time.time() + 240, None
            while time.time() < deadline and process.poll() is None:
                try:
                    status, _ = self.get("/health")
                    break
                except OSError:
                    time.sleep(2)
            self.assertIsNone(process.poll(), (self.tmp / "vms.log").read_text(encoding="utf-8", errors="replace")[-4000:])
            self.assertEqual(status, 200)
            status, body = self.get("/version")
            self.assertEqual(status, 200)
            self.assertIn("1.3.2", body)
            self.assertIn(self.commit[:7], body)
            self.assertTrue((self.program_data / "AnyAiCam" / "database" / "partner_portal.db").is_file())
        finally:
            process.terminate()
            process.wait(timeout=60)
        for path, was_there in existed.items():
            if not was_there:
                self.assertFalse(path.exists(), f"the VMS created {path} (a Linux path on Windows)")
        self.assertNotIn("Traceback", (self.tmp / "vms.log").read_text(encoding="utf-8", errors="replace").split("Application startup complete")[0])


if __name__ == "__main__":
    unittest.main()
