"""Codex review 2026-10-30 (a294 review, harness blocker): validate-in-sandbox.ps1 caught a Code Integrity export
exception and wrote it to report.txt only -- report.json, DONE and the totals could still say success. The export is
now an ordinary check (structured FAIL, counted) and the run exits nonzero on any failed check. These tests run the
script's REAL functions under Windows PowerShell with an injected exporter failure."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "sandbox" / "validate-in-sandbox.ps1").read_text(encoding="utf-8-sig")
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="needs Windows PowerShell")


def _functions() -> str:
    # Check, NotTested, Export-CodeIntegrityEvidence, WriteAcceptanceSummary (everything before the trap).
    return SCRIPT[SCRIPT.index("function Check([string]$Name, [scriptblock]$Test) {"):SCRIPT.index("\ntrap {")]


def _exit_block() -> str:
    return SCRIPT[SCRIPT.rindex("\nWriteAcceptanceSummary\n"):]


def _run(tmp_path: Path, inject: bool):
    results = tmp_path / "results"
    results.mkdir()
    body = "\n".join([
        "$ErrorActionPreference = 'Continue'",
        f"$results = '{results}'",
        "$checks = New-Object System.Collections.Generic.List[object]",
        _functions(),
        "function Get-WinEvent { throw 'injected Code Integrity export failure' }" if inject else
        "function Get-WinEvent { }",
        "Check 'an earlier passing check' { 'ok' }",
        _exit_block(),
    ])
    harness = tmp_path / "harness.ps1"
    harness.write_text(body, encoding="utf-8-sig")
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                          capture_output=True, text=True, timeout=120)
    report = json.loads((results / "report.json").read_text(encoding="utf-8-sig"))
    rows = report if isinstance(report, list) else [report]
    return proc.returncode, rows, (results / "DONE").read_text(encoding="utf-8-sig").strip()


def test_an_export_exception_is_a_structured_failed_check_and_a_nonzero_exit(tmp_path):
    code, rows, done = _run(tmp_path, inject=True)
    export = [r for r in rows if r["name"].startswith("Smart App Control evidence (diagnostic)")]
    assert len(export) == 1 and export[0]["status"] == "FAIL" and export[0]["ok"] is False
    assert "injected Code Integrity export failure" in export[0]["detail"]
    assert done.startswith("1 failed")
    assert code == 1


def test_a_clean_export_passes_and_exits_zero(tmp_path):
    code, rows, done = _run(tmp_path, inject=False)
    export = [r for r in rows if r["name"].startswith("Smart App Control evidence (diagnostic)")]
    assert len(export) == 1 and export[0]["status"] == "PASS"
    assert "0 would-block" in export[0]["detail"] and "0 blocked" in export[0]["detail"]
    assert done.startswith("0 failed")
    assert code == 0
