#!/usr/bin/env bash
set -euo pipefail
sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-status
sudo systemctl --no-pager status anyaicam-agent.service
