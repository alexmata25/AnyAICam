"""Windows 1.3.4 LPR (owner 2026-10-27): the minimal Tesseract build kit, without libarchive/libcurl.

Every published Windows Tesseract (conda-forge, the tesserocr wheels) hard-imports archive.dll, which imports the
LGPL libiconv (iconv.dll, charset.dll) and libxml2. tesseract-build/ builds Tesseract 5.5.2 from its pinned source
with DISABLE_ARCHIVE/DISABLE_CURL against the pinned, permissively licensed leptonica closure, inside Windows Sandbox.
These tests check the kit itself; the build and its OCR run happen in the Sandbox.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "installer" / "windows" / "tesseract-build"
LOCK = json.loads((KIT / "tesseract-minimal.lock.json").read_text(encoding="utf-8"))
BUILD = (KIT / "build-tesseract-in-sandbox.ps1").read_text(encoding="utf-8")
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
PERMISSIVE = {"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "Zlib", "zlib-acknowledgement", "HPND", "0BSD",
              "IJG AND BSD-3-Clause AND Zlib"}


class LockTests(unittest.TestCase):
    def test_the_source_is_pinned(self):
        self.assertEqual(LOCK["source"]["sha256"], "6235ea0dae45ea137f59c09320406f5888383741924d98855bd2ce0d16b54f21")
        self.assertEqual(LOCK["source"]["url"], "https://github.com/tesseract-ocr/tesseract/archive/refs/tags/5.5.2.tar.gz")

    def test_archive_curl_and_their_chains_are_disabled(self):
        options = set(LOCK["cmake_options"])
        for required in ("-DDISABLE_ARCHIVE=ON", "-DDISABLE_CURL=ON", "-DSW_BUILD=OFF", "-DBUILD_TRAINING_TOOLS=OFF",
                         "-DENABLE_NATIVE=OFF", "-DBUILD_SHARED_LIBS=OFF"):
            self.assertIn(required, options)
        for dll in ("archive.dll", "iconv.dll", "charset.dll", "libxml2.dll", "libcurl.dll", "lzo2.dll"):
            self.assertIn(dll, LOCK["forbidden_dlls"])

    def test_every_linked_package_is_permissive_and_none_is_an_archive_or_network_library(self):
        names = {p["name"] for p in LOCK["packages"]}
        self.assertFalse(names & {"libarchive", "libiconv", "libxml2", "libxml2-16", "libcurl", "icu", "openssl", "libssh2", "libpsl", "lzo"})
        for package in LOCK["packages"]:
            self.assertIn(package["license"], PERMISSIVE, package["name"])
            self.assertRegex(package["sha256"], r"^[0-9a-f]{64}$")

    def test_the_model_is_the_linux_one(self):
        self.assertEqual(LOCK["model"]["sha256"], "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2")

    def test_the_build_is_deterministic_where_msvc_allows(self):
        self.assertIn("/Brepro", LOCK["deterministic_flags"]["CMAKE_CXX_FLAGS"])
        self.assertIn("/Brepro", LOCK["deterministic_flags"]["CMAKE_EXE_LINKER_FLAGS"])


class SandboxScriptTests(unittest.TestCase):
    def test_it_verifies_every_input_first(self):
        self.assertLess(BUILD.index("inputs-manifest.json"), BUILD.index("vs_buildtools.exe"))
        self.assertIn("throw \"Input changed:", BUILD)

    def test_the_build_tools_must_be_microsoft_signed(self):
        self.assertIn("SignerCertificate.Subject -notmatch '^CN=Microsoft Corporation,'", BUILD)

    def test_it_records_the_real_imports_and_flags_forbidden_ones(self):
        self.assertIn("dumpbin /nologo /dependents", BUILD)
        self.assertIn("$info.forbidden_imports", BUILD)

    def test_ocr_runs_the_way_lpr_calls_tesseract(self):
        lpr = (ROOT / "app" / "lpr.py").read_text(encoding="utf-8")
        self.assertIn("--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", lpr)
        self.assertIn("--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", BUILD)

    def test_an_app_control_block_is_reported_not_counted_as_a_misread(self):
        self.assertIn("blocked_by_app_control", BUILD)


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class WsbTests(unittest.TestCase):
    def test_inputs_read_only_output_writable_and_isolation_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            inputs, output, wsb = Path(folder, "in"), Path(folder, "out"), Path(folder, "b.wsb")
            inputs.mkdir()
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                     str(KIT / "make-build-wsb.ps1"), "-Inputs", str(inputs), "-Output", str(output), "-WsbPath", str(wsb)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            xml = wsb.read_text(encoding="utf-8-sig")
            self.assertRegex(xml, r"<SandboxFolder>C:\\Inputs</SandboxFolder><ReadOnly>true</ReadOnly>")
            self.assertRegex(xml, r"<SandboxFolder>C:\\Output</SandboxFolder><ReadOnly>false</ReadOnly>")
            for setting in ("<ClipboardRedirection>Disable", "<VGpu>Disable", "<PrinterRedirection>Disable"):
                self.assertIn(setting, xml)
            self.assertIn(r"C:\Inputs\build-tesseract-in-sandbox.ps1", xml)

    def test_a_non_empty_output_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "in").mkdir()
            Path(folder, "out").mkdir()
            Path(folder, "out", "old.txt").write_text("x")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                     str(KIT / "make-build-wsb.ps1"), "-Inputs", str(Path(folder, "in")), "-Output", str(Path(folder, "out")),
                                     "-WsbPath", str(Path(folder, "b.wsb"))], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
