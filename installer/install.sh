#!/usr/bin/env bash
# AnyAiCam customer-ready appliance installer -- entrypoint/orchestrator.
# Runtime installers are built artifacts: VMS payload + release metadata are
# embedded at build time from one exact approved release commit/archive.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_VERSION="1.1.0"
PAYLOAD_DIR="$INSTALLER_DIR/payload"
VMS_PAYLOAD_DIR="$PAYLOAD_DIR/vms"
AGENT_PAYLOAD_DIR="$PAYLOAD_DIR/agent"
RUNTIME_DIR="$INSTALLER_DIR/runtime"
RELEASE_ENV_FILE="$INSTALLER_DIR/release.env"

VMS_INSTALL_ROOT=/opt/anyaicam
AGENT_INSTALL_ROOT=/opt/anyaicam-agent
AGENT_SOURCE_ROOT="$AGENT_INSTALL_ROOT/source"
CONFIG_DIR=/etc/anyaicam
VMS_SERVICE_FILE=/etc/systemd/system/anyaicam-vms.service
VERSION_MARKER="$CONFIG_DIR/installed_version"
VMS_RELEASE_MARKER="$CONFIG_DIR/vms_release.json"
IDENTITY_FILE="$CONFIG_DIR/appliance_identity.json"

VMS_RECORDINGS_DIR=/var/lib/anyaicam/vms/recordings
VMS_DATA_CONFIG_DIR=/var/lib/anyaicam/vms/data-config
VMS_HLS_DIR=/var/lib/anyaicam/vms/hls
VMS_ENV_FILE="$CONFIG_DIR/vms.env"
QUARANTINE_DIR="$VMS_RECORDINGS_DIR/quarantine"

VMS_RELEASE_COMMIT=""
VMS_RELEASE_SHA256=""
INSTALLER_SOURCE_COMMIT=""

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Ubuntu 24.04 mawk fix: the loop variable is deliberately `field`, never
# `index`, because a bare `index` shadows mawk's index() builtin.
parse_kv_line() {
    awk -F'=' -v want="$1" '{
        for (field = 1; field <= NF; field++) {
            if ($field ~ "^" want "$") { print $(field + 1); exit }
        }
    }'
}

load_release_metadata() {
    if [[ ! -f "$RELEASE_ENV_FILE" ]]; then
        echo "[ERROR] release.env is missing. Run a built installer artifact; do not install directly from an unbuilt source checkout." >&2
        return 1
    fi
    # shellcheck disable=SC1090
    source "$RELEASE_ENV_FILE"
    if [[ ! "${VMS_RELEASE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]; then
        echo "[ERROR] VMS_RELEASE_COMMIT must be one exact 40-character lowercase Git commit hash." >&2
        return 1
    fi
    if [[ -n "${VMS_RELEASE_SHA256:-}" && ! "${VMS_RELEASE_SHA256}" =~ ^[0-9a-f]{64}$ ]]; then
        echo "[ERROR] VMS_RELEASE_SHA256 is present but is not a lowercase SHA-256." >&2
        return 1
    fi
    if [[ ! "${INSTALLER_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]; then
        echo "[ERROR] INSTALLER_SOURCE_COMMIT must be one exact 40-character lowercase Git commit hash." >&2
        return 1
    fi
    if [[ ! -d "$VMS_PAYLOAD_DIR/app" || ! -f "$VMS_PAYLOAD_DIR/Dockerfile" || \
          ! -f "$VMS_PAYLOAD_DIR/docker-compose.yml" || ! -f "$VMS_PAYLOAD_DIR/requirements.txt" ]]; then
        echo "[ERROR] Built VMS payload is incomplete." >&2
        return 1
    fi
    if [[ ! -d "$AGENT_PAYLOAD_DIR" || ! -f "$AGENT_PAYLOAD_DIR/scripts/install.sh" ]]; then
        echo "[ERROR] Built appliance-agent payload is incomplete." >&2
        return 1
    fi
    if [[ ! -f "$RUNTIME_DIR/anyaicam-vms.service" ]]; then
        echo "[ERROR] Built VMS systemd unit is missing." >&2
        return 1
    fi
    export VMS_RELEASE_COMMIT VMS_RELEASE_SHA256 INSTALLER_SOURCE_COMMIT
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

    load_release_metadata
    log "AnyAiCam appliance installer v$INSTALLER_VERSION starting (requested mode=$mode, VMS=$VMS_RELEASE_COMMIT)"
    preflight_checks
    detect_install_state
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
    stamp_release
    log "Install complete (mode=$mode, detected state=$INSTALL_STATE, VMS=$VMS_RELEASE_COMMIT). Run $INSTALLER_DIR/validate.sh to verify."
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_install "$@"
fi
