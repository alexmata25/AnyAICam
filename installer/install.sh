#!/usr/bin/env bash
# AnyAiCam customer-ready appliance installer -- entrypoint/orchestrator.
#
# Reconstructed from Codex's handoff notes (the original branch/commit
# was never pushed before the disposable instance that built it was
# terminated -- see README.md). Known Ubuntu 24.04 fix baked in below:
# mawk (the default /usr/bin/awk on 24.04) treats a bare `index` used
# as a for-loop variable name as shadowing its own builtin index()
# function, corrupting field parsing -- the loop variable in
# parse_kv_line() below is deliberately named `field`, never `index`.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$INSTALLER_DIR/.." && pwd)"
INSTALLER_VERSION="1.0.0"
VMS_INSTALL_ROOT=/opt/anyaicam
# Path constants defined once, here, and referenced by variable
# everywhere else in this installer (03-detect-install.sh,
# 02-storage-check.sh, 05-provision-users-dirs.sh, 08-systemd-setup.sh,
# validate.sh) -- never re-hardcoded -- so every script agrees on the
# same literal path and so these can be overridden by a test harness
# (installer/tests/) without touching real system paths.
CONFIG_DIR=/etc/anyaicam
VMS_SERVICE_FILE=/etc/systemd/system/anyaicam-vms.service
VERSION_MARKER="$CONFIG_DIR/installed_version"
IDENTITY_FILE="$CONFIG_DIR/appliance_identity.json"
QUARANTINE_DIR=/var/lib/anyaicam/vms/recordings/quarantine

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# A tiny, reusable "read the value for KEY from KEY=VALUE lines"
# helper -- this is the function whose original implementation used
# `index` as the for-loop variable and broke under mawk. NF and the
# loop counter are both plain awk builtins; only the *name* of the
# loop variable was ever the bug.
parse_kv_line() {
    awk -F'=' -v want="$1" '{
        for (field = 1; field <= NF; field++) {
            if ($field ~ "^" want "$") { print $(field + 1); exit }
        }
    }'
}

# shellcheck source=01-preflight.sh
source "$INSTALLER_DIR/01-preflight.sh"
# shellcheck source=03-detect-install.sh
source "$INSTALLER_DIR/03-detect-install.sh"
# shellcheck source=02-storage-check.sh
source "$INSTALLER_DIR/02-storage-check.sh"
# shellcheck source=04-docker-setup.sh
source "$INSTALLER_DIR/04-docker-setup.sh"
# shellcheck source=05-provision-users-dirs.sh
source "$INSTALLER_DIR/05-provision-users-dirs.sh"
# shellcheck source=06-deploy-vms.sh
source "$INSTALLER_DIR/06-deploy-vms.sh"
# shellcheck source=07-install-agent.sh
source "$INSTALLER_DIR/07-install-agent.sh"
# shellcheck source=08-systemd-setup.sh
source "$INSTALLER_DIR/08-systemd-setup.sh"
# shellcheck source=09-identity.sh
source "$INSTALLER_DIR/09-identity.sh"

run_install() {
    local mode="install"
    for arg in "$@"; do
        case "$arg" in
            --repair) mode="repair" ;;
            *) echo "Unknown argument: $arg" >&2; return 2 ;;
        esac
    done

    log "AnyAiCam appliance installer v$INSTALLER_VERSION starting (requested mode=$mode)"
    preflight_checks
    detect_install_state   # sets INSTALL_STATE=clean|existing|partial
    if [[ "$mode" == "install" && "$INSTALL_STATE" == "partial" ]]; then
        log "Partial installation detected -- treating as repair, never silently as clean."
        mode="repair"
    fi
    storage_preflight "$INSTALL_STATE"
    docker_setup
    provision_users_dirs "$INSTALL_STATE"
    deploy_vms "$INSTALL_STATE"
    install_agent "$INSTALL_STATE"
    systemd_setup
    identity_provision "$INSTALL_STATE"
    log "Install complete (mode=$mode, detected state=$INSTALL_STATE). Run $INSTALLER_DIR/validate.sh to verify."
}

# Only run when executed directly -- validate.sh and uninstall.sh
# source this file purely to reuse log()/parse_kv_line()/the shared
# path constants/detect_install_state(), without triggering a full
# install run.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_install "$@"
fi
