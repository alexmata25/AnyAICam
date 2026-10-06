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
    # Source scripts and the root-only watcher/applier are later consumed as
    # root, so a root-owned symlink in either tree is not a trust anchor: its
    # target may remain writable by the service user. Keep system Python/venv
    # symlinks elsewhere in the agent root valid.
    local code_tree
    for code_tree in "$root/source" "$root/privileged"; do
        if [[ -e "$code_tree" || -L "$code_tree" ]]; then
            [[ -d "$code_tree" && ! -L "$code_tree" ]] || return 1
            [[ -z "$(find "$code_tree" -type l -print -quit 2>/dev/null)" ]] || return 1
        fi
    done
    # Any entry not owned by root, or writable by group/others.
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

# The installed agent source copy (2026-10-06, Green full-lifecycle B-1).
# The payload is unpacked by whichever login user extracted the release
# archive, and `sudo install.sh` then copied it with plain `rsync -a`, which
# kept that user's uid/gid: every file under $AGENT_SOURCE_ROOT stayed owned
# by the customer's login account, while root runs scripts/install.sh from
# it, pip-installs it into the root-owned venv and installs the privileged
# watcher and Software Update applier from it. (06-deploy-vms.sh fixed the
# same class for the VMS tree on 2026-09-24.)
#
# Now: the copy is root-owned (--no-owner/--no-group make root, the
# receiver, the owner), carries no group/other write bit (--chmod=go-w;
# execute bits stay), and anything an earlier install already left with
# the wrong owner or mode -- which rsync skips when unchanged -- is
# re-owned. The payload must contain no symbolic links (the shipped agent
# has none, and a link in a root-installed tree could point anywhere). The
# result is verified, so a failure stops the install instead of leaving an
# untrusted tree behind.
deploy_agent_source() {
    local src="$AGENT_PAYLOAD_DIR" dst="$AGENT_SOURCE_ROOT" link
    link="$(find "$src" -type l -print -quit 2>/dev/null)"
    if [[ -n "$link" ]]; then
        echo "[ERROR] The agent payload contains a symbolic link ($link); refusing to install it." >&2
        return 1
    fi
    if [[ -L "$dst" ]]; then
        echo "[ERROR] $dst is a symbolic link; refusing to install over it." >&2
        return 1
    fi
    install -d -m 0755 -o root -g root "$dst"
    rsync -a --no-owner --no-group --chmod=go-w --delete "$src/" "$dst/"
    # Recheck the copied tree, not only the mutable extracted payload. The
    # scan above and rsync are separate operations; reject a symlink inserted
    # during that interval before any installed code is executed.
    if [[ -n "$(find "$dst" -type l -print -quit 2>/dev/null)" ]]; then
        echo "[ERROR] The copied agent source contains a symbolic link; refusing to execute it." >&2
        return 1
    fi
    find "$dst" -exec chown -h root:root {} +
    find "$dst" \( -type f -o -type d \) -perm /022 -exec chmod go-w {} +
    if ! agent_tree_is_root_controlled "$dst"; then
        echo "[ERROR] $dst is not entirely root-owned and non-writable by others after copying." >&2
        return 1
    fi
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
    deploy_agent_source || return 1

    log "Installing appliance-agent control-plane package..."
    bash "$AGENT_SOURCE_ROOT/scripts/install.sh"
    if [[ "$state" == "clean" ]]; then
        log "Appliance agent installed (clean install)."
    else
        log "Appliance agent verified/updated; agent.env preserved."
    fi
}
