#!/usr/bin/env bash
set -euo pipefail
DEFER_CAPACITY_BOOTSTRAP=0
if [[ $# -gt 0 ]]; then
  if [[ $# -eq 1 && "$1" == "--defer-capacity-bootstrap" ]]; then
    DEFER_CAPACITY_BOOTSTRAP=1
  else
    echo "Usage: $0 [--defer-capacity-bootstrap]" >&2
    exit 2
  fi
fi
if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
# Root-run code is installed below (the venv, the privileged watcher): never
# with a group/other-writable umask inherited from the invoking login.
umask 022
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib-privileged-watcher.sh
source "$SOURCE_DIR/scripts/lib-privileged-watcher.sh"
source "$SOURCE_DIR/scripts/lib-capacity-accept.sh"
chmod 0755 "$SOURCE_DIR"/scripts/*.sh
id -u anyaicam >/dev/null 2>&1 || useradd --system --home /var/lib/anyaicam --shell /usr/sbin/nologin anyaicam
# /opt/anyaicam-agent holds the venv and the root-only privileged/ folder the
# root watcher executes from: it must be root-owned (2026-10-03, Software
# Update review) or a process running as anyaicam could swap privileged/ and
# have root run its code. The agent only ever reads it. Re-applied on repair.
install -d -m 0755 -o root -g root /opt/anyaicam-agent
install -d -m 0750 -o anyaicam -g anyaicam /etc/anyaicam /var/lib/anyaicam /var/lib/anyaicam/recordings /var/log/anyaicam
python3 -m venv /opt/anyaicam-agent/venv
if [[ "$DEFER_CAPACITY_BOOTSTRAP" == 1 ]]; then
  WHEELHOUSE="${ANYAICAM_AGENT_WHEELHOUSE:-}"
  if [[ -z "$WHEELHOUSE" || ! -d "$WHEELHOUSE" || -L "$WHEELHOUSE" ]]; then
    echo "A root-owned, pre-staged agent wheelhouse is required for deferred full-installer mode." >&2
    exit 1
  fi
  if [[ "$(stat -c '%u' -- "$WHEELHOUSE")" != 0 || "$(stat -c '%a' -- "$WHEELHOUSE")" != 700 ]] \
    || [[ -n "$(find "$WHEELHOUSE" -type l -print -quit 2>/dev/null)" ]]; then
    echo "The deferred agent wheelhouse must be root-owned, mode 0700, and contain no symlinks." >&2
    exit 1
  fi
  /opt/anyaicam-agent/venv/bin/pip install --no-cache-dir --no-index --find-links "$WHEELHOUSE" anyaicam-appliance-agent
else
  /opt/anyaicam-agent/venv/bin/pip install --no-cache-dir "$SOURCE_DIR"
fi
install -m 0644 "$SOURCE_DIR/systemd/anyaicam-agent.service" /etc/systemd/system/anyaicam-agent.service
# Created only when absent, through the root applier's symlink-safe write
# (system/apply_release.py agent_file_main): /etc/anyaicam is owned by the
# anyaicam user, so a plain redirection could be pointed elsewhere.
printf '%s\n' 'op write' 'path /etc/anyaicam/agent.env' 'mode 0600' 'owner anyaicam' 'if_missing' 'content' \
  'ANYAICAM_AGENT_MODE=development' 'ANYAICAM_PORTAL_URL=http://127.0.0.1:8000' \
  | python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import apply_release; sys.exit(apply_release.agent_file_main())' "$SOURCE_DIR/system"
chown -R anyaicam:anyaicam /etc/anyaicam /var/lib/anyaicam /var/log/anyaicam
systemctl daemon-reload
systemctl enable anyaicam-agent.service
# Confirmed live on Samsung: this used to only `enable` the service, never
# start or restart it -- a fresh install left it enabled-but-not-running
# until the next reboot, and a repair/update installed a new package
# version into the venv while the OLD version kept running in memory
# indefinitely (every RTSP-authentication fix this session required a
# separate, manual `systemctl restart anyaicam-agent.service` afterward
# for the new code to actually take effect). `restart`, not `start`, so
# this is safe and idempotent on both a fresh install (starts it for the
# first time) and a repair (picks up whatever was just pip-installed
# above) -- exactly the same pattern installer/08-systemd-setup.sh already
# uses for anyaicam-vms.service.
systemctl restart anyaicam-agent.service
install_privileged_watcher "$SOURCE_DIR"
install_capacity_accept "$SOURCE_DIR/system"
if [[ -f /var/lib/anyaicam/vms/recordings/appliance_identity.json && "$DEFER_CAPACITY_BOOTSTRAP" == 0 ]]; then
  bootstrap_capacity_identity
fi
echo "Installed and running. Run: sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-setup"
