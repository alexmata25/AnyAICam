"""Windows native installer (installer/windows, 2026-10-07): source-level checks
that run on any machine. They never install anything, change a firewall or
start a service; a clean Windows machine (Windows Sandbox) is the place for that.
"""
import importlib.util
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_restriction_touches_only_the_root_so_reinstalls_keep_files_readable(self):
        # 2026-10-07: with /T a reinstall left every existing file with an empty
        # permission list (second Sandbox run). Only the root is set; the
        # (OI)(CI) entries reach existing and new items by inheritance.
        restrict = [line for line in self.text.splitlines()
                    if line.startswith("& icacls.exe $DataRoot /inheritance:r")]
        self.assertEqual(restrict, ["& icacls.exe $DataRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /C /Q | Out-Null"])
        self.assertNotIn("/inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T", self.text)

    def test_damaged_install_is_repaired_before_vms_env_is_read(self):
        repair = self.text.index("[IO.File]::ReadAllBytes($environmentFile)")
        self.assertLess(repair, self.text.index("& takeown.exe /F $DataRoot /R /A /D Y"))
        self.assertLess(self.text.index("& takeown.exe /F $DataRoot /R /A /D Y"), self.text.index("& icacls.exe $DataRoot /reset /T /C /Q"))
        self.assertLess(self.text.index("& icacls.exe $DataRoot /reset /T /C /Q"), self.text.index("& icacls.exe $DataRoot /inheritance:r"))
        self.assertLess(self.text.index("& icacls.exe $DataRoot /inheritance:r"), self.text.index("foreach ($line in Get-Content -LiteralPath $environmentFile)"))
        self.assertIn("if (-not $readable) {", self.text)  # healthy installs skip the repair
        for failure in ("(takeown exit $LASTEXITCODE)", "Could not repair permissions under $DataRoot (icacls exit $LASTEXITCODE)"):
            self.assertIn(failure, self.text)  # a failed repair stops setup

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
                                       "ffmpeg-8.1.2-essentials_build.zip", "mediamtx_v1.21.0_windows_amd64.zip",
                                       "vc_redist.x64-14.44.35211.exe"})
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


class VcRuntimeTests(unittest.TestCase):
    """2026-10-07: a clean Windows has no Visual C++ runtime, so torch's DLLs
    failed to load and the service crash-looped (first Sandbox run)."""

    REDIST = "vc_redist.x64-14.44.35211.exe"

    def setUp(self):
        self.iss = read("AnyAiCam-VMS.iss")
        self.build = read("build.ps1")

    def test_redistributable_is_bundled_offline_and_signature_checked(self):
        self.assertIn(f'Source: "vendor\\{self.REDIST}"; DestDir: "{{tmp}}"; Flags: deleteafterinstall', self.iss)
        # Offline: nothing is downloaded during a customer install.
        for download in ("downloadtemporaryfile", "createdownloadpage", "idpadd", "invoke-webrequest", "bitsadmin"):
            self.assertNotIn(download, self.iss.lower())
        files = self.iss.split("[Files]", 1)[1].split("\n[", 1)[0]
        self.assertNotRegex(files.lower(), r"https?://")
        self.assertIn(f"Get-AuthenticodeSignature -LiteralPath (Join-Path $vendor '{self.REDIST}')", self.build)
        self.assertIn("'^CN=Microsoft Corporation,'", self.build)
        # The hash check loop runs before the signature check.
        self.assertLess(self.build.index("Vendor checksum failed"), self.build.index("Get-AuthenticodeSignature -LiteralPath (Join-Path $vendor"))

    def test_quiet_install_accepts_only_microsofts_success_codes(self):
        code = self.iss.split("procedure InstallVcRuntime;", 1)[1].split("function NeedRestart", 1)[0]
        self.assertIn(f"ExpandConstant('{{tmp}}\\{self.REDIST}'), '/install /quiet /norestart'", code)
        self.assertIn("0, 1638: ;", code)
        self.assertIn("3010: VcRuntimeNeedsRestart := True;", code)
        self.assertIn("else\n    RaiseException('Installing the Microsoft Visual C++ runtime failed", code)
        self.assertIn("Result := VcRuntimeNeedsRestart;", self.iss)

    def test_order_is_vc_runtime_then_python_runtime_then_service(self):
        steps = self.iss.split("procedure CurStepChanged", 1)[1]
        order = [steps.index(s) for s in ("InstallVcRuntime;", "ExpandConstant('{app}\\installer\\install-runtime.ps1')","'install', 'Installing the AnyAiCam Windows service", "'start', 'Starting")]
        self.assertEqual(order, sorted(order))

    def test_preflight_runs_after_packages_and_before_firewall(self):
        runtime = read("install-runtime.ps1")
        self.assertIn('Source: "runtime-preflight.py"; DestDir: "{app}\\installer"', self.iss)
        preflight = runtime.index("installer\\runtime-preflight.py')")
        self.assertLess(runtime.index("requirements-windows.txt"), preflight)
        self.assertLess(preflight, runtime.index("firewall.ps1"))
        self.assertIn("if ($LASTEXITCODE -ne 0) { throw 'AnyAiCam runtime preflight failed", runtime)

    def test_a_failed_post_install_step_fails_the_exit_code(self):
        # Inno ends with 0 after an exception in ssPostInstall (second Sandbox run).
        self.assertIn("const PostInstallFailedExitCode = 20;", self.iss)
        self.assertIn("function GetCustomSetupExitCode(): Integer;", self.iss)
        steps = self.iss.split("procedure CurStepChanged", 1)[1]
        self.assertIn("if CurStep = ssPostInstall then\n  try\n", steps)
        handler = steps.split("  except\n", 1)[1]
        self.assertIn("PostInstallFailed := True;", handler)
        self.assertIn("RaiseException(GetExceptionMessage);", handler)  # still shown to the user
        self.assertLess(steps.index("'start', 'Starting"), steps.index("  except\n"))

    def test_failures_reach_the_setup_log(self):
        self.assertIn("ExecAndLogOutput(FileName, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode, nil)", self.iss)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("runtime_preflight", WIN / "runtime-preflight.py")
        self.preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.preflight)

    def test_covers_what_main_imports_at_startup(self):
        self.assertTrue({"torch", "cv2", "ultralytics", "fastapi", "uvicorn"} <= set(self.preflight.REQUIRED_MODULES))

    def test_passes_when_everything_loads(self):
        self.assertEqual(self.preflight.check(("json", "sqlite3")), [])

    def test_reports_each_module_that_fails(self):
        failures = self.preflight.check(("json", "anyaicam_missing_module_xyz"))
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith("anyaicam_missing_module_xyz: ModuleNotFoundError"))

    def test_a_dll_load_error_is_a_failure_not_a_crash(self):
        def broken(name):
            raise OSError("[WinError 126] The specified module could not be found. Error loading c10.dll")
        with mock.patch.object(self.preflight.importlib, "import_module", broken):
            failures = self.preflight.check(("torch",))
        self.assertEqual(failures, ["torch: OSError: [WinError 126] The specified module could not be found. Error loading c10.dll"])

    def test_exits_nonzero_and_names_the_failure(self):
        script = ("import importlib.util,sys;"
                  f"s=importlib.util.spec_from_file_location('p',r'{WIN / 'runtime-preflight.py'}');"
                  "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                  "m.REQUIRED_MODULES=('anyaicam_missing_module_xyz',);sys.exit(m.main())")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AnyAiCam runtime preflight FAILED", result.stdout)
        self.assertIn("anyaicam_missing_module_xyz", result.stdout)


class CloudAgentServiceTests(unittest.TestCase):
    """2026-10-07: the cloud agent (appliance-agent) runs as AnyAiCamAgent next
    to AnyAiCamVMS and links the PC with the installer's claim code."""

    def setUp(self):
        self.iss = read("AnyAiCam-VMS.iss")
        self.runtime = read("install-runtime.ps1")

    def test_agent_package_and_service_files_are_shipped(self):
        self.assertIn('Source: "..\\..\\appliance-agent\\anyaicam_agent\\*"; DestDir: "{app}\\agent\\anyaicam_agent"; Excludes: "__pycache__\\*"', self.iss)
        for line in ('Source: "agent-main.py"; DestDir: "{app}\\agent"', 'Source: "agent-launcher.ps1"; DestDir: "{app}\\service"',
                     'Source: "AnyAiCamAgent.xml"; DestDir: "{app}\\service"', 'Source: "cloud-link.ps1"; DestDir: "{app}\\installer"',
                     'Source: "vendor\\WinSW-x64.exe"; DestDir: "{app}\\service"; DestName: "AnyAiCamAgent.exe"'):
            self.assertIn(line, self.iss)
        self.assertIn('Type: filesandordirs; Name: "{app}\\agent"', self.iss.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0])
        for folder in ("agent", "label"):
            self.assertIn(f'Name: "{{commonappdata}}\\AnyAiCam\\{folder}"; Flags: uninsneveruninstall', self.iss)

    def test_agent_service_starts_after_the_vms_and_is_removed_before_it(self):
        steps = self.iss.split("procedure CurStepChanged", 1)[1]
        order = [steps.index(s) for s in ("'start', 'Starting the AnyAiCam Windows service", "AnyAiCamAgent.exe'), 'install'", "AnyAiCamAgent.exe'), 'start'")]
        self.assertEqual(order, sorted(order))
        self.assertLess(steps.index("AnyAiCamAgent.exe'), 'start'"), steps.index("  except\n"))  # a failure still exits 20
        prepare = self.iss.split("function PrepareToInstall", 1)[1].split("end;\n\n", 1)[0]
        self.assertLess(prepare.index("AnyAiCamAgent.exe"), prepare.index("AnyAiCamVMS.exe"))
        uninstall = self.iss.split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0]
        self.assertLess(uninstall.index('AnyAiCamAgent.exe"; Parameters: "uninstall"'), uninstall.index('AnyAiCamVMS.exe"; Parameters: "stopwait"'))

    def test_services_are_stopped_with_stopwait_before_their_files_are_replaced(self):
        # WinSW `stop` returns before the service stops; the 1.3.0-4a90e4c
        # Sandbox reinstall aborted (exit 5) replacing a running AnyAiCamAgent.exe.
        prepare = self.iss.split("function PrepareToInstall", 1)[1].split("end;\n\n", 1)[0]
        uninstall = self.iss.split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0]
        self.assertEqual(prepare.count("'stopwait'"), 2)
        self.assertNotIn("'stop'", prepare)
        self.assertEqual(uninstall.count('Parameters: "stopwait"'), 2)
        self.assertNotIn('Parameters: "stop"', uninstall)

    def test_agent_service_definition(self):
        xml = read("AnyAiCamAgent.xml")
        self.assertIn("<startmode>Automatic</startmode>", xml)
        self.assertIn('<onfailure action="restart"', xml)
        self.assertIn('agent-launcher.ps1', xml)
        # No dependency on AnyAiCamVMS: restarting the VMS on the cloud's
        # request would otherwise stop the agent that asked for it.
        self.assertNotIn("<depend>", xml)

    def test_launcher_uses_the_vms_folders_and_its_own_python(self):
        launcher = read("agent-launcher.ps1")
        for line in ("$env:ANYAICAM_CONFIG_DIR = $config", "$env:ANYAICAM_STATE_DIR = Join-Path $dataRoot 'agent'",
                     "$env:ANYAICAM_VMS_RECORDINGS_PATH = Join-Path $dataRoot 'recordings'", "$env:ANYAICAM_VMS_HLS_PATH = Join-Path $dataRoot 'hls'",
                     "$env:ANYAICAM_VMS_RELEASE_MARKER = Join-Path $config 'vms_release.json'",
                     "& (Join-Path $installRoot 'runtime\\python\\python.exe') (Join-Path $installRoot 'agent\\agent-main.py')"):
            self.assertIn(line, launcher)
        self.assertIn("@('ANYAICAM_PORTAL_URL', 'ANYAICAM_AGENT_MODE')", launcher)  # only these keys from agent.env

    def test_cloud_link_and_agent_preflight_run_before_any_service(self):
        self.assertIn("-PortalUrl \"' + ExpandConstant('{param:PortalUrl|}') + '\"'", self.iss)
        agent_preflight = self.runtime.index("import anyaicam_agent.service, anyaicam_agent.windows")
        self.assertLess(self.runtime.index("installer\\runtime-preflight.py')"), agent_preflight)
        self.assertIn("throw 'AnyAiCam agent preflight failed", self.runtime)
        cloud_link = self.runtime.index("installer\\cloud-link.ps1') -DataRoot $DataRoot -AppVersion $AppVersion -SourceCommit $SourceCommit -PortalUrl $PortalUrl")
        self.assertLess(self.runtime.index("[IO.File]::WriteAllLines($environmentFile"), cloud_link)
        self.assertLess(cloud_link, self.runtime.index("installer\\firewall.ps1') -Apply"))

    def test_claim_code_is_shown_but_never_logged(self):
        code = self.iss.split("// ------------------------------------------------------------ claim code", 1)[1]
        self.assertNotIn("Log(", code)
        self.assertIn("WizardForm.FinishedLabel.Caption", code)
        self.assertIn('Filename: "{code:ClaimLink}"; Description: "Link this computer to my AnyAiCam account"; Flags: postinstall shellexec skipifsilent; Check: HasClaimLink', self.iss)
        self.assertIn("-Verb RunAs -ArgumentList '{commonappdata}\\AnyAiCam\\label\\claim-label.txt'", self.iss)
        link = read("cloud-link.ps1")
        for statement in ("Write-Host", "Write-Output $code", "Write-Verbose"):
            self.assertNotIn(statement, link)


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
