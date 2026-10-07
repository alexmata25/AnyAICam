"""Windows native installer (installer/windows, 2026-10-07): source-level checks
that run on any machine. They never install anything, change a firewall or
start a service; a clean Windows machine (Windows Sandbox) is the place for that.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


def pins(path):
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?==([^\s;]+)", line)
        if match:
            out[match.group(1).lower().replace("_", "-")] = match.group(3)
    return out


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.text = read("service-launcher.ps1")

    def test_every_vms_folder_is_set_and_matches_the_0_1_3_layout(self):
        for line in (
            "$env:ANYAICAM_STATIC_FOLDER = Join-Path $installRoot 'app\\static'",
            "$env:ANYAICAM_RECORDINGS_FOLDER = Join-Path $dataRoot 'recordings'",
            "$env:ANYAICAM_HLS_FOLDER = Join-Path $dataRoot 'hls'",
            "$env:ANYAICAM_PARTNER_DB = Join-Path $dataRoot 'database\\partner_portal.db'",
            "$env:ANYAICAM_MEDIAMTX_BINARY = Join-Path $installRoot 'runtime\\tools\\mediamtx\\mediamtx.exe'",
            "$env:ANYAICAM_MEDIAMTX_CONFIG = Join-Path $dataRoot 'mediamtx\\anyaicam-mediamtx.yml'",
        ):
            self.assertIn(line, self.text)
        self.assertIn("$dataRoot = Join-Path $env:ProgramData 'AnyAiCam'", self.text)

    def test_listens_on_the_local_network_by_default_with_overrides(self):
        self.assertIn("else { '0.0.0.0' }", self.text)
        self.assertIn("$settings['ANYAICAM_BIND_HOST']", self.text)
        self.assertIn("-match '^\\d{2,5}$'", self.text)
        self.assertIn("--host $bindHost --port $port", self.text)
        self.assertNotIn("--host 127.0.0.1", self.text)


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.text = read("firewall.ps1")

    def test_rules_admit_only_private_networks_and_tailscale(self):
        self.assertIn("$remote = @('LocalSubnet', '100.64.0.0/10')", self.text)
        self.assertIn("$profiles = @('Private', 'Domain')", self.text)
        self.assertNotIn("Public", self.text.split("$profiles", 1)[1].split("\n", 1)[0])
        self.assertEqual(self.text.count("-RemoteAddress $remote -Profile $profiles"), 2)

    def test_each_rule_is_scoped_to_this_installs_program_and_port(self):
        self.assertRegex(self.text, r"-Protocol TCP -LocalPort \$WebPort -Program \$python")
        self.assertRegex(self.text, r"-Protocol UDP -LocalPort \$WebRtcPort -Program \$mediamtx")
        self.assertIn("[int]$WebPort = 8000", self.text)
        self.assertIn("[int]$WebRtcPort = 8189", self.text)  # same port as the Linux appliance

    def test_apply_is_idempotent_and_remove_takes_only_our_group(self):
        self.assertIn("Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule", self.text)
        self.assertLess(self.text.index("\nRemove-AnyAiCamRules\n"), self.text.index("New-NetFirewallRule -Name 'AnyAiCam-VMS-Web'"))
        self.assertIn("if ($Apply -eq $Remove) { throw", self.text)


class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.iss = read("AnyAiCam-VMS.iss")

    def test_version_and_commit_come_from_the_build(self):
        self.assertNotIn("0.1.3", self.iss)
        self.assertNotIn("ec5272f", self.iss)
        self.assertIn("OutputBaseFilename=AnyAiCam-VMS-Setup-{#AppVersion}-{#ShortCommit}", self.iss)
        build = read("build.ps1")
        self.assertIn('"/DAppVersion=$Version"', build)
        self.assertIn('"/DSourceCommit=$sourceCommit"', build)
        self.assertIn("build only from a committed tree", build)

    def test_upgrade_replaces_program_code_but_never_data(self):
        self.assertIn('[InstallDelete]\n', self.iss)
        self.assertIn('Type: filesandordirs; Name: "{app}\\app"', self.iss)
        install_delete = self.iss.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0]
        self.assertNotIn("commonappdata", install_delete.split("\n", 3)[-1])
        for folder in ("config", "database", "recordings", "hls", "logs", "mediamtx"):
            self.assertIn(f'Name: "{{commonappdata}}\\AnyAiCam\\{folder}"; Flags: uninsneveruninstall', self.iss)

    def test_ships_mediamtx_and_firewall_and_removes_rules_on_uninstall(self):
        self.assertIn('Source: "vendor\\mediamtx_v1.21.0_windows_amd64.zip"', self.iss)
        self.assertIn('Source: "firewall.ps1"; DestDir: "{app}\\installer"', self.iss)
        uninstall = self.iss.split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0]
        self.assertIn('firewall.ps1"" -Remove', uninstall)
        self.assertIn("-MediaMtxArchive", self.iss)
        self.assertIn('-AppVersion "{#AppVersion}"', self.iss)

    def test_service_is_installed_started_and_auto_starting(self):
        self.assertIn("'install', 'Installing the AnyAiCam Windows service...'", self.iss)
        self.assertIn("'start', 'Starting the AnyAiCam Windows service...'", self.iss)
        service = read("AnyAiCamVMS.xml")
        self.assertIn("<startmode>Automatic</startmode>", service)
        self.assertIn('<onfailure action="restart"', service)


class RuntimeScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = read("install-runtime.ps1")

    def test_data_folder_is_restricted_to_system_and_administrators(self):
        self.assertIn("icacls.exe $DataRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'", self.text)
        self.assertIn("if ($LASTEXITCODE -ne 0) { throw \"Could not restrict access", self.text)

    def test_secrets_are_generated_once_and_kept(self):
        self.assertIn("if (-not $values.Contains('ANYAICAM_APP_SECRETS'))", self.text)
        self.assertIn("if (-not $values.Contains('ANYAICAM_CAMERA_CREDENTIAL_KEY'))", self.text)
        self.assertIn("RandomNumberGenerator", self.text)

    def test_version_mediamtx_and_firewall(self):
        self.assertIn("$values['ANYAICAM_VERSION'] = $AppVersion", self.text)
        self.assertIn("Expand-Archive -LiteralPath $MediaMtxArchive", self.text)
        self.assertIn("installer\\firewall.ps1') -Apply -InstallRoot $InstallRoot", self.text)


class PinTests(unittest.TestCase):
    def test_vendor_pins_match_dependencies_md(self):
        build = read("build.ps1")
        deps = read("DEPENDENCIES.md")
        pinned = dict(re.findall(r"'([A-Za-z0-9_.\-]+\.(?:zip|py|exe))'\s*=\s*'([0-9a-f]{64})'", build))
        self.assertEqual(set(pinned), {"python-3.12.10-embed-amd64.zip", "get-pip.py", "WinSW-x64.exe",
                                       "ffmpeg-8.1.2-essentials_build.zip", "mediamtx_v1.21.0_windows_amd64.zip"})
        for name, digest in pinned.items():
            self.assertIn(digest, deps, name)

    def test_windows_packages_match_the_linux_image_pins(self):
        windows = pins(WIN / "requirements-windows.txt")
        for source in ("requirements.txt", "requirements-cpu.txt", "requirements-push.txt"):
            for name, version in pins(ROOT / source).items():
                self.assertEqual(windows.get(name), version, f"{source}: {name}")

    def test_wheel_lock_covers_every_package(self):
        lock = read("wheels.lock.sha256").splitlines()
        self.assertTrue(lock)
        names = {re.split(r"-\d", line.split("  ", 1)[1], maxsplit=1)[0].lower().replace("_", "-") for line in lock}
        for name in pins(WIN / "requirements-windows.txt"):
            self.assertIn(name, names, name)
        for line in lock:
            self.assertRegex(line, r"^[0-9a-f]{64}  \S+\.whl$")


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class PowerShellParseTests(unittest.TestCase):
    def test_every_script_parses(self):
        for script in sorted(WIN.glob("*.ps1")):
            command = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                       f"'{script}',[ref]$null,[ref]$e); if($e){{$e|%{{$_.Message}}; exit 1}}")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stdout}{result.stderr}")


if __name__ == "__main__":
    unittest.main()
