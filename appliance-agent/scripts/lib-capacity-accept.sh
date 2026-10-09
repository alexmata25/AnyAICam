#!/usr/bin/env bash
# Installs the fixed root-only capacity snapshot verifier and its path unit.
# No caller-controlled paths are passed to the helper; service and helper
# paths are fixed in their packaged files.

install_capacity_accept() {
    local source_dir="$1"
    local privileged_dir="/opt/anyaicam-agent/privileged"
    local systemd_dir="/etc/systemd/system"
    local helper_src="$source_dir/capacity_accept.py"
    local path_src="$source_dir/anyaicam-capacity-accept.path"
    local service_src="$source_dir/anyaicam-capacity-accept.service"
    local source
    for source in "$helper_src" "$path_src" "$service_src"; do
        if [[ ! -f "$source" || -L "$source" ]]; then
            echo "[ERROR] Missing or unsafe capacity-accept payload: $source" >&2
            return 1
        fi
    done
    [[ ! -L /opt && ! -L /opt/anyaicam-agent && ! -L "$privileged_dir" &&
       ! -L /etc && ! -L /etc/systemd && ! -L "$systemd_dir" ]] || {
        echo "[ERROR] Unsafe symlink in fixed capacity-accept destination path." >&2; return 1;
    }
    for source in /opt/anyaicam-agent "$privileged_dir" "$systemd_dir"; do
        if [[ -e "$source" ]]; then
            [[ -d "$source" && "$(stat -c '%u' "$source")" == 0 ]] || {
                echo "[ERROR] Unsafe owner/type for $source" >&2; return 1;
            }
            mode="$(stat -c '%a' "$source")"
            (( (8#$mode & 8#022) == 0 )) || { echo "[ERROR] Unsafe permissions for $source" >&2; return 1; }
        fi
    done
    install -d -m 0755 -o root -g root /opt/anyaicam-agent
    install -d -m 0700 -o root -g root "$privileged_dir"
    for source in "$privileged_dir" "$systemd_dir"; do
        [[ -d "$source" ]] || { echo "[ERROR] Missing capacity-accept directory: $source" >&2; return 1; }
        local owner mode
        owner="$(stat -c '%u' "$source")"; mode="$(stat -c '%a' "$source")"
        [[ "$owner" == 0 ]] || { echo "[ERROR] Unsafe owner for $source" >&2; return 1; }
        (( (8#$mode & 8#022) == 0 )) || { echo "[ERROR] Unsafe permissions for $source" >&2; return 1; }
    done
    if [[ ! -x /usr/bin/openssl ]]; then
        apt-get update -y
        apt-get install -y openssl
    fi
    [[ -x /usr/bin/openssl ]] || { echo "[ERROR] OpenSSL is required by the signed-capacity verifier." >&2; return 1; }
    install -d -m 0700 -o root -g root "$privileged_dir"
    for source in "$privileged_dir/capacity_accept.py" \
                  "$systemd_dir/anyaicam-capacity-accept.path" \
                  "$systemd_dir/anyaicam-capacity-accept.service"; do
        if [[ -e "$source" ]]; then
            [[ -f "$source" && ! -L "$source" ]] || { echo "[ERROR] Unsafe existing capacity-accept file: $source" >&2; return 1; }
            [[ "$(stat -c '%u:%h' "$source")" == "0:1" ]] || { echo "[ERROR] Unsafe owner/link count: $source" >&2; return 1; }
            mode="$(stat -c '%a' "$source")"
            (( (8#$mode & 8#022) == 0 )) || { echo "[ERROR] Unsafe permissions: $source" >&2; return 1; }
        fi
    done
    install -m 0700 -o root -g root "$helper_src" "$privileged_dir/capacity_accept.py"
    install -m 0644 -o root -g root "$path_src" "$systemd_dir/anyaicam-capacity-accept.path"
    install -m 0644 -o root -g root "$service_src" "$systemd_dir/anyaicam-capacity-accept.service"
    systemctl daemon-reload
    systemctl enable --now anyaicam-capacity-accept.path
}

bootstrap_capacity_identity() {
    local privileged_dir="/opt/anyaicam-agent/privileged"
    local bootstrap_result
    local legacy_upgrade=()
    if [[ "${1:-}" == "migration" ]]; then
        legacy_upgrade=(--legacy-upgrade)
    fi
    if ! bootstrap_result="$(/usr/bin/python3 -I "$privileged_dir/capacity_accept.py" --bootstrap-identity "${legacy_upgrade[@]}" 2>&1)"; then
        echo "[ERROR] Signed camera-capacity identity bootstrap failed: ${bootstrap_result:-helper returned failure}." >&2
        return 1
    fi
    if [[ "$bootstrap_result" == "awaiting_signed_transition" ]]; then
        echo "[INFO] The installer UUID is staged only as claim correlation; waiting for a matching cloud-signed identity transition before pinning cloud identity or accepting capacity." >&2
    elif [[ "$bootstrap_result" != "unchanged" && "$bootstrap_result" != "bootstrapped" &&
            "$bootstrap_result" != "reconciled_accepted_identity" &&
            "$bootstrap_result" != "reconciled_signed_transition" ]]; then
        echo "[ERROR] Unexpected camera-capacity identity bootstrap result: $bootstrap_result" >&2
        return 1
    fi
    if [[ "${1:-}" == "migration" ]]; then
        if ! /usr/bin/python3 -I "$privileged_dir/capacity_accept.py" --accept-migrated-snapshot; then
            echo "[WARN] Pending migration snapshot was not accepted; existing protected capacity remains authoritative." >&2
        fi
    elif ! systemctl start anyaicam-capacity-accept.service; then
        if [[ -e /var/lib/anyaicam/vms/data-config/vms_capacity_signed.json ]]; then
            # The watcher rejects bad, expired, replayed, or otherwise
            # unauthorized staging without changing protected accepted
            # state. A repair invokes this after deploy_vms has stopped the
            # old container, so rejection must not strand the appliance
            # before systemd_setup can bring the VMS back. Leave the staged
            # file untouched for the owner/cloud retry path; the VMS keeps
            # enforcing the last accepted capacity (or fails closed if none).
            echo "[WARN] Pending signed camera-capacity snapshot was not accepted; existing protected capacity remains authoritative." >&2
        else
            echo "[INFO] No signed camera-capacity snapshot is pending; the claim must complete before cloud capacity can be accepted." >&2
        fi
    fi
    return 0
}

uninstall_capacity_accept() {
    local privileged_dir="/opt/anyaicam-agent/privileged"
    local systemd_dir="/etc/systemd/system"
    systemctl disable --now anyaicam-capacity-accept.path 2>/dev/null || true
    systemctl stop anyaicam-capacity-accept.service 2>/dev/null || true
    rm -f "$systemd_dir/anyaicam-capacity-accept.path" "$systemd_dir/anyaicam-capacity-accept.service"
    rm -f "$privileged_dir/capacity_accept.py"
    systemctl daemon-reload
    # The accepted authorization/trust state under /etc/anyaicam-update is
    # intentionally preserved by a normal uninstall for offline continuity.
}
