#!/usr/bin/env bash
# Full-stack uninstall. Defaults to preserving config, identity,
# credentials, camera bindings, and recordings -- matching the
# existing agent-only uninstall.sh's own stated behavior, extended to
# the VMS side. Pass --purge-all to additionally remove that preserved
# state; never the default.
#
# As of the persistent-layout fix (see 06-deploy-vms.sh), everything
# this default path removes below is genuinely replaceable software:
# real customer/config data no longer lives anywhere under
# $VMS_INSTALL_ROOT, so `rm -rf "$VMS_INSTALL_ROOT"` is now safe on
# every install, including ones that predate this fix -- migration
# runs on the next install/repair before that directory could ever be
# deleted again.
set -euo pipefail
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install.sh
source "$INSTALLER_DIR/install.sh"

run_uninstall() {
    # No root check here -- like every other function this installer
    # sources (deploy_vms, install_agent, ...), the caller is
    # responsible for privilege; the direct-execution guard below is
    # the one place that actually enforces it, so a test harness can
    # call run_uninstall() directly against a fixture without root.
    local purge=0
    for arg in "$@"; do
        [[ "$arg" == "--purge-all" ]] && purge=1
    done

    log "Uninstalling AnyAiCam appliance software (purge=$purge)..."

    systemctl disable --now anyaicam-vms.service 2>/dev/null || true
    rm -f "$VMS_SERVICE_FILE"

    if [[ -d "$VMS_INSTALL_ROOT" ]]; then
        (cd "$VMS_INSTALL_ROOT" && docker compose down 2>/dev/null || true)
    fi

    bash "$REPO_ROOT/appliance-agent/scripts/uninstall.sh" || true

    rm -rf "$VMS_INSTALL_ROOT"
    docker image rm anyaicam-vms 2>/dev/null || true

    systemctl daemon-reload

    if [[ "$purge" -eq 1 ]]; then
        log "PURGE requested: removing all preserved state (config, identity, credentials, camera bindings, recordings)."
        rm -rf "$CONFIG_DIR" /var/lib/anyaicam /var/log/anyaicam
    else
        log "Software removed. Preserved (default): $CONFIG_DIR (config, identity), /var/lib/anyaicam (credentials, camera bindings, recordings, customer state), /var/log/anyaicam."
    fi
}

# Only run when executed directly -- a test harness can safely source
# this file (via install.sh's own guard) purely to reuse run_uninstall()
# against a mocked/fixture environment, without triggering a real
# uninstall as a side effect of sourcing.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "Run with sudo." >&2
        exit 1
    fi
    run_uninstall "$@"
fi
