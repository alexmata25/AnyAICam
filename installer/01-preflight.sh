#!/usr/bin/env bash
# OS/arch/root/CPU/RAM/network preflight checks. Sourced by install.sh.
# Disk checks are installation-state-aware and remain in 02-storage-check.sh.

MIN_VCPU=4
# The surviving Aug-24 installer notes do not record an exact RAM threshold.
# 8 GiB is the lowest AnyAiCam hardware sizing baseline (Pi gateway); x86
# customer appliances are sized at 16 GiB or more. Keep this floor explicit
# and independently report/validate the target appliance class before release.
MIN_RAM_GIB=8

# verify_installer_payload (2026-10-01): before anything on this machine is
# changed, every file this package's build recorded in artifact-files.json
# must be present and match its SHA-256 -- a truncated download, a file
# edited after extraction or a mixed folder never reaches an existing
# installation. Read-only; python3 ships with Ubuntu 24.04.
verify_installer_payload() {
    local manifest="${1:-$INSTALLER_DIR/artifact-files.json}"
    if [[ ! -f "$manifest" ]]; then
        echo "[ERROR] This installer package is missing its file manifest (artifact-files.json); download it again." >&2
        return 1
    fi
    python3 - "$manifest" "$(dirname "$manifest")" <<'PYEOF' || {
import hashlib, json, os, sys
manifest, root = sys.argv[1], sys.argv[2]
entries = json.load(open(manifest, encoding="utf-8"))
bad = []
for entry in entries:
    rel = entry.get("path", "")
    path = os.path.normpath(os.path.join(root, rel))
    if rel.startswith("/") or ".." in rel.split("/") or not path.startswith(os.path.normpath(root)):
        bad.append(f"unsafe path {rel!r}"); continue
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        bad.append(f"missing {rel}"); continue
    if digest != entry.get("sha256"):
        bad.append(f"changed {rel}")
if bad or not entries:
    print("; ".join(bad[:10]) or "empty manifest", file=sys.stderr)
    sys.exit(1)
print(f"{len(entries)} files verified")
PYEOF
        echo "[ERROR] This installer package does not match its own file list -- it is incomplete or was modified. Download it again and verify its SHA-256 before running it." >&2
        return 1
    }
}
# /proc/meminfo's MemTotal is always below the installed RAM: firmware,
# the kernel image and (on some PCs) integrated graphics reserve part of it
# before Linux counts anything. A PC sold as "8 GB" reports about
# 7.6-7.9 GiB, so comparing MemTotal to exactly 8 GiB refused every 8 GB
# machine the customer page says is supported (2026-10-01). The floor stays
# 8 GB installed; up to 10% may be reserved (MemTotal >= ~7.2 GiB).
RAM_RESERVED_ALLOWANCE_PERCENT=10
MEMINFO_PATH="${MEMINFO_PATH:-/proc/meminfo}"

# ram_floor_ok MEMTOTAL_KIB -> exit 0 when the machine meets the floor.
ram_floor_ok() {
    local mem_kib="$1"
    [[ "$mem_kib" =~ ^[0-9]+$ ]] || return 2
    local min_kib=$(( MIN_RAM_GIB * 1024 * 1024 * (100 - RAM_RESERVED_ALLOWANCE_PERCENT) / 100 ))
    (( mem_kib >= min_kib ))
}

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
    mem_kib="$(awk '/^MemTotal:/ { print $2; exit }' "$MEMINFO_PATH")"
    if [[ -z "$mem_kib" || ! "$mem_kib" =~ ^[0-9]+$ ]]; then
        echo "[ERROR] Could not determine physical RAM from $MEMINFO_PATH." >&2
        exit 1
    fi
    if ! ram_floor_ok "$mem_kib"; then
        echo "[ERROR] Only $(( mem_kib / 1024 )) MiB of memory is usable; AnyAiCam needs a PC with at least ${MIN_RAM_GIB} GB of RAM installed (16 GB recommended)." >&2
        exit 1
    fi

    if ! getent hosts github.com >/dev/null 2>&1; then
        log "WARNING: network/DNS check failed (github.com unreachable) -- dependency/image pulls may fail."
    fi
    log "Preflight OK: Ubuntu $os_version, $arch, ${vcpu} vCPU, $((mem_kib / 1024 / 1024)) GiB RAM"
}

# WebRTC media port (2026-09-25): the VMS publishes UDP 8189 for same-LAN
# WebRTC viewers (MediaMTX). If another program on the host already holds
# that port, `docker compose up` would fail AFTER the VMS was stopped for
# the update -- so a conflict is refused here, before anything changes.
# Our own publish is not a conflict: on a repair of an appliance already
# publishing it, the only listener is Docker's docker-proxy for the
# anyaicam-vms container.
WEBRTC_UDP_PORT="${WEBRTC_UDP_PORT:-8189}"

webrtc_udp_listeners() {
    ss -H -u -l -n -p "sport = :$WEBRTC_UDP_PORT" 2>/dev/null
}

webrtc_port_published_by_vms() {
    docker port anyaicam-vms "$WEBRTC_UDP_PORT/udp" 2>/dev/null | grep -q ":$WEBRTC_UDP_PORT\$"
}

webrtc_port_preflight() {
    local listeners foreign
    listeners="$(webrtc_udp_listeners)"
    if [[ -z "$listeners" ]]; then
        log "Preflight OK: UDP $WEBRTC_UDP_PORT (WebRTC media) is free"
        return 0
    fi
    foreign="$(grep -v '"docker-proxy"' <<< "$listeners" || true)"
    if [[ -z "$foreign" ]] && webrtc_port_published_by_vms; then
        log "Preflight OK: UDP $WEBRTC_UDP_PORT is already published by this appliance's own VMS container"
        return 0
    fi
    echo "[ERROR] UDP port $WEBRTC_UDP_PORT (WebRTC media for same-LAN live view) is already in use on this host:" >&2
    echo "$listeners" >&2
    echo "Stop or reconfigure the program using it, then re-run the installer. Nothing has been changed." >&2
    return 1
}

# VMS web port (2026-10-01): the VMS publishes TCP 8000. A clean install
# on a machine where another program held it built the whole image (about
# 50 minutes) and only then failed at `docker compose up` with "address
# already in use". Refused here instead, before anything changes. The
# bind test also catches listeners `ss` cannot see (e.g. a port mirrored
# in from another OS). Our own publish is not a conflict.
VMS_HTTP_PORT="${VMS_HTTP_PORT:-8000}"

vms_http_port_published_by_vms() {
    docker port anyaicam-vms "$VMS_HTTP_PORT/tcp" 2>/dev/null | grep -q ":$VMS_HTTP_PORT\$"
}

vms_http_port_bindable() {
    python3 -c 'import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try: s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError: sys.exit(1)
finally: s.close()' "$VMS_HTTP_PORT"
}

vms_http_port_preflight() {
    if vms_http_port_published_by_vms; then
        log "Preflight OK: TCP $VMS_HTTP_PORT is already published by this appliance's own VMS container"
        return 0
    fi
    if vms_http_port_bindable; then
        log "Preflight OK: TCP $VMS_HTTP_PORT (VMS web) is free"
        return 0
    fi
    echo "[ERROR] TCP port $VMS_HTTP_PORT (the AnyAiCam VMS web interface) is already in use on this host:" >&2
    ss -H -t -l -n -p "sport = :$VMS_HTTP_PORT" 2>/dev/null >&2 || true
    echo "Stop or reconfigure the program using it, then re-run the installer. Nothing has been changed." >&2
    return 1
}
