#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
systemctl disable --now anyaicam-agent.service 2>/dev/null || true
rm -f /etc/systemd/system/anyaicam-agent.service
rm -rf /opt/anyaicam-agent
systemctl daemon-reload
echo "Agent removed. Configuration, credentials, logs, and recordings were preserved."
echo "Optional retained paths: /etc/anyaicam /var/lib/anyaicam /var/log/anyaicam"
