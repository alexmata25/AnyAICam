#!/usr/bin/env bash
set -euo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.."&&pwd)"; D="$R/installer/linux-deb/dist/anyaicam-vms_0.1.3+ec5272f-1_amd64.deb"
[[ "$(dpkg-deb -f "$D" Package)" == anyaicam-vms && "$(dpkg-deb -f "$D" Version)" == 0.1.3+ec5272f-1 && "$(dpkg-deb -f "$D" Architecture)" == amd64 ]]
c="$(dpkg-deb -c "$D")"; for p in ./opt/anyaicam/app/ ./opt/anyaicam-agent/source/ ./usr/lib/systemd/system/anyaicam-vms.service ./usr/lib/systemd/system/anyaicam-agent.service ./usr/bin/anyaicam-status ./usr/bin/anyaicam-purge-data; do grep -Fq "$p"<<<"$c"; done
! grep -Eq '\.(db|sqlite|sqlite3)( |$)'<<<"$c"; ! grep -Fq './etc/anyaicam/'<<<"$c"
grep -Fq 'torch==2.5.1+cpu' "$R/requirements-cpu.txt"; grep -Fq 'torchvision==0.20.1+cpu' "$R/requirements-cpu.txt"; grep -Fq 'ultralytics==8.3.40' "$R/requirements.txt"
! grep -Eiq '(cuda|nvidia)' "$R/requirements.txt" "$R/requirements-cpu.txt"; ! grep -Eiq '^pytesseract' "$R/requirements.txt" "$R/requirements-cpu.txt"; ! grep -Fq './app:/app' "$R/installer/linux-deb/runtime/docker-compose.package.yml"
[[ "$(grep -c '^RUN pip install' "$R/installer/linux-deb/runtime/Dockerfile.package")" == 2 ]]
grep -Fq 'PIP_DEFAULT_TIMEOUT=300 PIP_RETRIES=10' "$R/installer/linux-deb/runtime/Dockerfile.package"
grep -Fq 'ConditionPathExists=/var/lib/anyaicam/credential.json' "$R/installer/linux-deb/runtime/anyaicam-agent.service"
grep -Fq 'docker image rm anyaicam-vms:0.1.3-ec5272f' "$R/installer/linux-deb/packaging/postrm"
grep -Fq 'rm -rf /opt/anyaicam-agent' "$R/installer/linux-deb/packaging/postrm"
echo 'Debian package static checks passed.'
