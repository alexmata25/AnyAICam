#!/usr/bin/env bash
# OS/arch/root/CPU/RAM/network preflight checks. Sourced by install.sh.
# Disk checks are installation-state-aware and remain in 02-storage-check.sh.

MIN_VCPU=4
# The surviving Aug-24 installer notes do not record an exact RAM threshold.
# 8 GiB is the lowest AnyAiCam hardware sizing baseline (Pi gateway); x86
# customer appliances are sized at 16 GiB or more. Keep this floor explicit
# and independently report/validate the target appliance class before release.
MIN_RAM_GIB=8

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
        log "WARNING: validated target is Ubuntu 24.04; detected $os_version. Continuing, but this is unsupported."
    fi

    local arch
    arch="$(uname -m)"
    if [[ "$arch" != "x86_64" && "$arch" != "aarch64" ]]; then
        echo "Unsupported architecture: $arch" >&2
        exit 1
    fi

    local vcpu mem_kib min_mem_kib
    vcpu="$(nproc)"
    if (( vcpu < MIN_VCPU )); then
        echo "[ERROR] Only $vcpu vCPU detected; at least $MIN_VCPU are required." >&2
        exit 1
    fi
    mem_kib="$(awk '/^MemTotal:/ { print $2; exit }' /proc/meminfo)"
    min_mem_kib=$(( MIN_RAM_GIB * 1024 * 1024 ))
    if [[ -z "$mem_kib" || ! "$mem_kib" =~ ^[0-9]+$ ]]; then
        echo "[ERROR] Could not determine physical RAM from /proc/meminfo." >&2
        exit 1
    fi
    if (( mem_kib < min_mem_kib )); then
        echo "[ERROR] Less than ${MIN_RAM_GIB} GiB RAM detected; at least ${MIN_RAM_GIB} GiB are required by the installer floor." >&2
        exit 1
    fi

    if ! getent hosts github.com >/dev/null 2>&1; then
        log "WARNING: network/DNS check failed (github.com unreachable) -- dependency/image pulls may fail."
    fi
    log "Preflight OK: Ubuntu $os_version, $arch, ${vcpu} vCPU, $((mem_kib / 1024 / 1024)) GiB RAM"
}
