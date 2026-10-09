#!/usr/bin/env bash
# Installs and bootstraps protected camera-capacity acceptance before the
# installer or a repair can stop/recreate the VMS.

install_capacity_accept_step() {
    # Install from the verified package payload, not a repository checkout.
    # shellcheck source=../appliance-agent/scripts/lib-capacity-accept.sh
    source "$AGENT_PAYLOAD_DIR/scripts/lib-capacity-accept.sh"
    install_capacity_accept "$AGENT_PAYLOAD_DIR/system"
    # Existing activations are pinned before downtime. A fresh installation
    # creates its appliance ID later, then calls this again after identity setup.
    if [[ -e "$IDENTITY_FILE" ]]; then
        # Existing installations may have an expired but correctly signed
        # last authorization from before protected acceptance was introduced.
        # Only this root-run installer migration path permits that first
        # bootstrap; clean installs and the steady-state watcher require a
        # fresh, unexpired cloud snapshot.
        if [[ "${INSTALL_STATE:-existing}" == "clean" ]]; then
            bootstrap_capacity_identity || return 1
        else
            bootstrap_capacity_identity migration || return 1
        fi
    else
        # No appliance identity exists yet on a clean install. A failed/no-op
        # service start cannot grant capacity and is retried after identity
        # provisioning later in the installer.
        if ! systemctl start anyaicam-capacity-accept.service; then
            log "WARNING: no new capacity snapshot was accepted; the eligible legacy fallback or fail-closed state remains in effect."
        fi
    fi
}
