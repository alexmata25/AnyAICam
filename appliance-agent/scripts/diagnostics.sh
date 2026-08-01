#!/usr/bin/env bash
set -euo pipefail
sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-diagnostics
sudo journalctl -u anyaicam-agent.service -n 50 --no-pager
