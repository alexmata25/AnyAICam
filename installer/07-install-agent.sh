#!/usr/bin/env bash
# Wraps the existing, already-validated appliance-agent installer
# rather than reimplementing it. That script already handles the
# anyaicam user, its own venv/package install, and its own
# create-only-if-absent agent.env -- nothing here duplicates that.

install_agent() {
    local state="$1"
    log "Installing appliance-agent control-plane package..."
    bash "$REPO_ROOT/appliance-agent/scripts/install.sh"
    if [[ "$state" == "clean" ]]; then
        log "Appliance agent installed (clean install)."
    else
        log "Appliance agent verified/updated (existing install) -- agent.env untouched (install.sh only ever creates it if absent)."
    fi
}
