#!/usr/bin/env bash
# Install the appliance agent from the payload embedded with this installer
# source commit. No surrounding repository checkout is required.

# Legacy agent tree (2026-10-06). Older builds created /opt/anyaicam-agent
# owned by the unprivileged anyaicam user. Root runs code from it: the
# privileged watcher (a root path unit, triggered by the agent) runs
# privileged/watcher.py and the Software Update applier, and this installer
# runs the tree's venv pip. Re-owning it in place would keep anything that
# user planted there, so a tree that is not entirely root-controlled is moved
# aside (root-only, never executed) and a fresh root-owned directory takes
# its place; install_agent() below reinstalls the agent, its venv and the
# privileged files from this verified package. The agent's configuration and
# state live in /etc/anyaicam and /var/lib/anyaicam and are not touched.
# Runs first, before any other installer step. Idempotent: a clean machine or
# a root-controlled tree is left as it is.
agent_tree_is_root_controlled() {
    local root="$1"
    [[ -d "$root" && ! -L "$root" ]] || return 1
    # Any entry not owned by root, or (not a symlink and) writable by group/others.
    [[ -z "$(find "$root" \( \( ! -user root \) -o \( ! -type l -perm /022 \) \) -print -quit 2>/dev/null)" ]]
}

secure_agent_install_root() {
    local root="${AGENT_INSTALL_ROOT:-/opt/anyaicam-agent}"
    [[ -e "$root" || -L "$root" ]] || return 0
    if [[ -L "$root" ]]; then
        echo "[ERROR] $root is a symbolic link; refusing to install over it. Remove it and run the installer again." >&2
        return 1
    fi
    if [[ ! -d "$root" ]]; then
        echo "[ERROR] $root is not a directory; refusing to install over it." >&2
        return 1
    fi
    if agent_tree_is_root_controlled "$root"; then
        return 0
    fi
    local quarantine
    quarantine="${root}.untrusted-$(date -u +%Y%m%dT%H%M%SZ)"
    log "WARNING: $root is not entirely root-controlled (left by an older installer). Stopping the privileged watcher and moving it aside to $quarantine; the agent is reinstalled from this package."
    systemctl stop anyaicam-privileged-watcher.path anyaicam-privileged-watcher.service 2>/dev/null || true
    mv "$root" "$quarantine"
    chown -h root:root "$quarantine"
    chmod 0700 "$quarantine"
    install -d -m 0755 -o root -g root "$root"
}

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
