"""Requirement 9 (1.2.7), Windows installer step 2: the entitlement keyset reaches a protected trust folder.

The keyset (cloud entitlement-signing PUBLIC keys) is a required, verified build input (build.ps1 ->
prepare-keyset.py, the same validator as the Linux release build), ships in Setup, and install-runtime.ps1 installs it
into %ProgramData%\\AnyAiCam\\update owned by Administrators with SYSTEM/Administrators-only access -- what
app/platform_paths.windows_write_problem() requires before the VMS trusts it. The permission steps themselves need
Administrator rights and are exercised in the Windows Sandbox validation; here the helper runs for real and the
scripts are checked for the required steps in the right order."""
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
HELPER = WIN / "prepare-keyset.py"
KEY = base64.b64encode(bytes(range(32))).decode()
OTHER_KEY = base64.b64encode(bytes(range(32, 64))).decode()


def _canonical(keys):
    return (json.dumps({"keys": dict(sorted(keys.items()))}, sort_keys=True, indent=2) + "\n").encode("ascii")


def _registry(keys=None, *, environment="production"):
    """A SYNTHETIC registry document, for these build tests only."""
    keys = {"k-2026": (KEY, "active")} if keys is None else keys
    document = {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": environment, "generation": 1,
                "issued_at": "2026-11-01T00:00:00+00:00",
                "keys": {key_id: {"public_key": value, "state": state, "purposes": ["capacity"],
                                  "environment": environment, "changed_generation": 1}
                         for key_id, (value, state) in keys.items()}}
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("ascii")


def _run(tmp_path, document: bytes, sha: str, *, registry=None, registry_sha=None, environment="any", retired=None):
    registry = _registry() if registry is None else registry
    source, output = tmp_path / "in.json", tmp_path / "out" / "keys.json"
    registry_source, registry_output = tmp_path / "registry.json", tmp_path / "out" / "registry.json"
    source.write_bytes(document)
    registry_source.write_bytes(registry)
    result = subprocess.run([sys.executable, str(HELPER), str(source), sha, str(output), str(registry_source),
                             registry_sha or hashlib.sha256(registry).hexdigest(), str(registry_output), environment,
                             *([retired] if retired else [])],
                            capture_output=True, text=True)
    return result, output


def test_a_pinned_public_keyset_is_written_in_canonical_form(tmp_path):
    canonical = _canonical({"k-2026": KEY})
    result, output = _run(tmp_path, json.dumps({"keys": {"k-2026": KEY}}).encode(), hashlib.sha256(canonical).hexdigest())
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == canonical


def test_a_keyset_that_does_not_match_its_pin_is_refused(tmp_path):
    result, output = _run(tmp_path, json.dumps({"keys": {"k-2026": KEY}}).encode(), "0" * 64)
    assert result.returncode != 0 and "refusing to package an unverified trust anchor" in (result.stderr + result.stdout)
    assert not output.exists()


@pytest.mark.parametrize("document", [
    b'{"keys": {"k": "-----BEGIN PRIVATE KEY-----"}}',
    b'{"keys": {}}',
    b'{"keys": {"k": "c2hvcnQ="}}',
    b'{"keys": {"k": "' + KEY.encode() + b'"}, "extra": 1}',
])
def test_private_malformed_or_empty_keysets_are_refused(tmp_path, document):
    result, output = _run(tmp_path, document, "a" * 64)
    assert result.returncode != 0 and not output.exists()


def _pinned(tmp_path, **kwargs):
    canonical = _canonical({"k-2026": KEY})
    return _run(tmp_path, canonical, hashlib.sha256(canonical).hexdigest(), **kwargs)


def test_a_registry_covering_the_pinned_keyset_is_written_byte_for_byte(tmp_path):
    result, output = _pinned(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (output.parent / "registry.json").read_bytes() == _registry()


@pytest.mark.parametrize("keys, message", [
    ({"other": (OTHER_KEY, "active")}, "omits pinned key"),
    ({"k-2026": (OTHER_KEY, "active")}, "other key material"),
    ({"k-2026": (KEY, "retired"), "next": (OTHER_KEY, "active")}, "not active"),
    ({"k-2026": (KEY, "revoked"), "next": (OTHER_KEY, "active")}, "not active"),
    ({"k-2026": (KEY, "active"), "next": (OTHER_KEY, "active")}, "projection is not the pinned keyset"),
])
def test_a_registry_that_would_lose_a_pinned_key_stops_the_build(tmp_path, keys, message):
    result, output = _pinned(tmp_path, registry=_registry(keys))
    assert result.returncode != 0 and message in (result.stderr + result.stdout)
    assert not output.exists() and not (output.parent / "registry.json").exists()


def _rotation(tmp_path, state="retired", retired="k-2026", environment="any"):
    canonical = _canonical({"k-2026": KEY, "next": OTHER_KEY})
    return _run(tmp_path, canonical, hashlib.sha256(canonical).hexdigest(), environment=environment, retired=retired,
                registry=_registry({"k-2026": (KEY, state), "next": (OTHER_KEY, "active")}))


def test_a_rotation_build_keeps_a_retired_key_only_when_it_is_named(tmp_path):
    result, output = _rotation(tmp_path, environment="production")
    assert result.returncode == 0, result.stderr
    assert sorted(json.loads(output.read_bytes())["keys"]) == ["k-2026", "next"]


@pytest.mark.parametrize("state, retired, message", [
    ("retired", None, "not active"),
    ("active", "k-2026", "but the registry has it as active"),
    ("retired", "k-2026,gone", "not a pinned key"),
    ("retired", "next", "not active"),
])
def test_a_rotation_build_stops_on_a_retired_list_that_does_not_match(tmp_path, state, retired, message):
    result, output = _rotation(tmp_path, state, retired)
    assert result.returncode != 0 and message in (result.stderr + result.stdout)
    assert not output.exists()


def test_a_revoked_key_can_never_be_named_as_retired(tmp_path):
    result, output = _rotation(tmp_path, "revoked")
    assert result.returncode != 0 and not output.exists()


def test_a_registry_with_the_wrong_digest_stops_the_build(tmp_path):
    result, output = _pinned(tmp_path, registry_sha="0" * 64)
    assert result.returncode != 0 and "refusing to package an unverified trust registry" in (result.stderr + result.stdout)
    assert not output.exists()


def test_no_registry_stops_the_build(tmp_path):
    canonical = _canonical({"k-2026": KEY})
    source, output = tmp_path / "in.json", tmp_path / "keys.json"
    source.write_bytes(canonical)
    result = subprocess.run([sys.executable, str(HELPER), str(source), hashlib.sha256(canonical).hexdigest(), str(output)],
                            capture_output=True, text=True)
    assert result.returncode != 0 and not output.exists()


def test_a_signed_build_refuses_a_synthetic_registry(tmp_path):
    registry = _registry(environment="synthetic")
    assert _pinned(tmp_path, registry=registry)[0].returncode == 0
    result, _ = _pinned(tmp_path, registry=registry, environment="production")
    assert result.returncode != 0 and "needs the production registry" in (result.stderr + result.stdout)


def _text(name):
    return (WIN / name).read_text(encoding="utf-8-sig")


def test_build_requires_the_keyset_and_its_pin_and_hands_both_to_setup():
    build = _text("build.ps1")
    assert "[Parameter(Mandatory=$true)][string]$EntitlementKeyset," in build
    assert "[Parameter(Mandatory=$true)][string]$EntitlementKeysetSha256," in build
    assert "prepare-keyset.py" in build and '/DKeysetSha256=$EntitlementKeysetSha256' in build
    assert "[string]$PythonExe = ''" in build
    assert "& $PythonExe (Join-Path $PSScriptRoot 'prepare-keyset.py')" in build
    assert "Python 3.12.10" in build
    assert build.index("prepare-keyset.py") < build.index("& $iscc")
    iss = _text("AnyAiCam-VMS.iss")
    assert "#error KeysetSha256 is required" in iss
    assert 'Source: "output\\entitlement-signing-public-keys.json"; DestDir: "{tmp}"' in iss
    assert '-EntitlementKeysetSha256 "{#KeysetSha256}"' in iss
    assert "[Parameter(Mandatory=$true)][string]$EntitlementTrustRegistry," in build
    assert "[Parameter(Mandatory=$true)][string]$EntitlementTrustRegistrySha256," in build
    assert '/DRegistrySha256=$EntitlementTrustRegistrySha256' in build
    assert build.count("$registryEnvironment @retiredKeyArgument") == 2
    assert "#error RegistrySha256 is required" in iss
    assert 'Source: "output\\entitlement-trust-registry.json"; DestDir: "{tmp}"' in iss
    assert '-EntitlementTrustRegistrySha256 "{#RegistrySha256}"' in iss


def test_a_refused_registry_stops_setup_before_the_services_stop():
    iss = _text("AnyAiCam-VMS.iss")
    prepare = iss[iss.index("function PrepareToInstall"):]
    assert prepare.index("Result := TrustRegistryProblem();") < prepare.index("'stopwait'")
    problem = iss[iss.index("function TrustRegistryProblem"):iss.index("function PrepareToInstall")]
    assert '--check "' in problem and "{#RegistrySha256}" in problem
    assert "ResultCode = TrustRegistryRefused" in problem


def test_setup_continues_past_the_early_check_only_on_exit_0():
    iss = _text("AnyAiCam-VMS.iss")
    problem = iss[iss.index("function TrustRegistryProblem"):iss.index("function PrepareToInstall")]
    body = problem[problem.index("begin"):]
    assert body.index("Result := 'Setup could not confirm") < body.index("try")  # refuses unless proven otherwise
    assert body.count("Result := ''") == 2
    assert "if ResultCode = 0 then\n      Result := ''" in problem.replace("\r\n", "\n")
    assert "ResultCode := -1;" in body and "except" in body and "GetExceptionMessage" in body
    missing = body[body.index("if not FileExists(Python)"):body.index("ExtractTemporaryFile")].replace("\r\n", "\n")
    assert "service\\AnyAiCamVMS.exe" in missing and "service\\AnyAiCamAgent.exe" in missing
    assert missing.index("else\n        Result := ''") > missing.index("service\\AnyAiCamAgent.exe")


def _powershell(tmp_path, script):
    runner = tmp_path / "run.ps1"
    runner.write_text(f". '{WIN / 'data-root-guard.ps1'}'\n{script}\n", encoding="utf-8-sig")
    return subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(runner)], capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_a_hard_link_in_the_trust_folder_stops_the_install(tmp_path):
    folder = tmp_path / "update"
    (folder / "sub").mkdir(parents=True)
    (folder / "entitlement_trust_registry.json").write_text("{}")
    script = f"Assert-AnyAiCamTrustFolderHasNoHardLinks '{folder}'; 'clean'"
    clean = _powershell(tmp_path, script)
    assert clean.returncode == 0 and "clean" in clean.stdout, clean.stderr
    os.link(folder / "entitlement_trust_registry.json", tmp_path / "second-name")
    linked = _powershell(tmp_path, script)
    assert linked.returncode != 0 and "second hard link" in linked.stdout + linked.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_an_acl_repair_never_goes_through_a_hard_link(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x")
    os.link(target, tmp_path / "second-name")
    before = subprocess.run(["icacls.exe", str(target)], capture_output=True, text=True).stdout
    result = _powershell(tmp_path, f"Set-AnyAiCamEntryAcl (Get-Item -LiteralPath '{target}' -Force)")
    assert result.returncode != 0 and "through hard link" in result.stdout + result.stderr
    assert subprocess.run(["icacls.exe", str(target)], capture_output=True, text=True).stdout == before


def test_install_creates_a_protected_trust_folder_and_verifies_before_writing():
    script = _text("install-runtime.ps1")
    block = script[script.index("$updateRoot = Join-Path $DataRoot 'update'"):script.index("Entitlement keyset installed")]
    order = ["Assert-AnyAiCamTrustFolderHasNoHardLinks $updateRoot",
             "Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $updateRoot -Force)",
             "Contains('PRIVATE')", "Get-FileHash -Algorithm SHA256 -LiteralPath $EntitlementKeyset",
             "agent\\system\\trust_install_windows.py", "The entitlement trust registry was not installed",
             "WriteAllBytes($keysetTemp", "Move-Item -LiteralPath $keysetTemp", "$keysetTarget /setowner '*S-1-5-32-544'",
             "$keysetTarget /reset"]
    positions = [block.index(step) for step in order]
    assert positions == sorted(positions), "trust-folder steps out of order"
    assert "entitlement_signing_keys.json" in block
    assert block.count("Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $updateRoot -Force)") == 2  # again after the helper
    guard = (ROOT / "installer" / "windows" / "data-root-guard.ps1").read_text(encoding="utf-8-sig")
    assert guard.count("if ($LASTEXITCODE -ne 0) { throw") >= 2  # entry ACL failures stop setup


def test_the_vms_reads_the_keyset_from_the_folder_the_installer_writes():
    sys.path.insert(0, str(ROOT / "app"))
    platform_paths = __import__("platform_paths")
    script = _text("install-runtime.ps1")
    assert "Join-Path $DataRoot 'update'" in script and "'entitlement_signing_keys.json'" in script
    iss = _text("AnyAiCam-VMS.iss")
    assert "-DataRoot \"' + ExpandConstant('{commonappdata}\\AnyAiCam')" in iss   # DataRoot = %ProgramData%\AnyAiCam
    if platform_paths.WINDOWS:
        assert str(platform_paths.UPDATE_DIR).lower().endswith(r"anyaicam\update")


def test_windows_never_turns_the_user_writable_1_3_capacity_into_a_trusted_fallback():
    """Step 3 (req 9): 1.3.x kept unsigned capacity under C:\opt (user-modifiable): no migration, by design."""
    script = _text("install-runtime.ps1")
    block = script[script.index("Camera capacity fallback (Linux installer step 15)"):]
    block = block[:block.index("$values = [ordered]@{}")]
    assert "vms_capacity_fallback.json" not in block and "migrate_capacity_fallback" not in block
    assert "is not carried over" in block and "first signed capacity" in block
    assert "Join-Path $env:SystemDrive 'opt\\anyaicam\\data\\config\\vms_capacity.json'" in block
