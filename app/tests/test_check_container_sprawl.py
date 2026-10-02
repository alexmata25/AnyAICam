"""Post-cutover container-sprawl safety check (2026-09-17): regression
coverage for deploy/check_container_sprawl.py -- the script that would
have caught the real 2026-09-17 staging OOM outage (8 redundant portal-*
rollback containers left RUNNING instead of stopped on a 3.7GB host,
until the real Linux OOM killer killed the actual live container).

Imports the script directly as a module (deploy/ has no __init__.py and
isn't on the normal app/ package path), mirroring the established pattern
in test_verify_cutover_safety.py. `check()` takes a plain list of running
container names so it's tested without any real Docker/containers
involved; `_running_containers()` (the one function that shells out to
`docker ps`) is exercised separately with subprocess mocked.
"""

import importlib.util
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "check_container_sprawl.py"
_spec = importlib.util.spec_from_file_location("check_container_sprawl", _SCRIPT_PATH)
ccs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccs)


def test_ok_when_only_the_live_container_is_running():
    assert ccs.check(["portal-5b565f6-restored"], "portal-5b565f6-restored") == 0


def test_ok_when_live_plus_exactly_one_immediate_rollback():
    running = ["portal-5b565f6", "portal-rollback-4a84a81"]
    assert ccs.check(running, "portal-5b565f6", max_rollback=1) == 0


def test_blocked_reproduces_the_real_2026_09_17_incident_shape(capsys):
    """The real shape: 1 live container plus 7 other running rollback
    containers nobody had stopped, on a 3.7GB host."""
    running = ["portal-5b565f6-smtp2"] + [f"portal-rollback-{i}" for i in range(7)]
    assert ccs.check(running, "portal-5b565f6-smtp2", max_rollback=1) == 1
    output = capsys.readouterr().out
    assert "CONTAINER_SPRAWL_BLOCKED" in output
    assert "7 non-live portal container(s)" in output


def test_blocked_when_two_rollbacks_running_exceeds_default_max_of_one():
    running = ["portal-live", "portal-rollback-a", "portal-rollback-b"]
    assert ccs.check(running, "portal-live") == 1


def test_blocked_when_designated_live_container_is_not_actually_running(capsys):
    """Catches the other real failure mode this incident also produced:
    the thing everyone believes is live isn't in the running list at all."""
    assert ccs.check(["portal-rollback-old"], "portal-5b565f6") == 1
    output = capsys.readouterr().out
    assert "is not in the running list" in output


def test_stopped_containers_never_count_towards_the_limit():
    """check() only ever receives already-RUNNING names (docker ps, not
    docker ps -a) -- stopped/exited rollbacks are correctly invisible to
    this check by construction, matching the project's rename-not-delete
    convention costing zero RAM once stopped."""
    running_only = ["portal-live"]
    assert ccs.check(running_only, "portal-live", max_rollback=1) == 0


def test_max_rollback_is_configurable_for_a_deliberate_wider_window():
    running = ["portal-live", "portal-rollback-a", "portal-rollback-b"]
    assert ccs.check(running, "portal-live", max_rollback=2) == 0


def test_running_containers_filters_by_prefix_and_calls_real_docker_ps(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, check):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="portal-live\nanyaicam-staging-caddy\nportal-rollback-old\n", stderr="")

    monkeypatch.setattr(ccs.subprocess, "run", fake_run)
    result = ccs._running_containers("portal")
    assert result == ["portal-live", "portal-rollback-old"]
    assert captured["cmd"][:3] == ["docker", "ps", "--format"]


def test_cli_wires_prefix_and_max_rollback_flags_through(monkeypatch):
    monkeypatch.setattr(ccs, "_running_containers", lambda prefix: ["portal-live"])
    monkeypatch.setattr("sys.argv", ["check_container_sprawl.py", "--live", "portal-live", "--prefix", "portal", "--max-rollback", "0"])
    assert ccs.main() == 0
