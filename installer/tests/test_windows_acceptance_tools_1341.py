"""Windows 1.3.4.1 installer acceptance tools (Blue, 2026-10-28): installer/windows/acceptance/ + the size policy.

What runs here (any Windows PC, no elevation, never touching an installed VMS except read-only):
* A1 Tesseract: the tool against a MOCK engine in a temporary install (PASS, altered file, missing model, missing install).
* A3 SAPI: the PRODUCT's synthesize_speech_sapi through the tool as the current user (real SAPI when a voice exists).
* A4 Job Object: Simulate mode for real (vms-job-limits.ps1, Windows job API), plus a mis-configuration it must catch.
* A5 catalog coverage: a REAL catalog built by runtime-catalog.ps1's New-RuntimeCatalog (makecat) over a temp stage;
  PASS, an uncatalogued native file, a pip launcher, an OS-signed file; and read-only on the installed VMS if present.
* A6 lifecycle: compare rules on synthetic snapshots (upgrade/repair/uninstall, pass and fail); read-only snapshot.
* A7 size policy: the 750,000,000-byte constant, the function, and its place in build.ps1.
LocalSystem and WinSW-service modes are R430 work: here they must report BLOCKED, never PASS.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
ACC = WIN / "acceptance"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
MAKECAT = Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makecat.exe")
INSTALLED = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AnyAiCam"
ON_WINDOWS = os.name == "nt" and POWERSHELL


def _ps_file(script: Path, *args, timeout=300):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, args)],
                          capture_output=True, text=True, timeout=timeout)


def _ps(command: str, timeout=120):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, text=True, timeout=timeout)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _is_admin() -> bool:
    return _ps("([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("
               "[Security.Principal.WindowsBuiltInRole]::Administrator)").stdout.strip() == "True"


class StaticTests(unittest.TestCase):
    def test_the_tesseract_probe_uses_the_launchers_exact_environment(self):
        launcher = (WIN / "service-launcher.ps1").read_text(encoding="utf-8")
        common = (ACC / "acceptance-common.ps1").read_text(encoding="utf-8")
        self.assertIn("$env:PATH = (Join-Path $installRoot 'runtime\\tools\\tesseract') + ';' + $env:PATH", launcher)
        self.assertIn("$env:TESSDATA_PREFIX = Join-Path $installRoot 'runtime\\tools\\tesseract\\tessdata'", launcher)
        self.assertIn("Path = (Join-Path $InstallRoot 'runtime\\tools\\tesseract')", common)
        self.assertIn("TessdataPrefix = (Join-Path $InstallRoot 'runtime\\tools\\tesseract\\tessdata')", common)

    def test_the_coverage_verifier_reuses_the_build_extensions_and_publisher_pin(self):
        tool = (ACC / "Test-RuntimeCatalogCoverage.ps1").read_text(encoding="utf-8")
        self.assertIn(". (Join-Path $PSScriptRoot '..\\runtime-catalog.ps1')", tool)
        self.assertIn(". (Join-Path $PSScriptRoot '..\\publisher-pin.ps1')", tool)
        self.assertIn("$NativeExtensions -contains", tool)
        self.assertNotIn("Select-Object -First 1 }", tool.split("$files =")[1].split("foreach")[0])  # every file, no sample

    def test_the_size_budget_is_750_mb_and_enforced_right_after_inno_setup(self):
        # 1.3.4.1 reconciliation: the single gate is installer-size.ps1 (R430 line); installer-size-policy.ps1 removed.
        self.assertFalse((WIN / "installer-size-policy.ps1").exists())
        gate_script = (WIN / "installer-size.ps1").read_text(encoding="utf-8")
        self.assertIn("[ValidateRange(1,750000000)][long]$MaxBytes = 750000000", gate_script)
        build = (WIN / "build.ps1").read_text(encoding="utf-8")
        iscc = build.index("& $iscc @compilerArguments")
        gate = build.index("Assert-InstallerSize $setupPath")
        self.assertLess(iscc, gate)
        self.assertLess(gate, build.index("$setup = Get-Item -LiteralPath $setupPath"))  # before signing verification
        self.assertNotIn("-MaxBytes", build)  # the build never overrides the owner's budget

    def test_the_tools_are_not_packaged_into_the_setup(self):
        sources = [line for line in (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8").splitlines() if line.startswith("Source:")]
        self.assertTrue(sources)
        self.assertFalse([line for line in sources if "acceptance" in line.lower() or "installer-size" in line])

    def test_vms_env_secrets_are_never_recorded(self):
        snapshot = (ACC / "Get-AnyAiCamInstallState.ps1").read_text(encoding="utf-8")
        allow = snapshot.split("$PublicEnvKeys = @(")[1].split(")")[0]
        for secret in ("ANYAICAM_CAMERA_CREDENTIAL_KEY", "SECRET", "PASSWORD", "TOKEN"):
            self.assertNotIn(secret, allow)
        self.assertIn("if ($PublicEnvKeys -contains $key) { $public[$key] = $value }", snapshot)


@unittest.skipUnless(ON_WINDOWS, "PowerShell is required")
class SizePolicyTests(unittest.TestCase):
    """The coverage the removed installer-size-policy.ps1 had, kept against installer-size.ps1 (Assert-InstallerSize).
    Boundary cases 0 / at / over the limit are also in test_windows_installer_size.py."""

    GATE = WIN / "installer-size.ps1"

    def _assert(self, size, limit=None, path=None):
        with tempfile.TemporaryDirectory() as folder:
            if path is None:
                path = Path(folder, "AnyAiCam-VMS-Setup-test.exe")
                with open(path, "wb") as handle:
                    handle.truncate(size)
            limit_arg = f" -MaxBytes {limit}" if limit is not None else ""
            return _ps(f". '{self.GATE}'; Assert-InstallerSize '{path}'{limit_arg}")

    def test_at_and_under_the_budget_pass(self):
        self.assertEqual(self._assert(1000, 1000).returncode, 0)
        self.assertEqual(self._assert(999, 1000).returncode, 0)

    def test_one_byte_over_fails_and_names_the_size(self):
        result = self._assert(1001, 1000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1001 bytes, allowed 1..1000", result.stdout + result.stderr)

    def test_the_default_limit_is_the_owner_budget_when_executed(self):
        out = _ps(f". '{self.GATE}'; (Get-Command Assert-InstallerSize).Parameters['MaxBytes'].Attributes | "
                  "Where-Object { $_ -is [System.Management.Automation.ValidateRangeAttribute] } | ForEach-Object { $_.MaxRange }").stdout.strip()
        self.assertEqual(out, "750000000")
        self.assertEqual(self._assert(750000000 + 1, 750000001).returncode != 0, True)  # a caller cannot raise the budget

    def test_a_missing_setup_fails_not_passes(self):
        self.assertNotEqual(self._assert(0, path="C:\\nope\\setup.exe").returncode, 0)

    def test_an_empty_setup_fails(self):
        self.assertNotEqual(self._assert(0).returncode, 0)


MOCK_TESSERACT = """@echo off
if "%1"=="--version" (echo tesseract 5.5.2& echo  leptonica-1.87.0& exit /b 0)
if "%1"=="--list-langs" (echo List of available languages in "%TESSDATA_PREFIX%/" (2^):& for %%f in ("%TESSDATA_PREFIX%\\*.traineddata") do echo %%~nf& exit /b 0)
echo ABC1234
exit /b 0
"""


@unittest.skipUnless(ON_WINDOWS, "PowerShell is required")
class TesseractToolTests(unittest.TestCase):
    """A1 against a mock engine: proves the tool's logic, NOT the real engine as LocalSystem (R430)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aac-a1-"))
        self.root = self.tmp / "AnyAiCam"
        engine = self.root / "runtime" / "tools" / "tesseract"
        (engine / "tessdata").mkdir(parents=True)
        files = {"tesseract.cmd": MOCK_TESSERACT.encode("ascii"), "tessdata/eng.traineddata": b"model"}
        manifest = {"files": {}}
        import hashlib
        for name, blob in files.items():
            (engine / name).write_bytes(blob)
            manifest["files"][f"tesseract/{name}"] = {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}
        (engine / "BUNDLE-MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "licenses" / "tesseract" / "tesseract").mkdir(parents=True)
        (self.root / "licenses" / "tesseract" / "tesseract" / "LICENSE").write_text("Apache-2.0")
        self.engine = engine

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra):
        result = _ps_file(ACC / "Test-TesseractLocalSystem.ps1", "-InstallRoot", self.root, "-RunAs", "CurrentUser", "-OutputDir", self.tmp / "out", *extra)
        report = self.tmp / "out" / "A1-tesseract-localsystem.json"
        return result, (_json(report) if report.exists() else None)

    def test_a_good_install_passes(self):
        result, report = self._run("-Image", self.tmp / "plate.png", "-Expected", "ABC1234")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["probe"]["languages"], ["eng"])

    def test_an_altered_engine_file_fails(self):
        (self.engine / "tesseract.cmd").write_text(MOCK_TESSERACT + "rem tampered\n", encoding="ascii")
        result, report = self._run()
        self.assertEqual(result.returncode, 1)
        failed = [c["name"] for c in report["checks"] if not c["passed"]]
        self.assertIn("installed engine files match the bundle manifest", failed)

    def test_a_missing_language_model_fails(self):
        (self.engine / "tessdata" / "eng.traineddata").unlink()
        result, report = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("English model found (--list-langs has eng)", [c["name"] for c in report["checks"] if not c["passed"]])

    def test_no_install_is_blocked_not_passed(self):
        shutil.rmtree(self.root / "runtime")
        result, report = self._run()
        self.assertEqual((result.returncode, report["result"]), (2, "BLOCKED"))

    @unittest.skipIf(os.name == "nt" and _is_admin() if ON_WINDOWS else True, "elevated session: LocalSystem mode could run")
    def test_localsystem_without_elevation_is_blocked(self):
        result, report = _ps_file(ACC / "Test-TesseractLocalSystem.ps1", "-InstallRoot", self.root, "-OutputDir", self.tmp / "ls"), None
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("elevated", result.stdout)


@unittest.skipUnless(ON_WINDOWS, "Windows PowerShell is required")
class PythonLauncherCleanupTests(unittest.TestCase):
    """Generated pip console launchers are removable without changing Python packages."""

    def _remove(self, python_root):
        script = WIN / "python-launchers.ps1"
        command = f". '{script}'; Remove-AnyAiCamGeneratedPythonLaunchers '{python_root}'"
        return _ps(command)

    def test_removes_only_top_level_exe_launchers(self):
        with tempfile.TemporaryDirectory(prefix="aac-python-launchers-") as folder:
            root = Path(folder, "runtime", "python")
            scripts = root / "Scripts"
            nested = scripts / "nested"
            nested.mkdir(parents=True)
            (scripts / "uvicorn.exe").write_bytes(b"launcher")
            (scripts / "uvicorn-script.py").write_text("import uvicorn\n", encoding="ascii")
            (scripts / "uvicorn.cmd").write_text("@echo off\n", encoding="ascii")
            (nested / "tool.exe").write_bytes(b"unrelated nested fixture")
            result = self._remove(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1", result.stdout)
            self.assertFalse((scripts / "uvicorn.exe").exists())
            self.assertTrue((scripts / "uvicorn-script.py").is_file())
            self.assertTrue((scripts / "uvicorn.cmd").is_file())
            self.assertTrue((nested / "tool.exe").is_file())

    def test_missing_scripts_folder_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="aac-python-launchers-") as folder:
            root = Path(folder, "runtime", "python")
            root.mkdir(parents=True)
            result = self._remove(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("0", result.stdout)

    def test_install_invokes_cleanup_after_pip_and_before_preflight(self):
        install = (WIN / "install-runtime.ps1").read_text(encoding="utf-8")
        iss = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
        pip = install.index("pip install --no-index")
        cleanup = install.index("Remove-AnyAiCamGeneratedPythonLaunchers")
        preflight = install.index("runtime-preflight.py")
        self.assertLess(pip, cleanup)
        self.assertLess(cleanup, preflight)
        self.assertIn('Source: "python-launchers.ps1"; DestDir: "{app}\\installer"', iss)


@unittest.skipUnless(ON_WINDOWS, "PowerShell is required")
class SapiToolTests(unittest.TestCase):
    def test_the_product_greeting_through_the_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            result = _ps_file(ACC / "Test-SapiLocalSystem.ps1", "-RunAs", "CurrentUser", "-PythonExe", sys.executable,
                              "-AppDir", ROOT / "app", "-OutputDir", folder, timeout=400)
            report = _json(Path(folder, "A3-sapi-greeting-localsystem.json"))
            if not report.get("probe", {}).get("voices"):
                self.skipTest("no SAPI voice on this PC")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(report["probe"]["audio"]["rate"], 22050)

    def test_a_build_without_the_sapi_greeting_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder, "app"); app.mkdir()
            (app / "aac_voice_call_greeting.py").write_text("def synthesize_speech(text):\n    pass\n")
            result = _ps_file(ACC / "Test-SapiLocalSystem.ps1", "-RunAs", "CurrentUser", "-PythonExe", sys.executable, "-AppDir", app, "-OutputDir", folder)
            self.assertEqual(result.returncode, 2)
            self.assertIn("no SAPI greeting", result.stdout)


@unittest.skipUnless(ON_WINDOWS, "PowerShell is required")
class JobObjectToolTests(unittest.TestCase):
    def test_simulate_passes_with_the_real_job_limits_script(self):
        with tempfile.TemporaryDirectory() as folder:
            result = _ps_file(ACC / "Test-VmsJobObject.ps1", "-Mode", "Simulate", "-CpuPercent", "60", "-MemoryMB", "2048", "-OutputDir", folder)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = _json(Path(folder, "A4-vms-job-object.json"))
            self.assertEqual(report["probe"]["job_limits"], "cpu=60;memoryMB=2048")
            self.assertEqual(report["probe"]["breakaway_error"], 5)

    def test_simulate_catches_a_launcher_that_applies_no_job(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder, "vms-job-limits.ps1")
            broken.write_text("function Set-AnyAiCamVmsJobLimits([hashtable]$Settings) { 'AnyAiCam VMS launcher: resource limits NOT applied, starting without them: test' }\n")
            result = _ps_file(ACC / "Test-VmsJobObject.ps1", "-Mode", "Simulate", "-JobLimitsScript", broken, "-OutputDir", folder)
            self.assertEqual(result.returncode, 1, result.stdout)
            failed = [c["name"] for c in _json(Path(folder, "A4-vms-job-object.json"))["checks"] if not c["passed"]]
            self.assertIn("child (Python stand-in) inherited the job", failed)

    @unittest.skipIf(ON_WINDOWS and _is_admin(), "elevated session")
    def test_service_mode_without_elevation_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            result = _ps_file(ACC / "Test-VmsJobObject.ps1", "-Mode", "Service", "-OutputDir", folder)
            self.assertEqual(result.returncode, 2, result.stdout)


@unittest.skipUnless(ON_WINDOWS and MAKECAT.exists(), "PowerShell and the Windows SDK makecat are required")
class CatalogCoverageToolTests(unittest.TestCase):
    """A5 against a REAL catalog made by runtime-catalog.ps1's New-RuntimeCatalog (unsigned: -AllowUnsignedCatalog)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aac-a5-"))
        self.install = self.tmp / "install"
        dlls = (sorted(Path(sys.base_prefix, "DLLs").glob("*.pyd")) or sorted(Path(sys.base_prefix).glob("*.pyd")))[:4]
        self.assertGreaterEqual(len(dlls), 3)
        for i, source in enumerate(dlls):
            target = self.install / "runtime" / "python" / "Lib" / f"m{i}" / source.name
            target.parent.mkdir(parents=True)
            shutil.copyfile(source, target)
        stage = self.tmp / "stage"
        shutil.copytree(self.install, stage)
        self.catalog = self.tmp / "AnyAiCamRuntime.cat"
        result = _ps(f". '{WIN / 'runtime-catalog.ps1'}'; New-RuntimeCatalog '{stage}' '{self.catalog}' '{MAKECAT}'")
        self.assertTrue(self.catalog.exists(), result.stdout + result.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra):
        result = _ps_file(ACC / "Test-RuntimeCatalogCoverage.ps1", "-InstallRoot", self.install, "-Catalog", self.catalog,
                          "-AllowUnsignedCatalog", "-OutputDir", self.tmp / "out", *extra)
        return result, _json(self.tmp / "out" / "A5-runtime-catalog-coverage.json")

    def test_every_catalogued_file_passes(self):
        result, report = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report["counts"]["in_catalog"], report["counts"]["native_files"])

    def test_one_uncatalogued_native_file_fails_and_is_named(self):
        extra = self.install / "runtime" / "tools" / "new" / "unlisted.dll"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"MZ" + b"\0" * 512)
        result, report = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["uncovered"], [r"runtime\tools\new\unlisted.dll"])

    def test_pip_launchers_are_their_own_category(self):
        launcher = self.install / "runtime" / "python" / "Scripts" / "pip.exe"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"MZ launcher + script")
        result, report = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["generated_launchers"], [r"runtime\python\Scripts\pip.exe"])
        self.assertEqual(report["uncovered"], [])
        allowed, _ = self._run("-AllowGeneratedLaunchers")
        self.assertEqual(allowed.returncode, 0, allowed.stdout)

    def test_a_signed_file_outside_the_catalog_is_accepted(self):
        shutil.copyfile(Path(os.environ["SystemRoot"], "System32", "PING.EXE"), self.install / "runtime" / "PING.EXE")
        result, report = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(report["embedded"]), 1)

    def test_a_tampered_catalogued_file_is_no_longer_covered(self):
        target = next(self.install.rglob("*.pyd"))
        # Inside the image (the catalog's Authenticode hash ignores an embedded signature block at the end).
        data = bytearray(target.read_bytes()); data[len(data) // 2] ^= 0xFF
        target.write_bytes(bytes(data))
        result, report = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(report["uncovered"]), 1)


@unittest.skipUnless(ON_WINDOWS and (INSTALLED / "runtime").exists(), "no installed AnyAiCam VMS on this PC")
class InstalledReadOnlyTests(unittest.TestCase):
    """Read-only checks of the VMS installed on this PC (the Dell: the accepted signed 1.3.4)."""

    def test_every_installed_native_file_is_catalogued_or_signed_except_pip_launchers(self):
        with tempfile.TemporaryDirectory() as folder:
            result = _ps_file(ACC / "Test-RuntimeCatalogCoverage.ps1", "-OutputDir", folder, "-AllowGeneratedLaunchers", timeout=900)
            report = _json(Path(folder, "A5-runtime-catalog-coverage.json"))
            self.assertEqual(report["uncovered"], [], report["uncovered"][:10])
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_snapshot_is_read_only_and_never_records_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder, "state.json")
            result = _ps_file(ACC / "Get-AnyAiCamInstallState.ps1", "-OutputPath", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = _json(out)
            self.assertTrue(state["registered"])
            text = out.read_text(encoding="utf-8-sig")
            self.assertNotIn("CREDENTIAL_KEY=", text)


BASE_STATE = {
    "computer": "SYNTHETIC-WINDOWS", "install_root": r"C:\Program Files\AnyAiCam", "data_root": r"C:\ProgramData\AnyAiCam",
    "registered": True, "version": "1.3.4", "program_folder_exists": True,
    "services": {name: {"state": "Running", "start_mode": "Auto", "account": "LocalSystem", "path": "x"} for name in ("AnyAiCamVMS", "AnyAiCamAgent")},
    "program_file_list": ["app\\main.py", "service\\service-launcher.ps1"], "scripts": {"service\\service-launcher.ps1": "aa"},
    "tesseract_manifest": None, "runtime_catalog": "cat1", "firewall_rules": ["AnyAiCam-VMS-Web|True", "AnyAiCam-VMS-WebRTC|True"],
    "data": {"readable": True, "vms_env_keys": ["ANYAICAM_BUILD_ID", "ANYAICAM_CAMERA_CREDENTIAL_KEY", "ANYAICAM_LPR_ENABLED"],
             "vms_env_public": {"ANYAICAM_BUILD_ID": "e3d7eda", "ANYAICAM_LPR_ENABLED": "false"},
             "identity_sha256": {"agent\\credential.json": "c1"}, "database": {"bytes": 10},
             "recordings": {"count": 5, "bytes": 100}, "folders": ["agent", "config", "database", "recordings"]},
}


def _upgraded():
    after = copy.deepcopy(BASE_STATE)
    after.update(version="1.3.4.1", tesseract_manifest="m1", runtime_catalog="cat2")
    after["data"]["vms_env_public"]["ANYAICAM_BUILD_ID"] = "f00ba12"
    return after


def _uninstalled():
    after = copy.deepcopy(BASE_STATE)
    after.update(registered=False, version=None, program_folder_exists=False, runtime_catalog=None, firewall_rules=[],
                 services={"AnyAiCamVMS": None, "AnyAiCamAgent": None})
    return after


@unittest.skipUnless(ON_WINDOWS, "PowerShell is required")
class LifecycleCompareTests(unittest.TestCase):
    def _compare(self, before, after, step, *extra):
        with tempfile.TemporaryDirectory() as folder:
            b, a = Path(folder, "b.json"), Path(folder, "a.json")
            b.write_text(json.dumps(before)); a.write_text(json.dumps(after))
            result = _ps_file(ACC / "Compare-AnyAiCamInstallState.ps1", "-Before", b, "-After", a, "-Step", step, *extra)
            report = _json(Path(folder, f"a.compare-{step.lower()}.json"))
            return result.returncode, [c["name"] for c in report.get("checks", []) if not c["passed"]], report

    def test_a_good_upgrade_passes(self):
        code, failed, _ = self._compare(BASE_STATE, _upgraded(), "Upgrade", "-ExpectedVersion", "1.3.4.1")
        self.assertEqual((code, failed), (0, []))

    def test_an_upgrade_that_loses_the_cloud_link_or_the_lpr_choice_fails(self):
        after = _upgraded()
        after["data"]["identity_sha256"]["agent\\credential.json"] = "c2"
        after["data"]["vms_env_public"]["ANYAICAM_LPR_ENABLED"] = "true"
        code, failed, _ = self._compare(BASE_STATE, after, "Upgrade")
        self.assertEqual(code, 1)
        self.assertIn("identity file unchanged: agent\\credential.json", failed)
        self.assertIn("an explicit LPR choice is kept", failed)

    def test_a_good_repair_passes_and_a_lost_file_fails(self):
        self.assertEqual(self._compare(BASE_STATE, copy.deepcopy(BASE_STATE), "Repair")[:2], (0, []))
        after = copy.deepcopy(BASE_STATE); after["program_file_list"] = ["app\\main.py"]
        code, failed, _ = self._compare(BASE_STATE, after, "Repair")
        self.assertEqual(code, 1)
        self.assertIn("no program file lost", failed)

    def test_a_good_uninstall_keeps_data_and_removes_everything_else(self):
        self.assertEqual(self._compare(BASE_STATE, _uninstalled(), "Uninstall")[:2], (0, []))

    def test_an_uninstall_that_leaves_the_catalog_or_deletes_recordings_fails(self):
        after = _uninstalled(); after["runtime_catalog"] = "cat1"; after["data"]["recordings"]["count"] = 0
        code, failed, _ = self._compare(BASE_STATE, after, "Uninstall")
        self.assertEqual(code, 1)
        self.assertIn("runtime catalog removed", failed)
        self.assertIn("recordings kept (count not lower)", failed)

    def test_unreadable_data_is_blocked_not_passed(self):
        after = copy.deepcopy(BASE_STATE); after["data"] = {"readable": False, "reason": "access denied"}
        code, _, report = self._compare(BASE_STATE, after, "Repair")
        self.assertEqual((code, report["result"]), (2, "BLOCKED"))


if __name__ == "__main__":
    unittest.main()
