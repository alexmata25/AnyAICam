"""Windows 1.3.2 (Blue): the per-appliance secret the shared 168 h analytics grant depends on.

Red's shared design (RED-ANALYTICS-OFFLINE-168H-DESIGN-20261019.md §3) MACs the persisted analytics grant with a key
derived from ANYAICAM_APP_SECRETS; no secret means premium analytics are denied. On Windows that secret is generated
by install-runtime.ps1 into %ProgramData%\\AnyAiCam\\config\\vms.env (SYSTEM/Administrators only) and handed to the
VMS by service-launcher.ps1. These tests run the installer's own lines and the real launcher:
  * a new install gets a 256-bit random secret;
  * a repair or upgrade keeps the existing secret byte-identical (otherwise every stored grant -- and every session --
    would be invalidated by a reinstall);
  * two installs never share a secret;
  * the VMS service process receives exactly the stored secret.
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


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


class StaticTests(unittest.TestCase):
    def test_the_secret_is_generated_only_when_missing_and_never_logged(self):
        runtime = read("install-runtime.ps1")
        self.assertIn("if (-not $values.Contains('ANYAICAM_APP_SECRETS')) {", runtime)
        self.assertIn("[Security.Cryptography.RandomNumberGenerator]::Create()", runtime)
        self.assertNotRegex(runtime, r"Write-(Output|Host|Verbose)[^\n]*APP_SECRETS")
        self.assertIn("# Managed by the AnyAiCam Windows installer. Persistent across repair and uninstall.", runtime)


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell")
class InstallerSecretTests(unittest.TestCase):
    """install-runtime.ps1's own vms.env read + secret block, against a temporary vms.env."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-secret-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_block(self, env_file):
        runtime = read("install-runtime.ps1")
        start = runtime.index("$values = [ordered]@{}")
        end = runtime.index("if (-not $values.Contains('ANYAICAM_CAMERA_CREDENTIAL_KEY'))")
        script = (f"$environmentFile = '{env_file}'\n" + runtime[start:end] +
                  "\"SECRET=$($values['ANYAICAM_APP_SECRETS'])\"\n")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return re.search(r"^SECRET=(.*)$", result.stdout, re.M).group(1)

    def test_a_new_install_gets_a_256_bit_random_secret_and_two_installs_differ(self):
        first, second = self.run_block(self.tmp / "absent.env"), self.run_block(self.tmp / "absent.env")
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_repair_and_upgrade_keep_the_existing_secret(self):
        saved = "ab" * 32
        env_file = self.tmp / "vms.env"
        env_file.write_text(f"# Managed by the AnyAiCam Windows installer.\nANYAICAM_APP_SECRETS={saved}\nANYAICAM_VERSION=1.3.0\n",
                            encoding="utf-8")
        self.assertEqual(self.run_block(env_file), saved)


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell")
class LauncherSecretTests(unittest.TestCase):
    def test_the_vms_process_receives_the_stored_secret(self):
        tmp = Path(tempfile.mkdtemp(prefix="anyaicam-secret-launch-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        install, program_data = tmp / "install", tmp / "ProgramData"
        (install / "service").mkdir(parents=True)
        (install / "app").mkdir()
        (program_data / "AnyAiCam" / "config").mkdir(parents=True)
        secret = "cd" * 32
        (program_data / "AnyAiCam" / "config" / "vms.env").write_text(f"ANYAICAM_APP_SECRETS={secret}\n", encoding="utf-8")
        text = PYTHON_LINE.sub("\"SECRET=$env:ANYAICAM_APP_SECRETS\"", read("service-launcher.ps1"))
        script = install / "service" / "service-launcher.ps1"
        script.write_text(text, encoding="utf-8")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(script)], capture_output=True, text=True, timeout=120,
                                env={**os.environ, "ProgramData": str(program_data)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SECRET={secret}", result.stdout)


if __name__ == "__main__":
    unittest.main()
