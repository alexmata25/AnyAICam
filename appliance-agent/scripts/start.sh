#!/usr/bin/env bash
set -euo pipefail
sudo systemctl start anyaicam-agent.service
sudo systemctl --no-pager status anyaicam-agent.service
