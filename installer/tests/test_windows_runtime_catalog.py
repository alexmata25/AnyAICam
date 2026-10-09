"""Signed runtime catalog (installer/windows/runtime-catalog.ps1, 2026-10-08).

The Azure-signed 1.3.1 setup ran under Smart App Control in Windows Sandbox,
but the runtime it installed could not load: the pinned wheels' native files
(pydantic-core, numpy, torch), WinSW, FFmpeg and MediaMTX are not signed by
their authors. The build now lists all of them in one catalog signed with
the AnyAiCam certificate, and the setup registers it before anything native
loads. These tests never register anything: they build catalogs into a
temporary folder and only open and close a catalog-admin context."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
MAKECAT = Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makecat.exe")
SCRIPT = WIN / "runtime-catalog.ps1"


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


def ps(command):
    return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, text=True, timeout=300)


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class CatalogScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-catalog-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wheel = self.tmp / "demo-1.0-cp312-cp312-win_amd64.whl"
        with zipfile.ZipFile(self.wheel, "w") as z:
            z.writestr("demo/_core.cp312-win_amd64.pyd", b"MZ pyd")
            z.writestr("demo.libs/helper.DLL", b"MZ dll")
            z.writestr("demo/__init__.py", b"print('python')")
            z.writestr("demo-1.0.dist-info/RECORD", b"")
        self.loose = self.tmp / "WinSW-x64.exe"
        self.loose.write_bytes(b"MZ exe")

    def call(self, expression):
        result = ps(f". {quote(SCRIPT)}; {expression}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_exports_only_native_files(self):
        stage = self.tmp / "stage"
        self.assertEqual(self.call(f"Export-NativeFiles {quote(stage)} @({quote(self.wheel)}) @({quote(self.loose)})"), "3")
        found = sorted(str(p.relative_to(stage)).replace("\\", "/") for p in stage.rglob("*") if p.is_file())
        self.assertEqual(found, ["1/demo.libs/helper.DLL", "1/demo/_core.cp312-win_amd64.pyd", "files/WinSW-x64.exe"])

    def test_refuses_a_path_that_leaves_the_stage(self):
        bad = self.tmp / "bad.whl"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("demo/../../escape.dll", b"MZ")
        result = ps(f". {quote(SCRIPT)}; Export-NativeFiles {quote(self.tmp / 'stage')} @({quote(bad)}) @()")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "escape.dll").exists())

    @unittest.skipUnless(MAKECAT.is_file(), "makecat.exe (Windows SDK) is required")
    def test_builds_one_sha256_catalog_of_every_staged_file(self):
        stage = self.tmp / "stage"
        self.call(f"[void](Export-NativeFiles {quote(stage)} @({quote(self.wheel)}) @({quote(self.loose)}))")
        catalog = self.tmp / "AnyAiCamRuntime.cat"
        self.assertEqual(self.call(f"New-RuntimeCatalog {quote(stage)} {quote(catalog)} {quote(MAKECAT)}"), "3")
        self.assertGreater(catalog.stat().st_size, 0)
        self.assertFalse(catalog.with_suffix(".cdf").exists())
        # Not signed here: the build signs it (build.ps1).
        self.assertEqual(self.call(f"(Get-AuthenticodeSignature -LiteralPath {quote(catalog)}).Status"), "NotSigned")

    def test_an_unsigned_catalog_is_never_registered(self):
        catalog = self.tmp / "AnyAiCamRuntime.cat"
        catalog.write_bytes(b"not a catalog")
        out = self.call(f"Install-RuntimeCatalog {quote(catalog)}")
        self.assertIn("not registered: it is not validly signed", out)

    def test_the_windows_catalog_api_is_reachable(self):
        # Opens and closes a catalog-admin context only; nothing is added.
        self.assertEqual(self.call("Use-CatalogAdmin { param($api, $admin) $admin -ne [IntPtr]::Zero }"), "True")

    def test_registers_in_the_code_integrity_catalog_database(self):
        # Windows Sandbox 2026-10-08: with a NULL subsystem the catalog went to
        # CatRoot\{127D0A1D-...} and Smart App Control still blocked WinSW;
        # under DRIVER_ACTION_VERIFY {F750E6C3-...} WinSW ran at once.
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$CodeIntegritySubsystem = [Guid]'F750E6C3-38EE-11D1-85E5-00C04FC295EE'", text)
        self.assertIn("CryptCATAdminAcquireContext([ref]$admin, $subsystem, 0)", text)
        self.assertNotIn("CryptCATAdminAcquireContext([ref]$admin, [IntPtr]::Zero", text)
        self.assertTrue(self.call("Get-InstalledCatalogPath").endswith("\System32\CatRoot\{F750E6C3-38EE-11D1-85E5-00C04FC295EE}\AnyAiCamRuntime.cat"))

    def test_dot_sourcing_removes_nothing(self):
        self.assertEqual(self.call("'loaded'"), "loaded")


class CatalogWiringTests(unittest.TestCase):
    def test_build_catalogs_the_pinned_inputs_and_signs_and_verifies_it_before_compiling(self):
        build = read("build.ps1")
        steps = [build.index(s) for s in (
            "Get-VendorProblems $vendor @($pinned.Keys) $lockedWheels",
            "Export-NativeFiles $catalogStage $nativeArchives @(Join-Path $vendor 'WinSW-x64.exe')",
            "New-RuntimeCatalog $catalogStage $catalog $makeCat",
            "& $signTool sign /fd SHA256 /tr $timestampUrl /td SHA256 /dlib $dlib /dmdf $metadata $catalog",
            "(Get-AuthenticodeSignature -LiteralPath $catalog).Status -ne 'Valid'",
            "& $signTool verify /pa /c $catalog $sample.FullName",
            "& $iscc @compilerArguments")]
        self.assertEqual(steps, sorted(steps))
        self.assertIn("& $signTool sign /sha1 $thumbprint /fd SHA256 /tr $timestampUrl /td SHA256 $catalog", build)
        for archive in ("python-3.12.10-embed-amd64.zip", "ffmpeg-8.1.2-essentials_build.zip", "mediamtx_v1.21.0_windows_amd64.zip"):
            self.assertIn(f"'{archive}'", build.split("$nativeArchives = ", 1)[1].split("\n", 1)[0])

    def test_setup_ships_registers_first_and_removes_on_uninstall(self):
        iss = read("AnyAiCam-VMS.iss")
        self.assertIn('Source: "output\\AnyAiCamRuntime.cat"; DestDir: "{tmp}"; Flags: deleteafterinstall', iss)
        self.assertIn('Source: "runtime-catalog.ps1"; DestDir: "{app}\\installer"; Flags: ignoreversion', iss)
        self.assertIn("-RuntimeCatalog \"' + ExpandConstant('{tmp}\\AnyAiCamRuntime.cat') + '\" -EntitlementKeyset", iss)
        uninstall = iss.split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0]
        self.assertIn('runtime-catalog.ps1"" -Remove"', uninstall)
        self.assertLess(uninstall.index('AnyAiCamVMS.exe"; Parameters: "uninstall"'), uninstall.index("runtime-catalog.ps1"))
        runtime = read("install-runtime.ps1")
        register = runtime.index("Install-RuntimeCatalog $RuntimeCatalog")
        for later in ("Expand-Archive -LiteralPath $PythonArchive", "& $python $GetPipScript", "runtime-preflight.py"):
            self.assertLess(register, runtime.index(later), later)


if __name__ == "__main__":
    unittest.main()
