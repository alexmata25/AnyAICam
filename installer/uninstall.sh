#!/usr/bin/env bash
# Full-stack uninstall. Default preserves configuration, identity,
# credentials, camera bindings, recordings, and customer state.
set -euo pipefail
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install.sh
source "$INSTALLER_DIR/install.sh"

run_uninstall() {
    local purge=0
    for arg in "$@"; do
        [[ "$arg" == "--purge-all" ]] && purge=1
    done

    log "Uninstalling AnyAiCam appliance software (purge=$purge)..."
    systemctl disable --now anyaicam-vms.service 2>/dev/null || true
    rm -f "$VMS_SERVICE_FILE"
    # Confirmed live on Ryzen (2026-09-11): a hand-created drop-in
    # override (`.service.d/*.conf`) survives an uninstall that only
    # removes the base unit FILE, never the drop-in DIRECTORY systemd
    # associates with it by name -- the next install then writes a
    # fresh base unit under the same name, and systemd silently
    # reattaches the stale drop-in to it. See the identical fix and full
    # incident writeup in appliance-agent/scripts/uninstall.sh (that
    # incident was the agent unit; this guards the VMS unit against the
    # same class of defect). A drop-in has no legitimate reason to
    # survive an uninstall of the unit it modifies, regardless of who
    # created it or why.
    rm -rf "${VMS_SERVICE_FILE}.d"

    if [[ -d "$VMS_INSTALL_ROOT" ]]; then
        (cd "$VMS_INSTALL_ROOT" && docker compose down 2>/dev/null || true)
    fi

    # Prefer the installed self-contained source copy. A still-present
    # installer payload is only a fallback; never reach into a repo checkout.
    if [[ -f "$AGENT_SOURCE_ROOT/scripts/uninstall.sh" ]]; then
        bash "$AGENT_SOURCE_ROOT/scripts/uninstall.sh" || true
    elif [[ -f "$AGENT_PAYLOAD_DIR/scripts/uninstall.sh" ]]; then
        bash "$AGENT_PAYLOAD_DIR/scripts/uninstall.sh" || true
    else
        systemctl disable --now anyaicam-agent.service 2>/dev/null || true
        rm -f /etc/systemd/system/anyaicam-agent.service
        # Same drop-in-survives-uninstall defect as the VMS unit above --
        # this inline fallback path only runs when neither wrapped agent
        # uninstall script exists, but the same drop-in class of hazard
        # applies to it too.
        rm -rf /etc/systemd/system/anyaicam-agent.service.d
        rm -rf "$AGENT_INSTALL_ROOT"
    fi

    rm -rf "$VMS_INSTALL_ROOT"
    docker image rm anyaicam-vms 2>/dev/null || true
    systemctl daemon-reload

    if [[ "$purge" -eq 1 ]]; then
        log "PURGE requested: removing all preserved state."
        rm -rf "$CONFIG_DIR" /var/lib/anyaicam /var/log/anyaicam
        # Confirmed live: without this, detect_install_state() never
        # reports 0/5 ("clean") again after a purge -- `id -u anyaicam`
        # still succeeds, so the very next install run goes through the
        # existing/repair path (with its much looser storage-preflight
        # floor) instead of the strict 100GB clean-install check, even
        # though every other trace of the appliance is genuinely gone.
        # A true "start over from scratch" reinstall needs the system
        # user gone too, not just its data.
        userdel anyaicam 2>/dev/null || true
    else
        log "Software removed. Preserved: $CONFIG_DIR, /var/lib/anyaicam, /var/log/anyaicam."
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "Run with sudo." >&2
        exit 1
    fi
    run_uninstall "$@"
fi
