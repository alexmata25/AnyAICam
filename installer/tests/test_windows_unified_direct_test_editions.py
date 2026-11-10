"""Unified editions + Codex isolated direct-Dell profile (merge 2026-11-10).

The normal setup keeps the Gateway edition page and checks. The direct-test build shows no pages, accepts only an
exact edition name from /GatewayEdition= (default full_vms), passes it to install-runtime.ps1, and stays Local with
every transport off whatever the edition. build.ps1 keeps ONE keyset + trust-registry step, after the fresh output
directory exists.
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


def text(name):
    return (WIN / name).read_text(encoding="utf-8")


def section(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


class DirectTestEditionSourceTests(unittest.TestCase):
    def test_the_direct_build_creates_no_wizard_pages_and_the_normal_build_keeps_the_edition_page(self):
        wizard = section(text("AnyAiCam-VMS.iss"), "procedure InitializeWizard;", "\nend;")
        direct, normal = wizard.split("#else", 1)
        self.assertIn("#ifdef DirectDellTestCandidate", direct)
        self.assertIn("exit;", direct)
        self.assertNotIn("GatewayEditionPage", direct)
        self.assertLess(normal.index("GatewayEditionPage := CreateInputOptionPage("), normal.index("ProductModePage :="))

    def test_the_direct_build_accepts_only_an_exact_edition_name_before_its_install_guard(self):
        prepare = section(text("AnyAiCam-VMS.iss"), "function PrepareToInstall(", "\nend;")
        direct = prepare[:prepare.index("#else")]
        for name in ("full_vms", "local_gateway", "cloud_gateway"):
            self.assertIn("(ExpandConstant('{param:GatewayEdition|full_vms}') <> '%s')" % name, direct)
        self.assertLess(direct.index("{param:GatewayEdition|full_vms}"), direct.index("ExtractTemporaryFile('direct-test-install-guard.ps1')"))
        normal = prepare[prepare.index("#else"):]
        self.assertLess(normal.index("GatewayEditionProblem()"), normal.index("ProductModeProblem()"))

    def test_the_direct_runtime_install_receives_the_edition_and_stays_local(self):
        iss = text("AnyAiCam-VMS.iss")
        direct_call = [line for line in iss.splitlines() if "-DirectTest -TestRunId" in line]
        self.assertEqual(len(direct_call), 1)
        self.assertIn('-ProductMode local -GatewayEdition "{param:GatewayEdition|full_vms}" -DirectTest', direct_call[0])
        runtime = text("install-runtime.ps1")
        self.assertIn("if (-not $DirectTest) {\n    $ProductMode = Resolve-GatewayProductMode", runtime.replace("\r\n", "\n"))
        self.assertIn("Set-GatewayEdition -Values $values -Edition $GatewayEdition", runtime)
        for flag in ("ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED", "ANYAICAM_RECORDING_UPLOAD_ENABLED", "ANYAICAM_LIVE_RELAY_ENABLED"):
            self.assertIn("$values['%s'] = 'false'" % flag, runtime)
        self.assertIn("$values['ANYAICAM_PRODUCT_MODE'] = 'local'", runtime)

    def test_the_direct_launcher_validates_the_installed_edition_and_forces_transports_off_afterwards(self):
        launcher = text("service-launcher-direct-test.ps1")
        self.assertIn("'ANYAICAM_GATEWAY_EDITION')) {", launcher)
        assert_at = launcher.index("Assert-GatewayRuntimeConfiguration ([string]$env:ANYAICAM_GATEWAY_EDITION) 'edge'")
        self.assertLess(launcher.index("installer\\gateway-edition.ps1"), assert_at)
        self.assertLess(assert_at, launcher.index("$env:ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED = 'false'"))
        self.assertIn('Source: "gateway-edition.ps1"; DestDir: "{app}\\installer"', text("AnyAiCam-VMS.iss"))

    def test_build_has_one_keyset_step_after_the_fresh_output_directory(self):
        build = text("build.ps1")
        self.assertEqual(build.count("(Join-Path $PSScriptRoot 'prepare-keyset.py')"), 2)  # PythonExe / py -3.11 branches
        self.assertEqual(len(re.findall(r"^\$registryEnvironment = ", build, re.M)), 1)
        self.assertIn("$registryEnvironment = if ($InternalTestAzureSign) { 'internal-test' } elseif ($Sign -or $AzureSign) { 'production' } else { 'any' }", build)
        self.assertNotIn("Join-Path $PSScriptRoot 'output\\entitlement-signing-public-keys.json'", build)
        self.assertLess(build.index("New-Item -ItemType Directory -Path $outputDir -ErrorAction Stop"), build.index("$keysetOut = Join-Path $outputDir"))
        self.assertLess(build.index("$keysetOut = Join-Path $outputDir"), build.index("& $iscc"))


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell")
class DirectLauncherEditionRunTests(unittest.TestCase):
    """The launcher's edition gate itself, run in Windows PowerShell."""

    def run_gate(self, edition):
        script = ". '%s'; Assert-GatewayRuntimeConfiguration '%s' 'edge'; 'ok'" % (WIN / "gateway-edition.ps1", edition)
        return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=120)

    def test_each_edition_passes_and_an_unknown_one_is_refused(self):
        for edition in ("", "full_vms", "local_gateway", "cloud_gateway"):
            result = self.run_gate(edition)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok", result.stdout)
        refused = self.run_gate("gateway_plus")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Invalid Gateway edition", refused.stdout + refused.stderr)


if __name__ == "__main__":
    unittest.main()
