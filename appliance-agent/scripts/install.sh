#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chmod 0755 "$SOURCE_DIR"/scripts/*.sh
id -u anyaicam >/dev/null 2>&1 || useradd --system --home /var/lib/anyaicam --shell /usr/sbin/nologin anyaicam
install -d -m 0750 -o anyaicam -g anyaicam /opt/anyaicam-agent /etc/anyaicam /var/lib/anyaicam /var/lib/anyaicam/recordings /var/log/anyaicam
python3 -m venv /opt/anyaicam-agent/venv
/opt/anyaicam-agent/venv/bin/pip install --no-cache-dir "$SOURCE_DIR"
install -m 0644 "$SOURCE_DIR/systemd/anyaicam-agent.service" /etc/systemd/system/anyaicam-agent.service
if [[ ! -f /etc/anyaicam/agent.env ]]; then
  install -m 0600 -o anyaicam -g anyaicam /dev/null /etc/anyaicam/agent.env
  printf '%s\n' 'ANYAICAM_AGENT_MODE=development' 'ANYAICAM_PORTAL_URL=http://127.0.0.1:8000' > /etc/anyaicam/agent.env
fi
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
echo "Installed and running. Run: sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-setup"
