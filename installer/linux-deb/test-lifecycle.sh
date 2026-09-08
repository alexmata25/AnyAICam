#!/usr/bin/env bash
set -euo pipefail
[[ "${ANYAICAM_DESTRUCTIVE_PACKAGE_TEST:-}" == 1 ]] || { echo 'Set ANYAICAM_DESTRUCTIVE_PACKAGE_TEST=1 in a disposable Ubuntu host.' >&2; exit 2; }
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 2; }
D="${1:?deb path required}"
S=/var/lib/anyaicam/vms/recordings/.package-lifecycle-state
C=/etc/anyaicam/.package-lifecycle-config

apt-get install -y "$D"
systemctl is-enabled --quiet anyaicam-vms.service
systemctl is-active --quiet anyaicam-vms.service
systemctl is-enabled --quiet anyaicam-agent.service
systemctl is-active --quiet anyaicam-agent.service && { echo 'Unactivated agent must remain stopped.' >&2; exit 1; } || true
curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null
printf 'recording-preservation-test\n' > "$S"
printf 'configuration-preservation-test\n' > "$C"
before="$(sha256sum "$S" "$C")"
apt-get remove -y anyaicam-vms
[[ ! -e /opt/anyaicam && ! -e /opt/anyaicam-agent ]]
[[ ! -e /usr/lib/systemd/system/anyaicam-vms.service && ! -e /usr/lib/systemd/system/anyaicam-agent.service ]]
! docker image inspect anyaicam-vms:0.1.3-ec5272f >/dev/null 2>&1
[[ "$before" == "$(sha256sum "$S" "$C")" ]]
apt-get install -y "$D"
systemctl is-enabled --quiet anyaicam-vms.service
systemctl is-active --quiet anyaicam-vms.service
curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null
[[ "$before" == "$(sha256sum "$S" "$C")" ]]
echo 'Debian package install/remove/reinstall lifecycle passed.'
