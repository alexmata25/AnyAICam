"""Pre-cutover safety check (2026-09-17): regression coverage for
deploy/verify_cutover_safety.py -- the script that would have caught the
real 2026-09-16 staging incident (wrong volume mount -> candidate
container silently ran against an empty database, every login/appliance
call failed for ~45 minutes before a human noticed).

Imports the script directly as a module (deploy/ has no __init__.py and
isn't on the normal app/ package path) and monkeypatches subprocess.run
to simulate `docker exec` output -- no real Docker/containers involved.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "verify_cutover_safety.py"
_spec = importlib.util.spec_from_file_location("verify_cutover_safety", _SCRIPT_PATH)
vcs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vcs)


def _fake_run(counts_by_container: dict[str, dict], unreadable: set[str] = frozenset()):
    def run(cmd, capture_output, text, timeout, check):
        container = cmd[2]  # ["docker", "exec", container, "python3", "-c", script]
        if container in unreadable:
            raise subprocess.CalledProcessError(1, cmd)
        stdout = json.dumps(counts_by_container[container])
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


REAL_LIVE_DATA = {"partner_users": 5, "appliances": 3, "customers": 4, "cameras": 23}
EMPTY_DATABASE = {"partner_users": 0, "appliances": 0, "customers": 0, "cameras": 0}


def test_cutover_allowed_when_candidate_matches_live(monkeypatch):
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"live": REAL_LIVE_DATA, "candidate": REAL_LIVE_DATA}))
    assert vcs.check("live", "candidate") == 0


def test_cutover_allowed_when_candidate_has_more_data_than_live(monkeypatch):
    grown = dict(REAL_LIVE_DATA, cameras=24)
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"live": REAL_LIVE_DATA, "candidate": grown}))
    assert vcs.check("live", "candidate") == 0


def test_cutover_blocked_exactly_reproduces_the_real_incident(monkeypatch, capsys):
    """The real 2026-09-16 shape: live container has real data, the
    wrongly-mounted candidate's database is completely empty."""
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"live": REAL_LIVE_DATA, "candidate": EMPTY_DATABASE}))
    assert vcs.check("live", "candidate") == 1
    output = capsys.readouterr().out
    assert "CUTOVER_BLOCKED" in output
    assert "partner_users: live=5 candidate=0" in output
    assert "appliances: live=3 candidate=0" in output


def test_cutover_blocked_on_a_partial_regression_in_just_one_table(monkeypatch):
    partial = dict(REAL_LIVE_DATA, cameras=0)
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"live": REAL_LIVE_DATA, "candidate": partial}))
    assert vcs.check("live", "candidate") == 1


def test_cutover_blocked_when_candidate_database_is_unreadable(monkeypatch):
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"live": REAL_LIVE_DATA}, unreadable={"candidate"}))
    assert vcs.check("live", "candidate") == 1


def test_cutover_blocked_when_live_is_unreadable_and_not_forced(monkeypatch):
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"candidate": REAL_LIVE_DATA}, unreadable={"live"}))
    assert vcs.check("live", "candidate", force=False) == 1


def test_cutover_allowed_when_live_unreadable_and_forced_for_first_deploy(monkeypatch):
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"candidate": REAL_LIVE_DATA}, unreadable={"live"}))
    assert vcs.check("live", "candidate", force=True) == 0


def test_cli_argument_parsing_wires_force_flag_through(monkeypatch):
    monkeypatch.setattr(vcs.subprocess, "run", _fake_run({"candidate": REAL_LIVE_DATA}, unreadable={"live"}))
    monkeypatch.setattr("sys.argv", ["verify_cutover_safety.py", "--live", "live", "--candidate", "candidate", "--force"])
    assert vcs.main() == 0
