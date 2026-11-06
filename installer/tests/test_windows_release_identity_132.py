"""Windows setup identity (owner 2026-10-20): customer release "AnyAiCam VMS 1.2.7", internal Windows setup 1.3.2.

* The customer reads the release: wizard title (AppVerName), Apps & Features / uninstall name (UninstallDisplayName),
  the setup file's Product version, /version and the VMS pages (vms.env ANYAICAM_VERSION), and the cloud's
  software_version (vms_release.json release_version -> the agent's "<release>+<commit12>").
* The setup version keeps rising for the downgrade guard: AppVersion -> DisplayVersion, ANYAICAM_WINDOWS_SETUP_VERSION,
  installer_version, and the setup file name (unchanged pattern, which the Sandbox tools and publishing rely on).
* The build identification is the exact full commit (ANYAICAM_BUILD_ID / vms_release_commit; the short form in the name).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
SETUP = ISS.split("[Setup]", 1)[1].split("\n[", 1)[0]
BUILD = (WIN / "build.ps1").read_text(encoding="utf-8")
RUNTIME = (WIN / "install-runtime.ps1").read_text(encoding="utf-8")
CLOUD_LINK = WIN / "cloud-link.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
ISCC = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 7" / "ISCC.exe"
COMMIT = "c8abe0d34db2425afe224d89a8deb03340d7cdd0"

sys.path.insert(0, str(ROOT / "appliance-agent"))
from anyaicam_agent.updater.owner_update import installed_release_label  # noqa: E402


class IdentityTests(unittest.TestCase):
    def test_customer_facing_names_carry_the_release(self):
        for line in ("AppVerName={#AppName} {#ReleaseVersion}", "UninstallDisplayName={#AppName} {#ReleaseVersion}",
                     "VersionInfoProductName={#AppName}", "VersionInfoProductTextVersion={#ReleaseVersion}",
                     "VersionInfoCompany=AnyAiCam"):
            self.assertIn(line, SETUP)

    def test_the_setup_version_still_orders_upgrades(self):
        self.assertIn("AppVersion={#AppVersion}", SETUP)                       # -> DisplayVersion (the guard reads it)
        self.assertIn("VersionInfoTextVersion={#AppVersion}", SETUP)
        self.assertIn("OutputBaseFilename=AnyAiCam-VMS-Setup-{#AppVersion}-{#ShortCommit}", SETUP)
        self.assertIn("{#SourceCommit}", re.search(r"^VersionInfoDescription=.*$", SETUP, re.M).group(0))
        self.assertIn('-SetupVersion "{#AppVersion}"', ISS)                      # the guard compares setup versions

    def test_a_bare_compile_falls_back_to_the_setup_version(self):
        self.assertRegex(ISS, r"#ifndef ReleaseVersion\s+#define ReleaseVersion AppVersion\s+#endif")

    def test_build_requires_and_validates_both_versions(self):
        self.assertIn("[Parameter(Mandatory=$true)][string]$ReleaseVersion,", BUILD)
        release_rule = re.search(r"\$ReleaseVersion -notmatch '([^']+)'", BUILD).group(1)
        for ok in ("1.2.7", "1.3.0", "2.0.0"):
            self.assertRegex(ok, release_rule)
        for bad in ("1.2", "1.2.7.1", "1.2.7-rc", "01.2.7", ""):
            self.assertNotRegex(bad, release_rule)
        self.assertIn('$compilerArguments += "/DReleaseVersion=$ReleaseVersion"', BUILD)
        self.assertIn('$compilerArguments += "/DSourceCommit=$sourceCommit"', BUILD)

    def test_install_runtime_writes_the_release_and_the_setup_version_separately(self):
        rule = re.search(r"\[ValidatePattern\('([^']+)'\)\]\[string\]\$ReleaseVersion", RUNTIME).group(1)
        self.assertRegex("1.2.7", rule)
        self.assertNotRegex("1.2.7-rc1", rule)
        self.assertIn("$values['ANYAICAM_VERSION'] = $ReleaseVersion", RUNTIME)
        self.assertIn("$values['ANYAICAM_WINDOWS_SETUP_VERSION'] = $AppVersion", RUNTIME)
        self.assertIn("$values['ANYAICAM_BUILD_ID'] = $SourceCommit", RUNTIME)
        self.assertIn("-AppVersion $AppVersion -ReleaseVersion $ReleaseVersion -SourceCommit $SourceCommit", RUNTIME)
        self.assertIn('-AppVersion "{#AppVersion}" -ReleaseVersion "{#ReleaseVersion}"', ISS)

    def test_customer_messages_name_the_release(self):
        code = ISS.split("[Code]", 1)[1]
        self.assertIn("This setup (AnyAiCam VMS {#ReleaseVersion}, ' +", code)
        self.assertIn("safe to install AnyAiCam VMS {#ReleaseVersion} on this computer", code)
        self.assertIn("Log('Downgrade refused: a newer AnyAiCam VMS is installed", code)   # Sandbox greps this prefix


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "runs cloud-link.ps1 with Windows PowerShell")
class ReleaseMarkerTests(unittest.TestCase):
    def run_cloud_link(self, *extra):
        root = Path(tempfile.mkdtemp(prefix="aac-release-id-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(CLOUD_LINK), "-DataRoot", str(root), "-AppVersion", "1.3.2",
                                 "-SourceCommit", COMMIT, *extra], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = root / "config" / "vms_release.json"
        return marker, json.loads(marker.read_text(encoding="utf-8-sig"))

    def test_the_agent_reports_the_release_and_the_full_commit_to_the_cloud(self):
        marker, release = self.run_cloud_link("-ReleaseVersion", "1.2.7")
        self.assertEqual((release["release_version"], release["installer_version"], release["vms_release_commit"],
                          release["platform"]), ("1.2.7", "1.3.2", COMMIT, "windows"))
        config = type("Config", (), {"vms_release_marker_file": marker, "software_version": "0.1.0"})()
        self.assertEqual(installed_release_label(config), "1.2.7+" + COMMIT[:12])    # the heartbeat's software_version

    def test_an_older_caller_without_a_release_keeps_one_number(self):
        _, release = self.run_cloud_link()
        self.assertEqual((release["release_version"], release["installer_version"]), ("1.3.2", "1.3.2"))


@unittest.skipUnless(os.name == "nt" and ISCC.exists(), "needs the Inno Setup 7 compiler")
class SetupSectionCompileTests(unittest.TestCase):
    def test_the_real_setup_section_and_code_compile(self):
        """The real [Setup] section (AppVerName, UninstallDisplayName, VersionInfo*) plus the real [Code], compiled with
        the build's defines and /O- (no setup file is produced)."""
        guard = WIN / "install-version-guard.ps1"
        code = ISS.split("[Code]", 1)[1]
        with tempfile.TemporaryDirectory(prefix="aac-iss-setup-") as folder:
            wrapper = Path(folder) / "identity-check.iss"
            wrapper.write_text(
                '#define AppName "AnyAiCam VMS"\n#define AppVersion "1.3.2"\n#define ReleaseVersion "1.2.7"\n'
                f'#define SourceCommit "{COMMIT}"\n#define ShortCommit Copy(SourceCommit, 1, 7)\n'
                '#define KeysetSha256 "2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c"\n'
                '#define RegistrySha256 "0000000000000000000000000000000000000000000000000000000000000000"\n'  # required since LIC-W1 66de7c9
                "[Setup]" + SETUP.replace("OutputDir=output", "OutputDir=out") + "\n"
                f'[Files]\nSource: "{guard}"; Flags: dontcopy\n[Code]' + code, encoding="utf-8")
            result = subprocess.run([str(ISCC), "/O-", "/Q", str(wrapper)], capture_output=True, text=True, cwd=folder, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(list(Path(folder).rglob("*.exe")))


if __name__ == "__main__":
    unittest.main()
