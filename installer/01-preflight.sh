#!/usr/bin/env bash
# OS/arch/root/network preflight checks. Sourced by install.sh.

preflight_checks() {
    log "Running preflight checks..."
    if [[ $EUID -ne 0 ]]; then
        echo "This installer must be run as root (sudo)." >&2
        exit 1
    fi
    if [[ ! -f /etc/os-release ]]; then
        echo "Cannot determine OS version (/etc/os-release missing)." >&2
        exit 1
    fi
    local os_id os_version
    os_id="$(parse_kv_line ID </etc/os-release | tr -d '"')"
    os_version="$(parse_kv_line VERSION_ID </etc/os-release | tr -d '"')"
    if [[ "$os_id" != "ubuntu" ]]; then
        echo "This installer supports Ubuntu only (detected: $os_id)." >&2
        exit 1
    fi
    if [[ "$os_version" != "24.04" ]]; then
        log "WARNING: validated on Ubuntu 24.04; detected $os_version. Continuing, but this is unsupported."
    fi
    local arch
    arch="$(uname -m)"
    if [[ "$arch" != "x86_64" && "$arch" != "aarch64" ]]; then
        echo "Unsupported architecture: $arch" >&2
        exit 1
    fi
    if ! getent hosts github.com >/dev/null 2>&1; then
        log "WARNING: network/DNS check failed (github.com unreachable) -- Docker image pulls may fail."
    fi
    log "Preflight OK: Ubuntu $os_version, $arch"
}
