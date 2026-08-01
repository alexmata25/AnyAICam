#!/usr/bin/env bash
set -euo pipefail
sudo systemctl restart anyaicam-agent.service
sudo systemctl --no-pager status anyaicam-agent.service
