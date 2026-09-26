"""Regression coverage for a confirmed-live installer gap: scripts/
install.sh used to only `systemctl enable anyaicam-agent.service`,
never start or restart it. A fresh install left the service enabled
but not running until the next reboot, and every repair/update this
session installed a new package version into the venv while the OLD
version kept running in memory indefinitely -- each of the three RTSP-
authentication fixes required a separate, manual `systemctl restart
anyaicam-agent.service` afterward before the new code actually took
effect.

A full behavioral test would need to mock python3/pip/useradd/id/
install/chown/systemctl as fake executables and bypass this script's
own `set -euo pipefail` + EUID==0 root check -- disproportionate to
what's actually at risk here. This is a structural, source-text check
instead (the same style already used elsewhere in this codebase for
shell/HTML content, e.g. test_website_partner_session_nav_links.py's
own guard test): it proves the shipped script actually calls
`systemctl restart anyaicam-agent.service` after enabling it, so a
future edit can't silently drop the fix back to enable-only. The real
behavioral proof is the live deploy/restart cycle this fix was
verified against on the Samsung appliance.
"""

from pathlib import Path

INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


def test_install_script_restarts_the_agent_service_after_enabling_it():
    source = INSTALL_SCRIPT.read_text(encoding="utf-8")
    enable_index = source.index("systemctl enable anyaicam-agent.service")
    restart_index = source.index("systemctl restart anyaicam-agent.service")
    assert restart_index > enable_index, (
        "systemctl restart anyaicam-agent.service must appear after "
        "systemctl enable anyaicam-agent.service, so both a fresh "
        "install and a repair/update actually end with the service "
        "running the just-installed code, not merely enabled for the "
        "next reboot."
    )


def test_install_script_restarts_after_the_pip_install_step():
    """The restart must come after the pip install of the freshly
    built package -- restarting before that step would just relaunch
    whatever version was already running, defeating the whole point."""
    source = INSTALL_SCRIPT.read_text(encoding="utf-8")
    pip_install_index = source.index("pip install")
    restart_index = source.index("systemctl restart anyaicam-agent.service")
    assert restart_index > pip_install_index


def test_install_script_uses_restart_not_start():
    """`restart` (not `start`) is required so this is safe and
    idempotent on both a fresh install (starts it for the first time --
    restarting a never-started unit is equivalent to starting it) and a
    repair (actually picks up the just-installed version, which a bare
    `start` would skip entirely if the service happened to still be
    marked active)."""
    source = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "systemctl restart anyaicam-agent.service" in source
    assert "systemctl start anyaicam-agent.service" not in source
