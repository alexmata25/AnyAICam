#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib-privileged-watcher.sh
source "$SOURCE_DIR/scripts/lib-privileged-watcher.sh"
systemctl disable --now anyaicam-agent.service 2>/dev/null || true
rm -f /etc/systemd/system/anyaicam-agent.service
# Pairs with install.sh's own install_privileged_watcher() call -- must
# run before /opt/anyaicam-agent is removed below (disable the systemd
# unit before deleting the script it points at, not after) so an
# in-flight trigger can never be left pointed at a just-deleted binary.
uninstall_privileged_watcher
rm -rf /opt/anyaicam-agent
systemctl daemon-reload
echo "Agent removed. Configuration, credentials, logs, and recordings were preserved."
echo "Optional retained paths: /etc/anyaicam /var/lib/anyaicam /var/log/anyaicam"
