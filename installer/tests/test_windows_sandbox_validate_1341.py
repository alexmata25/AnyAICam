"""1.3.4.1 Sandbox acceptance (2026-10-29, P-L on 46713ee): validate-in-sandbox.ps1 read the setup version from the
file name with a three-part pattern only, so the four-part 1.3.4.1 setup failed "setup identity" before any real check.
The setup version has three or four parts (the downgrade guard and make-wsb.ps1 accept both)."""
import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "validate-in-sandbox.ps1").read_text(encoding="utf-8-sig")


def _version_pattern() -> str:
    line = next(l for l in SCRIPT.splitlines() if l.startswith("$expectedVersion = "))
    return re.search(r"-match '([^']+)'", line).group(1)


def test_three_and_four_part_setup_versions_are_read_from_the_setup_name():
    pattern = _version_pattern()
    for name, version in (("AnyAiCam-VMS-Setup-1.3.3-b8c7a7e.exe", "1.3.3"),
                          ("AnyAiCam-VMS-Setup-1.3.4.1-46713ee.exe", "1.3.4.1")):
        match = re.search(pattern, name)
        assert match and match.group(1) == version, name


def test_a_malformed_setup_name_still_yields_no_version():
    assert not re.search(_version_pattern(), "AnyAiCam-VMS-Setup-latest-46713ee.exe")
