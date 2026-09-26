"""Regression coverage for a confirmed-live installer gap: scripts/
uninstall.sh only removed the base `anyaicam-agent.service` unit FILE,
never the `.service.d/` drop-in DIRECTORY systemd associates with it by
name. Confirmed live on Ryzen (2026-09-11): a hand-created drop-in
override from an unrelated earlier local-dev session (a BindReadOnlyPaths
pointing at paths that no longer existed) survived a full
`uninstall.sh --purge-all` + fresh reinstall untouched, because the base
unit file it modifies was removed and recreated under the identical name
-- systemd silently reattached the stale drop-in to the freshly
installed unit, crash-looping the agent 600+ times with "Failed to set
up mount namespacing" before this was ever noticed.

Same testing rationale as test_install_script_restarts_service.py's own
docstring: a full behavioral test would need to mock systemctl and
bypass this script's own `set -euo pipefail` + EUID==0 root check --
disproportionate to what's at risk here. This is a structural,
source-text check instead: it proves the shipped script actually
removes the drop-in directory, so a future edit can't silently drop
this fix. The real behavioral proof is the live Ryzen incident this fix
was written to close.
"""

from pathlib import Path

UNINSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "uninstall.sh"


def test_uninstall_script_removes_the_service_dropin_directory():
    source = UNINSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "rm -rf /etc/systemd/system/anyaicam-agent.service.d" in source, (
        "uninstall.sh must remove the .service.d drop-in DIRECTORY, not "
        "just the base unit file -- otherwise a stale drop-in from any "
        "prior source survives uninstall and silently reattaches to the "
        "next fresh install of the same unit name."
    )


def test_uninstall_script_removes_dropin_directory_after_disabling_the_unit():
    """The unit must be disabled/stopped before its drop-in directory is
    removed -- not strictly required for correctness here, but matches
    this script's own established ordering discipline (disable the unit
    before deleting anything it depends on) and keeps the two related
    cleanup lines adjacent rather than scattered."""
    source = UNINSTALL_SCRIPT.read_text(encoding="utf-8")
    disable_index = source.index("systemctl disable --now anyaicam-agent.service")
    dropin_removal_index = source.index("rm -rf /etc/systemd/system/anyaicam-agent.service.d")
    assert dropin_removal_index > disable_index
