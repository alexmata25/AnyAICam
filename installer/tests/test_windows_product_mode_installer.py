"""Requirement 9 (1.2.7), Windows step 5: Local / Hybrid selection, with the rules of Linux installer/03-product-mode.sh.

The decision block of install-runtime.ps1 is extracted and EXECUTED in Windows PowerShell for every case (it needs no
Administrator rights); the Setup page wiring in AnyAiCam-VMS.iss is checked statically."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _block():
    script = (WIN / "install-runtime.ps1").read_text(encoding="utf-8-sig")
    start = script.index("$savedMode = ''")
    end = script.index("# What /version reports")
    return script[start:end]


def _decide(env: dict, had_file: bool, mode: str):
    """Run the real block; return (ok, resulting values or the error message)."""
    setup = "$values = [ordered]@{}\n" + "".join(f"$values['{k}'] = '{v}'\n" for k, v in env.items())
    setup += f"$hadEnvironmentFile = ${str(had_file).lower()}\n$ProductMode = '{mode}'\n"
    harness = ("$ErrorActionPreference = 'Stop'\ntry {\n" + setup + _block() +
               "\n$values | ConvertTo-Json -Compress\n} catch { 'ERROR: ' + $_.Exception.Message }")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", harness],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    if out.startswith("ERROR: "):
        return False, out[7:]
    return True, json.loads(out) if out else {}


needs_powershell = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="needs Windows PowerShell")


@needs_powershell
@pytest.mark.parametrize("mode", ["local", "hybrid"])
def test_a_new_installation_records_the_chosen_mode_as_the_bootstrap_mode(mode):
    ok, values = _decide({}, had_file=False, mode=mode)
    assert ok and values == {"ANYAICAM_PRODUCT_MODE_BOOTSTRAP": mode}


@needs_powershell
def test_a_new_installation_without_a_mode_is_refused():
    ok, error = _decide({}, had_file=False, mode="")
    assert not ok and "needs a product mode" in error


@needs_powershell
@pytest.mark.parametrize("key", ["ANYAICAM_PRODUCT_MODE", "ANYAICAM_PRODUCT_MODE_BOOTSTRAP"])
def test_an_upgrade_keeps_the_saved_mode_and_never_switches_it(key):
    ok, values = _decide({key: "local"}, had_file=True, mode="")
    assert ok and values == {key: "local"}
    ok, values = _decide({key: "local"}, had_file=True, mode="local")
    assert ok and values == {key: "local"}
    ok, error = _decide({key: "local"}, had_file=True, mode="hybrid")
    assert not ok and "cannot switch the existing product mode" in error


@needs_powershell
def test_an_earlier_installation_without_a_recorded_mode_is_left_unset():
    ok, values = _decide({"ANYAICAM_APP_SECRETS": "x"}, had_file=True, mode="")
    assert ok and "ANYAICAM_PRODUCT_MODE_BOOTSTRAP" not in values


@needs_powershell
@pytest.mark.parametrize("env,mode,needle", [
    ({}, "Local", "exactly local or hybrid"),                                      # case-sensitive like Linux
    ({}, "cloud", "exactly local or hybrid"),
    ({"ANYAICAM_PRODUCT_MODE_BOOTSTRAP": "Hybrid"}, "", "must be exactly local or hybrid"),
])
def test_anything_but_exactly_local_or_hybrid_is_refused(env, mode, needle):
    ok, error = _decide(env, had_file=bool(env), mode=mode)
    assert not ok and needle in error


def test_setup_offers_the_choice_only_on_a_new_interactive_install_and_passes_it():
    iss = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8-sig")
    assert "CreateInputOptionPage(GatewayEditionPage.ID, 'How will you use AnyAiCam?'" in iss
    assert "ProductModePage.Add('Local:" in iss and "ProductModePage.Add('Hybrid:" in iss
    assert "(ExistingInstall() or (ExpandConstant('{param:ProductMode|}') <> ''))" in iss       # skipped on upgrade / silent param
    assert "not ExistingInstall() and not WizardSilent()" in iss                                # silent new install: no default
    assert "-ProductMode \"' + ProductModeChoice() + '\" -GatewayEdition" in iss
    script = (WIN / "install-runtime.ps1").read_text(encoding="utf-8-sig")
    assert "[string]$ProductMode = ''" in script


def test_the_persisted_mode_never_defaults_to_a_user_writable_windows_folder():
    source = (ROOT / "app" / "product_mode.py").read_text(encoding="utf-8")
    assert 'str(_CONFIG_DIR / "product_mode.json")' in source and '"/opt/anyaicam/data/config/product_mode.json",' not in source


# ------------------------------------------------------------ 1.3.2 Sandbox acceptance (2026-10-20)
# A refused mode switch used to be decided only in install-runtime.ps1, AFTER PrepareToInstall had stopped and
# uninstalled both services: the PC was left with no running VMS (setup exit 20). Setup now refuses BEFORE any change.
def _code():
    return (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8-sig").split("[Code]", 1)[1]


def _function(name):
    code = _code()
    return code.split(f"function {name}", 1)[1].split("\nend;\n", 1)[0]


def test_setup_checks_the_mode_before_stopping_or_removing_any_service():
    prepare = _function("PrepareToInstall(var NeedsRestart: Boolean): String;")
    check = prepare.index("Result := ProductModeProblem();")
    assert check < prepare.index("if Result <> '' then begin", check)
    assert prepare.index("Result := GatewayEditionProblem();") < check
    assert check < prepare.index("'stopwait'") and check < prepare.index("'uninstall'")
    assert "Log('Install refused before any change: ' + Result);" in prepare
    assert prepare.index("exit;") < prepare.index("'stopwait'")                       # refused -> returns before any change


def test_the_early_check_applies_exactly_install_runtimes_rules_and_messages():
    problem = _function("ProductModeProblem(): String;")
    saved = _function("SavedProductMode(var Mode: String): Boolean;")
    runtime = (WIN / "install-runtime.ps1").read_text(encoding="utf-8-sig")
    # same requested-mode rule and message
    assert "Requested := ProductModeChoice();" in problem
    assert "'The product mode must be exactly local or hybrid.'" in problem
    assert "throw 'The product mode must be exactly local or hybrid.'" in runtime
    # same switch refusal message
    assert "'Setup cannot switch the existing product mode (' + Saved + '); change it from your AnyAiCam account instead.'" in problem
    assert 'throw "Setup cannot switch the existing product mode ($savedMode); change it from your AnyAiCam account instead."' in runtime
    # same key order (PRODUCT_MODE before BOOTSTRAP) and exact, case-sensitive values
    assert saved.index("'ANYAICAM_PRODUCT_MODE='") < saved.index("'ANYAICAM_PRODUCT_MODE_BOOTSTRAP='")
    assert "foreach ($key in @('ANYAICAM_PRODUCT_MODE', 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP'))" in runtime
    assert "(Saved <> 'local') and (Saved <> 'hybrid')" in problem
    # an unreadable vms.env is left to install-runtime (which first repairs the permissions)
    assert "if not SavedProductMode(Saved) or (Saved = '') then exit;" in problem
    assert r"LoadStringsFromFile(ExpandConstant('{commonappdata}\AnyAiCam\config\vms.env'), Lines)" in saved
