#!/usr/bin/env bash
# Install the appliance agent from the payload embedded with this installer
# source commit. No surrounding repository checkout is required.

install_agent() {
    local state="$1"
    log "Installing python3.12-venv prerequisite for the appliance agent..."
    apt-get update -y
    apt-get install -y python3.12-venv rsync

    [[ -f "$AGENT_PAYLOAD_DIR/scripts/install.sh" ]] || {
        echo "[ERROR] Built appliance-agent payload is missing." >&2
        return 1
    }

    # Keep an installed source copy so uninstall/repair remains self-contained
    # even if the user deletes the downloaded installer archive afterward.
    install -d -m 0755 -o root -g root "$AGENT_SOURCE_ROOT"
    rsync -a --delete "$AGENT_PAYLOAD_DIR/" "$AGENT_SOURCE_ROOT/"

    log "Installing appliance-agent control-plane package..."
    bash "$AGENT_SOURCE_ROOT/scripts/install.sh"
    if [[ "$state" == "clean" ]]; then
        log "Appliance agent installed (clean install)."
    else
        log "Appliance agent verified/updated; agent.env preserved."
    fi
}
