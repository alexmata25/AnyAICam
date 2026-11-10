"""Static and helper coverage for the explicitly selected internal-test Azure signing path.

These tests never invoke ISCC, SignTool, Azure Artifact Signing, or Windows Sandbox.
"""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
HELPER = WIN / "prepare-keyset.py"


def _keyset():
    value = base64.b64encode(bytes(range(32))).decode("ascii")
    return (json.dumps({"keys": {"k-2026": value}}, sort_keys=True, indent=2) + "\n").encode("ascii")


def _registry(environment):
    value = base64.b64encode(bytes(range(32))).decode("ascii")
    document = {
        "type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": environment,
        "generation": 1, "issued_at": "2026-11-01T00:00:00+00:00",
        "keys": {"k-2026": {"public_key": value, "state": "active", "purposes": ["capacity"],
                                "environment": environment, "changed_generation": 1}},
    }
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("ascii")


def _prepare(tmp_path, *, registry_environment, required_environment):
    keyset = _keyset()
    registry = _registry(registry_environment)
    key_path, registry_path = tmp_path / "keys.json", tmp_path / "registry.json"
    out_key, out_registry = tmp_path / "out" / "keys.json", tmp_path / "out" / "registry.json"
    key_path.write_bytes(keyset)
    registry_path.write_bytes(registry)
    result = subprocess.run(
        [sys.executable, str(HELPER), str(key_path), hashlib.sha256(keyset).hexdigest(), str(out_key),
         str(registry_path), hashlib.sha256(registry).hexdigest(), str(out_registry), required_environment],
        capture_output=True, text=True, check=False,
    )
    return result, out_key, out_registry


def test_internal_test_mode_accepts_only_synthetic_registry(tmp_path):
    result, out_key, out_registry = _prepare(
        tmp_path, registry_environment="synthetic", required_environment="internal-test"
    )
    assert result.returncode == 0, result.stderr
    assert out_key.read_bytes() == _keyset()
    assert out_registry.read_bytes() == _registry("synthetic")


def test_internal_test_mode_rejects_production_registry(tmp_path):
    result, out_key, out_registry = _prepare(
        tmp_path, registry_environment="production", required_environment="internal-test"
    )
    assert result.returncode != 0
    assert "internal-test signing requires a synthetic registry" in result.stderr + result.stdout
    assert not out_key.exists() and not out_registry.exists()


def test_production_mode_still_rejects_synthetic_registry(tmp_path):
    result, out_key, out_registry = _prepare(
        tmp_path, registry_environment="synthetic", required_environment="production"
    )
    assert result.returncode != 0
    assert "a signed build needs the production registry" in result.stderr + result.stdout
    assert not out_key.exists() and not out_registry.exists()


def test_internal_signing_is_default_off_and_output_and_labels_are_isolated():
    build = (WIN / "build.ps1").read_text(encoding="utf-8-sig")
    iss = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8-sig")
    marker = (WIN / "internal-test-marker.txt").read_text(encoding="utf-8")
    assert "[switch]$InternalTestAzureSign" in build
    assert "output-internal-test" in build and "Test-Path -LiteralPath $outputDir" in build
    assert "Internal-test output cannot be a reparse point" in build
    assert "New-Item -ItemType Directory -Path $outputDir -ErrorAction Stop" in build
    assert "if ($InternalTestAzureSign) { $compilerArguments += '/DInternalTestOnly=1' }" in build
    assert '"/DBuildOutputDir=$outputName"' in build
    assert "-INTERNAL-TEST-DO-NOT-SHIP" in build
    assert "INTERNAL TEST - DO NOT SHIP" in iss and "INTERNAL TEST - DO NOT SHIP" in marker
    assert "#ifdef InternalTestOnly" in iss
    assert 'OutputDir={#BuildOutputDir}' in iss
    assert 'OutputBaseFilename=AnyAiCam-VMS-Setup-{#AppVersion}-{#ShortCommit}{#InternalTestFilenameSuffix}' in iss
    assert 'SignedUninstallerDir={#BuildOutputDir}\\signed-uninstallers' in iss
    # f9b9a57: the catalog name is a define (the direct-test build has its own catalog), still under BuildOutputDir.
    assert 'Source: "{#BuildOutputDir}\\{#RuntimeCatalogFileName}"' in iss
    assert '#define RuntimeCatalogFileName "AnyAiCamRuntime.cat"' in iss
    assert 'Source: "internal-test-marker.txt"; DestDir: "{app}"' in iss
    assert "Start-Transcript -LiteralPath $internalTranscript" in build
    assert "build-evidence.json" in build and "SHA256SUMS" in build
    assert "CatalogCoverageVerified" in build


def test_internal_and_production_signing_guards_remain_separate():
    build = (WIN / "build.ps1").read_text(encoding="utf-8-sig")
    assert "($Sign -and $AzureSign) -or ($Sign -and $InternalTestAzureSign) -or ($AzureSign -and $InternalTestAzureSign)" in build
    assert "$registryEnvironment = if ($InternalTestAzureSign) { 'internal-test' } elseif ($Sign -or $AzureSign) { 'production' } else { 'any' }" in build
    assert "if ($InternalTestAzureSign) { $compilerArguments += '/DInternalTestOnly=1' }" in build
    assert "/dlib" in build and "/dmdf" in build
    assert "if ($Sign -and $signature.SignerCertificate.Thumbprint -ne $thumbprint)" in build
    assert "Get-AnyAiCamPublisherProblem" in build
    assert "if ($Sign -or $AzureSign -or $InternalTestAzureSign)" in build
    # 0c12c4c: exactly one signed uninstaller cache artifact, by name, not a reparse point (stricter than "at least one").
    assert ("if ($InternalTestAzureSign -and ($cacheEntries.Count -ne 1 -or $signedUninstallers.Count -ne 1 -or "
            "$signedUninstallers[0].Name -ne 'AnyAiCamSignedUninstaller.exe' -or "
            "(($cacheEntries[0].Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)))") in build
