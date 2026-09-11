#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib-privileged-watcher.sh
source "$SOURCE_DIR/scripts/lib-privileged-watcher.sh"
systemctl disable --now anyaicam-agent.service 2>/dev/null || true
rm -f /etc/systemd/system/anyaicam-agent.service
# Confirmed live on Ryzen (2026-09-11): a hand-created drop-in override
# (`.service.d/*.conf`, e.g. an ad hoc BindReadOnlyPaths for local dev
# testing) survives this uninstall untouched -- only the base unit FILE
# was ever removed, never the drop-in DIRECTORY systemd associates with
# it by name. The next install then writes a fresh base unit under the
# exact same name, and systemd silently reattaches the stale drop-in to
# it -- on Ryzen this crash-looped the freshly-installed agent 600+
# times with "Failed to set up mount namespacing" because the drop-in's
# bind-mount source no longer existed. A drop-in has no legitimate
# reason to survive an uninstall of the unit it modifies, regardless of
# who created it or why -- removing it here closes this class of defect
# for any future drop-in, not just this one incident.
rm -rf /etc/systemd/system/anyaicam-agent.service.d
# Pairs with install.sh's own install_privileged_watcher() call -- must
# run before /opt/anyaicam-agent is removed below (disable the systemd
# unit before deleting the script it points at, not after) so an
# in-flight trigger can never be left pointed at a just-deleted binary.
uninstall_privileged_watcher
rm -rf /opt/anyaicam-agent
systemctl daemon-reload
echo "Agent removed. Configuration, credentials, logs, and recordings were preserved."
echo "Optional retained paths: /etc/anyaicam /var/lib/anyaicam /var/log/anyaicam"
