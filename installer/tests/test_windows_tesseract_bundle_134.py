"""Windows 1.3.4 LPR parity (Blue, 2026-10-27): the Tesseract OCR engine ships with the setup.

* Pins: tesseract-bundle.lock.json and build.ps1 name the same bundle SHA-256; the bundle's natives are in the signed
  runtime catalog; the .iss ships it and passes it to install-runtime.ps1.
* Licences: every package is permissive except libiconv (LGPL-2.1, imported by archive.dll), which the lock carries as
  an explicit legal gate. Nothing GPL is bundled. Every package's own licence files ship in the bundle.
* Bundle: when the vendor copy exists, its manifest matches every file, nothing is unlisted, the DLLs are exactly
  tesseract.exe's import closure (read from the PE import tables), and it fits its size budget.
* Behaviour: the install-runtime.ps1 Tesseract block is EXECUTED with real PowerShell: clean install, upgrade (the old
  folder is replaced as a whole), a tampered file and an unlisted file (both refused, the old install untouched).
  tesseract.exe itself is never run here: its OCR is proven in the Sandbox/physical scenarios.
"""
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
LOCK = json.loads((WIN / "tesseract-bundle.lock.json").read_text(encoding="utf-8"))
BUILD = (WIN / "build.ps1").read_text(encoding="utf-8")
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
RUNTIME = (WIN / "install-runtime.ps1").read_text(encoding="utf-8")
LAUNCHER = (WIN / "service-launcher.ps1").read_text(encoding="utf-8")
BUNDLE = WIN / "vendor" / "tesseract-5.5.2-win64-anyaicam.zip"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
PERMISSIVE = {
    "Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "Zlib", "zlib-acknowledgement", "HPND", "0BSD",
    "bzip2-1.0.6", "curl", "IJG AND BSD-3-Clause AND Zlib",
}


def _prepare_module():
    spec = importlib.util.spec_from_file_location("prepare_tesseract_bundle", WIN / "prepare-tesseract-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PinsAndPackagingTests(unittest.TestCase):
    def test_one_bundle_pin(self):
        pin = LOCK["bundle_sha256"]
        self.assertRegex(pin, r"^[0-9a-f]{64}$")
        self.assertIn(f"'tesseract-5.5.2-win64-anyaicam.zip'    = '{pin}'", BUILD)

    def test_the_bundles_natives_are_in_the_signed_runtime_catalog(self):
        native_line = next(line for line in BUILD.splitlines() if line.startswith("$nativeArchives"))
        self.assertIn("'tesseract-5.5.2-win64-anyaicam.zip'", native_line)

    def test_setup_ships_the_bundle_and_hands_it_to_install_runtime(self):
        self.assertIn('Source: "vendor\\tesseract-5.5.2-win64-anyaicam.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall', ISS)
        self.assertIn("-TesseractArchive \"' + ExpandConstant('{tmp}\\tesseract-5.5.2-win64-anyaicam.zip')", ISS)
        self.assertIn("[Parameter(Mandatory=$true)][string]$TesseractArchive", RUNTIME)

    def test_the_vms_finds_tesseract_and_its_model(self):
        self.assertIn("$env:PATH = (Join-Path $installRoot 'runtime\\tools\\tesseract') + ';' + $env:PATH", LAUNCHER)
        self.assertIn("$env:TESSDATA_PREFIX = Join-Path $installRoot 'runtime\\tools\\tesseract\\tessdata'", LAUNCHER)

    def test_lpr_is_defaulted_like_linux_without_overriding_a_choice(self):
        flags = re.search(r"foreach \(\$analyticsFlag in @\(([^)]*)\)\)", RUNTIME).group(1)
        self.assertIn("'ANYAICAM_LPR_ENABLED'", flags)
        self.assertIn("if (-not $values.Contains($analyticsFlag)) { $values[$analyticsFlag] = 'true' }", RUNTIME)


class LicenceTests(unittest.TestCase):
    def test_every_package_is_pinned_from_conda_forge(self):
        for package in LOCK["packages"]:
            self.assertRegex(package["sha256"], r"^[0-9a-f]{64}$", package["name"])
            self.assertTrue(package["url"].startswith("https://conda.anaconda.org/conda-forge/win-64/"), package["name"])
            self.assertTrue(package["url"].endswith("/" + package["file"]), package["name"])

    def test_only_libiconv_is_copyleft_and_it_is_an_explicit_gate(self):
        copyleft = {p["name"]: p["license"] for p in LOCK["packages"] if p["license"] not in PERMISSIVE}
        self.assertEqual(copyleft, {"libiconv": "LGPL-2.1-only"})
        gate = LOCK["license_review"]["copyleft"]
        self.assertEqual([entry["package"] for entry in gate], ["libiconv"])
        self.assertIn("LEGAL GATE", gate[0]["status"])
        self.assertFalse([p for p in LOCK["packages"] if "GPL" in p["license"] and "LGPL" not in p["license"]])

    def test_every_dll_comes_from_a_locked_package(self):
        names = {p["name"] for p in LOCK["packages"]}
        for entry in LOCK["dlls"]:
            self.assertIn(entry["package"], names, entry["dll"])

    def test_the_model_is_tessdata_fast_english(self):
        self.assertEqual(LOCK["model"]["sha256"], "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2")


@unittest.skipUnless(BUNDLE.exists(), "vendor bundle not prepared (run prepare-tesseract-bundle.py)")
class RealBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = BUNDLE.read_bytes()
        cls.zip = zipfile.ZipFile(io.BytesIO(cls.data))
        cls.manifest = json.loads(cls.zip.read("BUNDLE-MANIFEST.json"))

    def test_the_vendor_copy_is_the_pinned_one(self):
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), LOCK["bundle_sha256"])

    def test_the_manifest_matches_every_file_and_nothing_is_unlisted(self):
        names = set(self.zip.namelist()) - {"BUNDLE-MANIFEST.json"}
        self.assertEqual(names, set(self.manifest["files"]))
        for name, info in self.manifest["files"].items():
            self.assertEqual(hashlib.sha256(self.zip.read(name)).hexdigest(), info["sha256"], name)

    def test_the_dlls_are_exactly_the_import_closure_of_tesseract(self):
        prepare = _prepare_module()
        dlls = {n.split("/")[-1].lower(): self.zip.read(n) for n in self.zip.namelist() if n.lower().endswith(".dll")}
        needed, missing = prepare.import_closure(self.zip.read("tesseract/tesseract.exe"), dlls)
        self.assertEqual(missing, set())
        self.assertEqual(needed, set(dlls))
        self.assertEqual(needed, {entry["dll"].lower() for entry in LOCK["dlls"]})

    def test_every_package_licence_ships(self):
        for package in LOCK["packages"]:
            self.assertTrue(any(n.startswith(f"licenses/{package['name']}/") for n in self.zip.namelist()), package["name"])

    def test_size_budget(self):
        self.assertLessEqual(len(self.data), 30 * 1024 * 1024)


def _runtime_block() -> str:
    start = RUNTIME.index("$tesseractRoot = Join-Path $InstallRoot")
    end = RUNTIME.index("} finally { if (Test-Path $tesseractTemp)", start)
    return RUNTIME[start:RUNTIME.index("\n", end)]


def _bundle(path: Path, files: dict, *, unlisted: dict | None = None, tamper: str | None = None):
    manifest = {"files": {name: {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)} for name, blob in files.items()}}
    with zipfile.ZipFile(path, "w") as archive:
        for name, blob in {**files, **(unlisted or {})}.items():
            archive.writestr(name, b"tampered" if name == tamper else blob)
        archive.writestr("BUNDLE-MANIFEST.json", json.dumps(manifest))


GOOD = {
    "tesseract/tesseract.exe": b"MZ exe",
    "tesseract/tesseract55.dll": b"MZ dll",
    "tesseract/tessdata/eng.traineddata": b"model",
    "licenses/tesseract/LICENSE": b"Apache-2.0",
}


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class InstallBlockExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aac-tess-"))
        self.root = self.tmp / "Program Files" / "AnyAiCam"
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, archive: Path):
        script = self.tmp / "block.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$InstallRoot = '{self.root}'\n$TesseractArchive = '{archive}'\n" + _runtime_block() + "\n",
            encoding="utf-8",
        )
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True, timeout=120)

    def _installed(self):
        base = self.root / "runtime" / "tools" / "tesseract"
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())

    def test_clean_install_places_engine_model_manifest_and_licences(self):
        archive = self.tmp / "b.zip"
        _bundle(archive, GOOD)
        result = self._run(archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._installed(), ["BUNDLE-MANIFEST.json", "tessdata/eng.traineddata", "tesseract.exe", "tesseract55.dll"])
        self.assertEqual((self.root / "licenses" / "tesseract" / "tesseract" / "LICENSE").read_bytes(), b"Apache-2.0")

    def test_an_upgrade_replaces_the_old_folder_as_a_whole(self):
        old = self.root / "runtime" / "tools" / "tesseract"
        old.mkdir(parents=True)
        (old / "stale-4.1.dll").write_bytes(b"old")
        archive = self.tmp / "b.zip"
        _bundle(archive, GOOD)
        self.assertEqual(self._run(archive).returncode, 0)
        self.assertNotIn("stale-4.1.dll", self._installed())

    def test_a_tampered_file_is_refused_and_the_old_install_kept(self):
        old = self.root / "runtime" / "tools" / "tesseract"
        old.mkdir(parents=True)
        (old / "tesseract.exe").write_bytes(b"previous")
        archive = self.tmp / "b.zip"
        _bundle(archive, GOOD, tamper="tesseract/tesseract55.dll")
        result = self._run(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed its checksum", result.stderr + result.stdout)
        self.assertEqual((old / "tesseract.exe").read_bytes(), b"previous")

    def test_an_unlisted_file_is_refused(self):
        archive = self.tmp / "b.zip"
        _bundle(archive, GOOD, unlisted={"tesseract/extra.dll": b"MZ"})
        result = self._run(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unlisted files", result.stderr + result.stdout)
        self.assertFalse((self.root / "runtime" / "tools" / "tesseract").exists())

    @unittest.skipUnless(BUNDLE.exists(), "vendor bundle not prepared")
    def test_the_real_bundle_installs(self):
        result = self._run(BUNDLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self._installed()
        self.assertIn("tesseract.exe", installed)
        self.assertIn("tessdata/eng.traineddata", installed)
        self.assertEqual(len([n for n in installed if n.endswith(".dll")]), len(LOCK["dlls"]))


if __name__ == "__main__":
    unittest.main()
