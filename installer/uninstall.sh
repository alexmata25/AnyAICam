#!/usr/bin/env bash
# Full-stack uninstall. Defaults to preserving config, identity,
# credentials, camera bindings, and recordings -- matching the
# existing agent-only uninstall.sh's own stated behavior, extended to
# the VMS side. Pass --purge-all to additionally remove that preserved
# state; never the default.
set -euo pipefail
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install.sh
source "$INSTALLER_DIR/install.sh"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

PURGE=0
for arg in "$@"; do
    [[ "$arg" == "--purge-all" ]] && PURGE=1
done

log "Uninstalling AnyAiCam appliance software (purge=$PURGE)..."

systemctl disable --now anyaicam-vms.service 2>/dev/null || true
rm -f "$VMS_SERVICE_FILE"

if [[ -d "$VMS_INSTALL_ROOT" ]]; then
    (cd "$VMS_INSTALL_ROOT" && docker compose down 2>/dev/null || true)
fi

bash "$REPO_ROOT/appliance-agent/scripts/uninstall.sh" || true

rm -rf "$VMS_INSTALL_ROOT"
docker image rm anyaicam-vms 2>/dev/null || true

systemctl daemon-reload

if [[ "$PURGE" -eq 1 ]]; then
    log "PURGE requested: removing all preserved state (config, identity, credentials, camera bindings, recordings)."
    rm -rf "$CONFIG_DIR" /var/lib/anyaicam /var/log/anyaicam
else
    log "Software removed. Preserved (default): $CONFIG_DIR (config, identity), /var/lib/anyaicam (credentials, camera bindings, recordings, customer state), /var/log/anyaicam."
fi
