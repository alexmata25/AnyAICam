#!/usr/bin/env bash
# Wraps the existing, already-validated appliance-agent installer
# rather than reimplementing it. That script already handles the
# anyaicam user, its own venv/package install, and its own
# create-only-if-absent agent.env -- nothing here duplicates that.
#
# One OS-level prerequisite that script assumes but does not itself
# install: python3.12-venv, which provides the ensurepip support its
# `python3 -m venv` call needs. Not present on a fresh Ubuntu 24.04
# cloud image -- confirmed by reading appliance-agent/scripts/install.sh
# end-to-end: `useradd`, `install`, `chmod`, `chown`, `python3 -m venv`,
# pip, and `systemctl` are its only OS-level touchpoints, and all but
# the venv module are already present on any base Ubuntu install.
# Installed here, in the reconstructed wrapper, rather than patching
# the reused script.

install_agent() {
    local state="$1"
    log "Installing python3.12-venv prerequisite for the appliance agent..."
    apt-get update -y
    apt-get install -y python3.12-venv
    log "Installing appliance-agent control-plane package..."
    bash "$REPO_ROOT/appliance-agent/scripts/install.sh"
    if [[ "$state" == "clean" ]]; then
        log "Appliance agent installed (clean install)."
    else
        log "Appliance agent verified/updated (existing install) -- agent.env untouched (install.sh only ever creates it if absent)."
    fi
}
