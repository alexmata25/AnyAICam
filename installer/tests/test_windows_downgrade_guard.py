"""Windows downgrade and unknown-version protection (Red 2026-10-18; fail-closed after Codex's FAIL on ef93b503).

Setup's InitializeSetup runs installer/windows/install-version-guard.ps1 before anything is changed and continues
only on exit code 0. These tests EXECUTE the real guard script with real PowerShell (no model of it):
  * a throwaway HKCU key stands in for the two HKLM uninstall-entry views (the script's test seam);
  * a temporary folder stands in for %ProgramData% and for the install folder.
The earlier tests asserted fail-OPEN behaviour (no version -> proceed; unparseable -> proceed; 1.3.2.4 -> "cannot judge"
-> proceed). They are replaced by the cases below. The static checks pin that the .iss refuses on everything but 0.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

WIN = Path(__file__).resolve().parents[1] / "windows"
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
CODE = ISS.split("[Code]", 1)[1]
GUARD = WIN / "install-version-guard.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
CLEAN, DOWNGRADE, UNKNOWN, BAD_SETUP = 0, 10, 11, 12


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "runs the real guard with Windows PowerShell")
class GuardExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aac-guard-"))
        self.program_data = self.tmp / "ProgramData"
        self.app_dir = self.tmp / "Program Files" / "AnyAiCam"
        self.reg_root = f"HKCU:\\Software\\AnyAiCamBlueGuardTest\\{uuid.uuid4().hex}"
        self.views = [self.reg_root + "\\Uninstall64", self.reg_root + "\\Uninstall32"]
        self.denied = []

    def tearDown(self):
        for path in self.denied:
            subprocess.run(["icacls", str(path), "/remove:d", os.environ["USERNAME"]], capture_output=True)
        self._ps(f"Remove-Item -LiteralPath {_ps_quote(self.reg_root)} -Recurse -Force -ErrorAction SilentlyContinue")
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ fixtures
    def _ps(self, script):
        return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=120)

    def registry(self, view=0, display_version=None, install_location=None):
        path = self.views[view]
        lines = [f"New-Item -Path {_ps_quote(path)} -Force | Out-Null"]
        if display_version is not None:
            lines.append(f"New-ItemProperty -LiteralPath {_ps_quote(path)} -Name DisplayVersion -Value {_ps_quote(display_version)} -PropertyType String -Force | Out-Null")
        if install_location is not None:
            lines.append(f"New-ItemProperty -LiteralPath {_ps_quote(path)} -Name InstallLocation -Value {_ps_quote(install_location)} -PropertyType String -Force | Out-Null")
        result = self._ps("; ".join(lines))
        self.assertEqual(result.returncode, 0, result.stderr)

    def vms_env(self, version_line="ANYAICAM_VERSION=1.3.1", *, unreadable=False):
        path = self.program_data / "AnyAiCam" / "config" / "vms.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["ANYAICAM_APP_SECRETS=abc"] + ([version_line] if version_line is not None else [])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if unreadable:
            result = subprocess.run(["icacls", str(path), "/deny", f"{os.environ['USERNAME']}:(R)"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.denied.append(path)
            with self.assertRaises(PermissionError):
                path.read_text()
        return path

    def program_files(self, folder=None):
        service = Path(folder or self.app_dir) / "service"
        service.mkdir(parents=True, exist_ok=True)
        (service / "AnyAiCamVMS.exe").write_bytes(b"MZ")

    def run_guard(self, setup_version="1.3.2"):
        views = ",".join(_ps_quote(v) for v in self.views)
        script = (f"& {_ps_quote(str(GUARD))} -SetupVersion {_ps_quote(setup_version)} "
                  f"-DefaultAppDir {_ps_quote(str(self.app_dir))} -ProgramData {_ps_quote(str(self.program_data))} "
                  f"-UninstallKeyPaths @({views}); exit $LASTEXITCODE")
        result = self._ps(script)
        return result.returncode, result.stdout + result.stderr

    def assertDecision(self, expected, setup_version="1.3.2"):
        code, output = self.run_guard(setup_version)
        self.assertEqual(code, expected, output)
        self.assertIn(f"install-version-guard: exit {expected}:", output)
        return output

    # ------------------------------------------------------------------ allowed
    def test_genuine_clean_install_succeeds(self):
        self.assertIn("clean install", self.assertDecision(CLEAN))

    def test_same_version_repair_succeeds(self):
        self.registry(display_version="1.3.2", install_location=str(self.app_dir))
        self.program_files()
        self.vms_env("ANYAICAM_VERSION=1.3.2")
        self.assertIn("repair or upgrade", self.assertDecision(CLEAN))

    def test_upgrade_from_older_versions_succeeds(self):
        for installed in ("1.3.0", "1.3.1", "1.2.9", "1.3.1.7", "0.9.99"):
            with self.subTest(installed=installed):
                self.registry(display_version=installed)
                self.vms_env(f"ANYAICAM_VERSION={installed}")
                self.assertDecision(CLEAN)

    def test_three_and_four_part_forms_of_the_same_version_are_a_repair(self):
        self.registry(display_version="1.3.2.0")
        self.assertDecision(CLEAN, setup_version="1.3.2")
        self.registry(display_version="1.3.2")
        self.assertDecision(CLEAN, setup_version="1.3.2.0")

    def test_four_part_setup_upgrades_its_three_part_base(self):
        self.registry(display_version="1.3.2")
        self.assertDecision(CLEAN, setup_version="1.3.2.1")

    def test_repair_with_a_damaged_or_unreadable_vms_env_is_decided_by_the_uninstall_entry(self):
        # validate-in-sandbox.ps1 makes vms.env unreadable before its repair reinstall: that repair must still run.
        self.registry(display_version="1.3.2")
        self.vms_env(None)
        self.assertDecision(CLEAN)
        self.vms_env("ANYAICAM_VERSION=garbage")
        self.assertDecision(CLEAN)
        self.vms_env("ANYAICAM_VERSION=1.3.2", unreadable=True)
        self.assertDecision(CLEAN)

    def test_reinstall_after_uninstall_keeps_using_the_data_folder_version(self):
        self.vms_env("ANYAICAM_VERSION=1.3.1")         # uninstalled: no entry, no program files, data kept
        self.assertDecision(CLEAN)

    # ------------------------------------------------------------------ 1.3.2+: release 1.2.7 vs setup 1.3.2 (owner 2026-10-20)
    def env_lines(self, *lines):
        path = self.program_data / "AnyAiCam" / "config" / "vms.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(("ANYAICAM_APP_SECRETS=abc",) + lines) + "\n", encoding="utf-8")

    def test_the_setup_version_line_wins_over_the_customer_release(self):
        # what 1.3.2 writes: ANYAICAM_VERSION is the release (1.2.7), the setup version has its own line
        self.registry(display_version="1.3.2")
        self.env_lines("ANYAICAM_VERSION=1.2.7", "ANYAICAM_WINDOWS_SETUP_VERSION=1.3.2")
        self.assertIn("ANYAICAM_WINDOWS_SETUP_VERSION 1.3.2", self.assertDecision(CLEAN, setup_version="1.3.2"))   # repair
        self.assertDecision(CLEAN, setup_version="1.3.3")                                                     # upgrade

    def test_a_release_number_above_the_setup_number_never_blocks(self):
        # a later customer release (e.g. 1.4.0) shipped as Windows setup 1.3.5 must still repair/upgrade
        self.registry(display_version="1.3.5")
        self.env_lines("ANYAICAM_VERSION=1.4.0", "ANYAICAM_WINDOWS_SETUP_VERSION=1.3.5")
        self.assertDecision(CLEAN, setup_version="1.3.5")
        self.assertDecision(CLEAN, setup_version="1.3.6")

    def test_after_an_uninstall_the_setup_version_line_decides(self):
        self.env_lines("ANYAICAM_VERSION=1.2.7", "ANYAICAM_WINDOWS_SETUP_VERSION=1.3.2")
        self.assertDecision(CLEAN, setup_version="1.3.2")          # reinstall of the same setup
        self.assertDecision(DOWNGRADE, setup_version="1.3.1")      # an older setup over 1.3.2 data is refused
        self.assertDecision(CLEAN, setup_version="1.3.3")

    def test_a_malformed_setup_version_line_is_not_bypassed_by_the_release_line(self):
        self.env_lines("ANYAICAM_VERSION=1.2.7", "ANYAICAM_WINDOWS_SETUP_VERSION=1.3.2-rc1")
        self.assertIn("malformed ANYAICAM_WINDOWS_SETUP_VERSION", self.assertDecision(UNKNOWN))

    def test_a_1_3_0_or_1_3_1_install_still_upgrades_through_its_legacy_line(self):
        for legacy in ("1.3.0", "1.3.1"):
            with self.subTest(legacy=legacy):
                self.registry(display_version=legacy)
                self.env_lines(f"ANYAICAM_VERSION={legacy}")        # no setup-version line before 1.3.2
                self.assertIn(f"ANYAICAM_VERSION {legacy}", self.assertDecision(CLEAN, setup_version="1.3.2"))

    # ------------------------------------------------------------------ refused: downgrade
    def test_installed_1_3_2_1_refuses_1_3_2(self):
        self.registry(display_version="1.3.2.1")
        self.assertIn("1.3.2.1", self.assertDecision(DOWNGRADE, setup_version="1.3.2"))

    def test_newer_versions_are_refused(self):
        for installed in ("1.3.3", "1.4.0", "2.0.0", "1.3.10", "9.9.9"):
            with self.subTest(installed=installed):
                self.registry(display_version=installed)
                self.assertDecision(DOWNGRADE)

    def test_a_newer_version_in_the_32_bit_view_alone_is_refused(self):
        self.registry(view=1, display_version="1.3.3")
        self.assertDecision(DOWNGRADE)

    def test_newer_data_after_an_uninstall_is_refused(self):
        self.vms_env("ANYAICAM_VERSION=1.3.3")         # older setup over newer, kept data
        self.assertDecision(DOWNGRADE)

    def test_a_valid_newer_vms_env_refuses_even_with_an_older_uninstall_entry(self):
        self.registry(display_version="1.3.1")
        self.vms_env("ANYAICAM_VERSION=1.3.3")
        self.assertDecision(DOWNGRADE)

    # ------------------------------------------------------------------ refused: unknown / malformed
    def test_malformed_or_missing_installed_version_fails_safely(self):
        for value in ("", " ", "1.3.2-rc1", "1.3", "1.3.2.1.5", "v1.3.2", " 1.3.2", "1.3.2 ", "01.3.2", "1..2",
                      "1.3.x", "-1.3.2", "1.3.2+build", "1,3,2", "9999999999.0.0"):
            with self.subTest(value=value):
                self.registry(display_version=value)
                self.assertDecision(UNKNOWN)

    def test_an_uninstall_entry_without_any_display_version_fails_safely(self):
        self.registry(display_version=None)
        self.assertDecision(UNKNOWN)

    def test_one_good_and_one_malformed_view_fails_safely(self):
        self.registry(view=0, display_version="1.3.1")
        self.registry(view=1, display_version="garbage")
        self.assertDecision(UNKNOWN)

    def test_existing_data_without_a_valid_version_fails_safely(self):
        for line in (None, "ANYAICAM_VERSION=", "ANYAICAM_VERSION=1.3.2-rc1"):
            with self.subTest(line=line):
                self.vms_env(line)
                self.assertDecision(UNKNOWN)

    def test_unreadable_data_without_an_uninstall_entry_fails_safely(self):
        self.vms_env("ANYAICAM_VERSION=1.3.1", unreadable=True)
        self.assertIn("cannot be read", self.assertDecision(UNKNOWN))

    def test_program_files_without_any_version_fail_safely(self):
        self.program_files()
        self.assertDecision(UNKNOWN)

    def test_program_files_at_the_recorded_install_location_count(self):
        other = self.tmp / "D-drive" / "AnyAiCam"
        self.program_files(other)
        self.registry(display_version=None, install_location=str(other))
        self.assertDecision(UNKNOWN)

    def test_a_guard_that_cannot_run_prints_no_success_line(self):
        # Why Setup requires the "exit 0:" line and not only exit code 0: a script that fails to parse ends with
        # exit code 0 under -Command (and prints no decision line).
        broken = self.tmp / "broken-guard.ps1"
        broken.write_text(GUARD.read_text(encoding="utf-8").replace("${SetupVersion}: repair", "$SetupVersion: repair"), encoding="utf-8")
        result = self._ps(f"& {_ps_quote(str(broken))} -SetupVersion '1.3.2'; exit $LASTEXITCODE")
        self.assertNotIn("install-version-guard: exit 0:", result.stdout + result.stderr)

    # ------------------------------------------------------------------ the setup's own version
    def test_a_malformed_setup_version_is_refused(self):
        for setup in ("0.0.0-unset", "", "1.3", "1.3.2.1.5", "latest"):
            with self.subTest(setup=setup):
                self.assertDecision(BAD_SETUP, setup_version=setup)


ISCC = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 7" / "ISCC.exe"


@unittest.skipUnless(os.name == "nt" and ISCC.exists(), "needs the Inno Setup 7 compiler")
class PascalCompileTests(unittest.TestCase):
    def test_the_complete_code_section_compiles_with_inno_setup_7(self):
        """The real [Code] section (every function, including InitializeSetup and ExecAndCaptureOutput's types) in a
        minimal wrapper, compiled with /O- (syntax and types only; no setup file is produced)."""
        with tempfile.TemporaryDirectory(prefix="aac-iss-") as folder:
            wrapper = Path(folder) / "compile-check.iss"
            wrapper.write_text(
                '#define AppVersion "1.3.2"\n#define ReleaseVersion "1.2.7"\n#define SourceCommit "0000000000000000000000000000000000000000"\n'
                '#define KeysetSha256 "2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c"\n'
                "[Setup]\nAppId={{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}\nAppName=AnyAiCam VMS compile check\n"
                "AppVersion={#AppVersion}\nDefaultDirName={autopf}\\AnyAiCamCompileCheck\nPrivilegesRequired=admin\n"
                "ArchitecturesInstallIn64BitMode=x64compatible\nOutputDir=out\n"
                f'[Files]\nSource: "{GUARD}"; Flags: dontcopy\n[Code]' + CODE,
                encoding="utf-8")
            result = subprocess.run([str(ISCC), "/O-", "/Q", str(wrapper)], capture_output=True, text=True, cwd=folder, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(list(Path(folder).rglob("*.exe")))


class SetupScriptTests(unittest.TestCase):
    def initialize_setup(self):
        return CODE.split("function InitializeSetup(): Boolean;", 1)[1].split("\nend;\n", 1)[0]

    def test_the_guard_is_bundled_for_initialize_setup_only(self):
        self.assertRegex(ISS, r'(?m)^Source: "install-version-guard\.ps1"; Flags: dontcopy$')
        self.assertIn("ExtractTemporaryFile('install-version-guard.ps1');", self.initialize_setup())

    def test_initialize_setup_fails_closed(self):
        body = self.initialize_setup()
        # starts refused; only exit code 0 AND the guard's own success line turn it to True
        self.assertLess(body.index("Result := False;"), body.index("ExtractTemporaryFile"))
        self.assertLess(body.index("ResultCode := -1;"), body.index("ExecAndCaptureOutput"))
        self.assertLess(body.index("Allowed := False;"), body.index("ExecAndCaptureOutput"))
        self.assertIn("if Pos('install-version-guard: exit 0: ', Output.StdOut[I]) = 1 then Allowed := True;", body)
        self.assertIn("if (ResultCode = 0) and Allowed then begin\n    Result := True;", body)
        self.assertEqual(body.count("Result := True;"), 1)
        self.assertIn("    ResultCode := -1;", body.split("ExecAndCaptureOutput", 1)[1].split("for I := 0", 1)[0])
        self.assertIn("if ResultCode = 0 then ResultCode := -1;", body)   # exit 0 without the line -> generic refusal

    def test_the_guard_output_reaches_the_setup_log(self):
        body = self.initialize_setup()
        self.assertIn("Log(Output.StdOut[I]);", body)
        self.assertIn("Log(Output.StdErr[I]);", body)

    def test_the_guard_gets_the_setup_version_and_the_default_folder(self):
        body = self.initialize_setup()
        self.assertIn("{sys}\\WindowsPowerShell\\v1.0\\powershell.exe", body)
        self.assertIn('-SetupVersion "{#AppVersion}" -DefaultAppDir "', body)
        self.assertIn("ExpandConstant('{autopf}\\AnyAiCam')", body)
        self.assertNotIn("UninstallKeyPaths", ISS)                  # the test seam is never used by Setup

    def test_refusals_are_logged_and_explained(self):
        body = self.initialize_setup()
        self.assertIn("Log('Downgrade refused: a newer AnyAiCam VMS is installed", body)
        self.assertIn("Log('Install refused: the installed AnyAiCam VMS version is missing or not valid", body)
        self.assertIn("if not WizardSilent() then\n    MsgBox(Message, mbError, MB_OK);", body)

    def test_the_old_fail_open_comparison_is_gone(self):
        for removed in ("CompareAnyAiCamVersions", "InstalledAnyAiCamVersion", "if Installed = '' then exit;"):
            self.assertNotIn(removed, CODE)

    def test_the_guard_and_the_build_accept_the_same_version_forms(self):
        guard = re.search(r"-notmatch '([^']+)'", GUARD.read_text(encoding="utf-8")).group(1)
        build = re.search(r"\$Version -notmatch '([^']+)'", (WIN / "build.ps1").read_text(encoding="utf-8")).group(1)
        self.assertEqual(guard, build)
        for ok in ("1.3.2", "1.3.2.1", "0.0.1", "10.20.30.40"):
            self.assertRegex(ok, guard)
        for bad in ("1.3", "1.3.2-rc1", "01.3.2", "1.3.2.1.5", "0.0.0-unset"):
            self.assertNotRegex(bad, guard)

    def test_the_guard_has_no_override_switch(self):
        params = GUARD.read_text(encoding="utf-8").split("param(", 1)[1].split("\n)", 1)[0]
        names = re.findall(r"\$(\w+)", params)
        self.assertEqual(sorted(names), ["DefaultAppDir", "ProgramData", "SetupVersion", "UninstallKeyPaths"])


if __name__ == "__main__":
    unittest.main()
