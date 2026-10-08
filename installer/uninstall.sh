#!/usr/bin/env bash
# Full-stack uninstall. Default preserves configuration, identity,
# credentials, camera bindings, recordings, and customer state.
set -euo pipefail
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install.sh
source "$INSTALLER_DIR/install.sh"

# Every tag of the VMS image (latest, rollback-*, release-*, before-rollback),
# never any other image. Untagging the last tag deletes the image.
remove_vms_images() {
    local ref
    docker image ls --format '{{.Repository}}:{{.Tag}}' "$VMS_IMAGE" 2>/dev/null | while read -r ref; do
        [[ "$ref" == "$VMS_IMAGE:"* && "$ref" != *"<none>"* ]] || continue
        docker image rm "$ref" >/dev/null 2>&1 || true
    done
}

# The quarantined legacy agent trees secure_agent_install_root() moved aside
# (real directories only; never through a link).
remove_agent_quarantine() {
    local dir
    for dir in "$AGENT_INSTALL_ROOT".untrusted-*; do
        [[ -d "$dir" && ! -L "$dir" ]] || continue
        rm -rf -- "$dir"
    done
}

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
    # Software Update (2026-10-03): the staged / previous / failed application trees.
    rm -rf "$VMS_INSTALL_ROOT.next" "$VMS_INSTALL_ROOT.previous" "$VMS_INSTALL_ROOT.failed"
    rm -f /usr/local/sbin/anyaicam-rollback
    desktop_setup_remove
    docker image rm anyaicam-vms 2>/dev/null || true
    # The VMS is gone, so UDP 8189 is no longer published: its restriction goes too.
    remove_webrtc_firewall
    systemctl daemon-reload

    if [[ "$purge" -eq 1 ]]; then
        log "PURGE requested: removing all preserved state."
        rm -rf "$CONFIG_DIR" /var/lib/anyaicam /var/log/anyaicam
        rm -rf /etc/anyaicam-update /var/lib/anyaicam-update
        # (2026-10-06, Green) What a full purge used to leave behind: the
        # rollback/release image tags (their rollback points were just
        # removed above), the inert root-only quarantine copies of a
        # legacy agent tree, and the suspend/hibernate masks.
        remove_vms_images
        remove_agent_quarantine
        restore_system_suspend
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
        log "Software removed. Preserved: $CONFIG_DIR, /var/lib/anyaicam, /var/log/anyaicam, /etc/anyaicam-update, /var/lib/anyaicam-update."
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "Run with sudo." >&2
        exit 1
    fi
    run_uninstall "$@"
fi
