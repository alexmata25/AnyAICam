"""Codex 2026-10-17 (Windows): pin the approved Azure publisher identity; a VALID signature from any other publisher
must be rejected. Runs the real installer/windows/publisher-pin.ps1 in Windows PowerShell."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIN = ROOT / "installer" / "windows" / "publisher-pin.ps1"
POWERSHELL = shutil.which("powershell")
APPROVED_RELEASE = Path(r"C:\Users\Alejandro Mata\anyaicam-release-windows\candidates\1.3.1-7c66928-signed")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="needs Windows PowerShell")


def _ps(body: str) -> str:
    script = f". '{PIN}'\n{body}"
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _verdict_for_file(path) -> str:
    return _ps(f"$p = Get-AnyAiCamPublisherProblem (Get-AuthenticodeSignature -LiteralPath '{path}'); if ($p) {{ 'REJECT: ' + $p }} else {{ 'ACCEPT' }}")


def test_the_approved_publisher_release_is_accepted():
    setups = sorted(APPROVED_RELEASE.glob("*.exe")) if APPROVED_RELEASE.exists() else []
    if not setups:
        pytest.skip("the approved signed 1.3.1 release is not on this machine")
    assert _verdict_for_file(setups[0]) == "ACCEPT"


def test_a_valid_signature_from_an_unapproved_publisher_is_rejected():
    """powershell.exe carries a perfectly valid Microsoft Windows signature -- valid, but not AnyAiCam."""
    target = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    assert _ps(f"(Get-AuthenticodeSignature -LiteralPath '{target}').Status") == "Valid"
    verdict = _verdict_for_file(target)
    assert verdict.startswith("REJECT: signed by an unapproved publisher (CN=Microsoft Windows")


def test_an_unsigned_file_is_rejected(tmp_path):
    unsigned = tmp_path / "unsigned.ps1"
    unsigned.write_text("Write-Output 'x'\n", encoding="utf-8")
    assert _verdict_for_file(unsigned).startswith("REJECT: not validly signed")


@pytest.mark.parametrize("with_identity_eku,expected", [
    (False, "the signing certificate does not carry the approved publisher identity"),
    (True, "the signing certificate has no issuing chain"),   # self-signed: refused before the root comparison
])
def test_a_look_alike_certificate_with_the_exact_subject_is_rejected(with_identity_eku, expected):
    """A self-made certificate copying the subject (and even the identity EKU) is still not the publisher."""
    eku = "1.3.6.1.5.5.7.3.3" + (",$AnyAiCamPublisherIdentityEku" if with_identity_eku else "")
    body = (
        "$cert = New-SelfSignedCertificate -Subject $AnyAiCamPublisherSubject -Type Custom -KeyUsage DigitalSignature "
        f"-TextExtension @(\"2.5.29.37={{text}}{eku}\") -CertStoreLocation Cert:\\CurrentUser\\My\n"
        "try { $fake = [pscustomobject]@{ Status = 'Valid'; SignerCertificate = $cert }\n"
        "      $p = Get-AnyAiCamPublisherProblem $fake; if ($p) { 'REJECT: ' + $p } else { 'ACCEPT' } }\n"
        "finally { Remove-Item -LiteralPath ('Cert:\\CurrentUser\\My\\' + $cert.Thumbprint) -Force }"
    )
    verdict = _ps(body)
    assert verdict == f"REJECT: {expected}"


def test_install_and_build_use_the_pin():
    catalog = (ROOT / "installer" / "windows" / "runtime-catalog.ps1").read_text(encoding="utf-8-sig")
    assert ". (Join-Path $PSScriptRoot 'publisher-pin.ps1')" in catalog
    assert "Get-AnyAiCamPublisherProblem $signature" in catalog
    assert catalog.index("Get-AnyAiCamPublisherProblem $signature") < catalog.index("CryptCATAdminAddCatalog($admin")
    build = (ROOT / "installer" / "windows" / "build.ps1").read_text(encoding="utf-8-sig")
    assert build.count("Get-AnyAiCamPublisherProblem") == 2 and "if ($AzureSign) {" in build
    iss = (ROOT / "installer" / "windows" / "AnyAiCam-VMS.iss").read_text(encoding="utf-8-sig")
    assert 'Source: "publisher-pin.ps1"; DestDir: "{app}\\installer"' in iss
