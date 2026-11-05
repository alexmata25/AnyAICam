"""1.3.4.1 upgrade harness: U134-F seeds a public keyset over the real baseline, inside the Sandbox only."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "installer" / "windows" / "sandbox"
MAKE_WSB = SANDBOX / "make-wsb.ps1"
FUNCTIONAL = (SANDBOX / "functional-in-sandbox.ps1").read_text(encoding="utf-8-sig")
PUBLIC = b'{"keys": {"sandbox-test-1": "AAAA"}}\n'


def make_wsb(tmp_path, keyset_bytes=PUBLIC, *extra, pin=None):
    keyset = tmp_path / "keys.json"
    keyset.write_bytes(keyset_bytes)
    setup = tmp_path / "AnyAiCam-VMS-Setup-1.3.4.1-2598f88.exe"
    previous = tmp_path / "AnyAiCam-VMS-Setup-1.3.3-b8c7a7e.exe"
    signing = tmp_path / "signing.json"
    setup.write_bytes(b"x")
    previous.write_bytes(b"x")
    signing.write_text(json.dumps({"key_id": "sandbox-test-1"}), encoding="utf-8")
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(MAKE_WSB), "-Setup", str(setup),
               "-Folder", str(tmp_path / "run"), "-ProductMode", "local", "-KeysetKind", "synthetic-test",
               "-ExpectedKeysetSha256", pin or hashlib.sha256(keyset_bytes).hexdigest(),
               "-SyntheticSigningKey", str(signing), "-BaselineKeyset", str(keyset), *extra]
    return subprocess.run(command, capture_output=True, text=True, timeout=60), previous


def functional(tmp_path, keyset_bytes=PUBLIC, pin=None):
    result, _ = make_wsb(tmp_path, keyset_bytes, "-Functional", "-Previous",
                         str(tmp_path / "AnyAiCam-VMS-Setup-1.3.3-b8c7a7e.exe"), pin=pin)
    return result


@pytest.fixture
def prepared(tmp_path):
    (tmp_path / "AnyAiCam-VMS-Setup-1.3.3-b8c7a7e.exe").write_bytes(b"x")
    return tmp_path


def test_a_matching_public_keyset_reaches_the_sandbox_and_is_flagged(prepared):
    result = functional(prepared)
    assert result.returncode == 0, result.stderr
    settings = json.loads((prepared / "run" / "scripts" / "acceptance.json").read_text(encoding="utf-8-sig"))
    assert settings["baseline_trust_seeded"] is True
    assert (prepared / "run" / "scripts" / "baseline-seed-keyset.json").read_bytes() == PUBLIC


def test_a_keyset_that_differs_from_the_pin_is_refused(prepared):
    result = functional(prepared, pin="0" * 64)
    assert result.returncode != 0 and "does not match" in result.stderr
    assert not (prepared / "run").exists()


def test_private_material_is_refused(prepared):
    result = functional(prepared, b'{"private_key": "x"}\n')
    assert result.returncode != 0 and "private key material" in result.stderr


def test_seeding_needs_a_functional_upgrade_run(prepared):
    result, _ = make_wsb(prepared)
    assert result.returncode != 0 and "functional upgrade run" in result.stderr


def test_the_seed_happens_only_when_flagged_and_after_a_negative_control():
    gate = FUNCTIONAL.index("if ($settings.baseline_trust_seeded) {\n    Check 'baseline trust") if "\r\n" not in FUNCTIONAL \
        else FUNCTIONAL.index("if ($settings.baseline_trust_seeded) {\r\n    Check 'baseline trust")
    assert gate < FUNCTIONAL.index("(negative control)") < FUNCTIONAL.index("Move-Item -LiteralPath $temp -Destination $keysetFile")
    assert FUNCTIONAL.index("Move-Item -LiteralPath $temp -Destination $keysetFile") < FUNCTIONAL.index("function UpgradeState")


def test_every_seeded_report_says_it_is_not_customer_state():
    assert "LABEL: BASELINE TRUST SEEDED IN SANDBOX -- not customer upgrade state." in FUNCTIONAL


def test_recovery_is_called_fix_forward_and_rollback_is_reported_as_untested():
    assert "Actual rollback is NOT TESTED" in FUNCTIONAL
    assert "Check 'rollback:" not in FUNCTIONAL and "setup-rollback-install" not in FUNCTIONAL


def test_the_keyset_must_survive_the_upgrade_compare():
    state = FUNCTIONAL.split("function UpgradeState {", 1)[1].split("\nfunction ", 1)[0]
    assert "keyset" in state and "$keysetFile" in state


def test_recovery_after_a_failed_setup_reruns_the_same_setup_and_checks_what_matters():
    block = FUNCTIONAL.split("# R4 (1.3.4.1)", 1)[1].split("Cloud POST '/test/revoke-appliance'", 1)[0]
    assert "$r4Fail.ExitCode -ne 20" in block and "$r4Again.ExitCode -ne 0" in block
    assert "'appliance_id', 'credential', 'secrets', 'keyset'" in block and "ClipIds" in block
