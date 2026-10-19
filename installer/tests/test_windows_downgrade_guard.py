"""Windows downgrade prevention (Red 2026-10-18): Setup refuses to replace a NEWER installed AnyAiCam VMS.

Static checks of AnyAiCam-VMS.iss (the repository's Windows installer tests run without Inno Setup), plus a Python
model of the Pascal version comparison exercised on the cases the guard must decide. The guard itself is exercised for
real in the Windows Sandbox acceptance (installer/windows/sandbox/acceptance-127.ps1).
"""
import re
import unittest
from pathlib import Path

ISS = (Path(__file__).resolve().parents[1] / "windows" / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
CODE = ISS.split("[Code]", 1)[1]


def model_compare(a: str, b: str) -> int:
    """Line-for-line model of CompareAnyAiCamVersions (1 newer, -1 older, 0 equal, -2 not plain x.y.z)."""
    def parts(s):
        out = []
        for _ in range(3):
            part, _, s = s.partition(".")
            out.append(int(part) if part.isdigit() else -1)
        return out, s
    (x, rest_a), (y, rest_b) = parts(a), parts(b)
    for i in range(3):
        if x[i] < 0 or y[i] < 0:
            return -2
        if x[i] != y[i]:
            return 1 if x[i] > y[i] else -1
    return -2 if (rest_a or rest_b) else 0


class DowngradeGuardTests(unittest.TestCase):
    def test_initialize_setup_refuses_a_newer_installed_version(self):
        body = CODE.split("function InitializeSetup(): Boolean;", 1)[1].split("\nend;", 1)[0]
        self.assertIn("InstalledAnyAiCamVersion()", body)
        self.assertIn("CompareAnyAiCamVersions(Installed, '{#AppVersion}') = 1", body)
        self.assertIn("Result := False;", body)
        self.assertIn("Log('Downgrade refused:", body)
        self.assertIn("if not WizardSilent() then", body)

    def test_reads_the_uninstall_entry_of_this_exact_app_id_in_both_registry_views(self):
        app_id = re.search(r"^AppId=\{\{([0-9A-F-]+)\}", ISS, re.M).group(1)
        self.assertIn(f"Uninstall\\{{{app_id}}}_is1'", CODE)
        self.assertIn("RegQueryStringValue(HKLM64, AnyAiCamUninstallKey, 'DisplayVersion'", CODE)
        self.assertIn("RegQueryStringValue(HKLM32, AnyAiCamUninstallKey, 'DisplayVersion'", CODE)

    def test_no_installed_version_means_a_fresh_install_proceeds(self):
        body = CODE.split("function InitializeSetup(): Boolean;", 1)[1]
        self.assertIn("if Installed = '' then exit;", body)
        self.assertLess(body.index("Result := True;"), body.index("if Installed = '' then exit;"))

    def test_version_comparison_model(self):
        cases = {("1.3.0", "1.2.7"): 1, ("1.3.0", "1.3.2"): -1, ("1.3.2", "1.3.2"): 0, ("0.1.3", "1.3.2"): -1,
                 ("1.3.10", "1.3.9"): 1, ("2.0.0", "1.9.9"): 1, ("1.3.1", "1.3.2"): -1,
                 ("0.0.0-unset", "1.3.2"): -1, ("1.3.2-rc", "1.3.2"): -2, ("1.3", "1.3.2"): -2, ("1.3.2.4", "1.3.2"): -2, ("x.y.z", "1.3.2"): -2}
        for (a, b), expected in cases.items():
            self.assertEqual(model_compare(a, b), expected, (a, b))

    def test_model_matches_the_pascal_shape(self):
        self.assertIn("for I := 1 to 3 do begin", CODE)
        self.assertIn("if (X < 0) or (Y < 0) then begin Result := -2; exit; end;", CODE)
        self.assertIn("if (A <> '') or (B <> '') then Result := -2;", CODE)
        self.assertIn("Result := StrToIntDef(Part, -1);", CODE)

    def test_only_a_strictly_newer_install_is_refused(self):
        # Equal (repair), older (upgrade, e.g. 1.3.0 -> 1.3.2) and unparseable (cannot judge) all proceed.
        self.assertIn("= 1 then begin", CODE.split("function InitializeSetup(): Boolean;", 1)[1])
        self.assertEqual(model_compare("1.3.0", "1.3.2"), -1)
        self.assertEqual(model_compare("1.3.2", "1.3.2"), 0)


if __name__ == "__main__":
    unittest.main()
