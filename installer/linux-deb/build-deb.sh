#!/usr/bin/env bash
set -euo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.."&&pwd)"; V=0.1.3+ec5272f-1; C=ec5272fb619eda50e188eaac7c6629e1157af3e7
GIT=git; GR="$R"; if grep -qE '^gitdir: [A-Za-z]:' "$R/.git" 2>/dev/null && command -v git.exe >/dev/null; then GIT=git.exe; GR="$(wslpath -w "$R")"; fi
"$GIT" -C "$GR" merge-base --is-ancestor "$C" HEAD || { echo "Authoritative source $C is not an ancestor of HEAD" >&2; exit 1; }
"$GIT" -C "$GR" diff --quiet "$C" HEAD -- app appliance-agent requirements.txt requirements-cpu.txt Dockerfile Dockerfile.production || { echo "Packaged source differs from authoritative source $C" >&2; exit 1; }
$GIT -C "$GR" diff --quiet -- app appliance-agent requirements.txt requirements-cpu.txt Dockerfile Dockerfile.production || exit 1
$GIT -C "$GR" diff --cached --quiet -- app appliance-agent requirements.txt requirements-cpu.txt Dockerfile Dockerfile.production || exit 1
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; P="$W/package"; O="$R/installer/linux-deb/dist"; N="anyaicam-vms_${V}_amd64.deb"
install -d "$P/DEBIAN" "$P/opt/anyaicam" "$P/opt/anyaicam-agent/source" "$P/usr/lib/systemd/system" "$P/usr/bin" "$P/usr/share/doc/anyaicam-vms"
cp "$R/installer/linux-deb/packaging/"* "$P/DEBIAN/"
cp -a "$R/app" "$P/opt/anyaicam/app"
cp "$R/requirements.txt" "$R/requirements-cpu.txt" "$R/installer/linux-deb/runtime/Dockerfile.package" "$P/opt/anyaicam/"
cp "$R/installer/linux-deb/runtime/docker-compose.package.yml" "$P/opt/anyaicam/"
cp -a "$R/appliance-agent/anyaicam_agent" "$R/appliance-agent/scripts" "$P/opt/anyaicam-agent/source/"; cp "$R/appliance-agent/pyproject.toml" "$P/opt/anyaicam-agent/source/"
cp "$R/installer/linux-deb/runtime/anyaicam-vms.service" "$R/installer/linux-deb/runtime/anyaicam-agent.service" "$P/usr/lib/systemd/system/"
cp "$R/installer/linux-deb/runtime/anyaicam-status" "$R/installer/linux-deb/runtime/anyaicam-purge-data" "$P/usr/bin/"; cp "$R/installer/linux-deb/README.md" "$P/usr/share/doc/anyaicam-vms/"
find "$P" -type d -exec chmod 0755 {} +; find "$P" -type f -exec chmod 0644 {} +; chmod 0755 "$P/DEBIAN/"{preinst,postinst,prerm,postrm} "$P/usr/bin/"*
mkdir -p "$O"; dpkg-deb --root-owner-group --build "$P" "$O/$N"; sha256sum "$O/$N">"$O/$N.sha256"; echo "$O/$N"
