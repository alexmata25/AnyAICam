#!/usr/bin/env bash
# Installs and bootstraps protected camera-capacity acceptance before the
# installer or a repair can stop/recreate the VMS.

ensure_capacity_accept_data_config_dir() {
    local data_config_dir="${VMS_DATA_CONFIG_DIR:-/var/lib/anyaicam/vms/data-config}"
    local cursor owner_mode owner directory_mode
    local current_uid="$(id -u)"
    local anyaicam_uid=""

    if id -u anyaicam >/dev/null 2>&1; then
        anyaicam_uid="$(id -u anyaicam)"
    fi

    # systemd 226 prepares ReadWritePaths before ExecStart. On a clean
    # installation the normal user/directory provisioning step runs later,
    # so create only missing directories now. Existing state is never
    # chowned or chmodded here; it is checked and left intact.
    cursor="$data_config_dir"
    while [[ "$cursor" != "/" && -n "$cursor" ]]; do
        if [[ -L "$cursor" ]]; then
            echo "[ERROR] Capacity data/config path contains a symlink: $cursor" >&2
            return 1
        fi
        cursor="$(dirname -- "$cursor")"
    done

    if [[ ! -e "$data_config_dir" ]]; then
        (umask 077; mkdir -p -- "$data_config_dir") || {
            echo "[ERROR] Could not create the capacity data/config directory." >&2
            return 1
        }
        chmod 0750 -- "$data_config_dir" || return 1
        # The root-run installer owns the newly-created directory as root;
        # the later provision_users_dirs step assigns it to anyaicam before
        # the VMS is started.
    fi

    [[ -d "$data_config_dir" && ! -L "$data_config_dir" ]] || {
        echo "[ERROR] Capacity data/config path is not a real directory." >&2
        return 1
    }
    owner_mode="$(stat -c '%u:%a' -- "$data_config_dir")" || return 1
    owner="${owner_mode%%:*}"
    directory_mode="${owner_mode#*:}"
    [[ "$owner" == "$current_uid" || "$owner" == "0" || ( -n "$anyaicam_uid" && "$owner" == "$anyaicam_uid" ) ]] || {
        echo "[ERROR] Unsafe owner for capacity data/config directory." >&2
        return 1
    }
    (( (8#$directory_mode & 8#022) == 0 )) || {
        echo "[ERROR] Capacity data/config directory is group- or world-writable." >&2
        return 1
    }
}

install_capacity_accept_step() {
    ensure_capacity_accept_data_config_dir || return 1
    # Install from the verified package payload, not a repository checkout.
    # shellcheck source=../appliance-agent/scripts/lib-capacity-accept.sh
    source "$AGENT_PAYLOAD_DIR/scripts/lib-capacity-accept.sh"
    install_capacity_accept "$AGENT_PAYLOAD_DIR/system"
    # Existing activations are pinned before downtime. A fresh installation
    # creates its appliance ID later, then calls this again after identity setup.
    if [[ -e "$IDENTITY_FILE" ]]; then
        # Existing installations may have an expired but correctly signed
        # last authorization from before protected acceptance was introduced.
        # Only this root-run installer migration path permits that first
        # bootstrap; clean installs and the steady-state watcher require a
        # fresh, unexpired cloud snapshot.
        if [[ "${INSTALL_STATE:-existing}" == "clean" ]]; then
            bootstrap_capacity_identity || return 1
        else
            bootstrap_capacity_identity migration || return 1
        fi
    else
        # No appliance identity exists yet on a clean install. A failed/no-op
        # service start cannot grant capacity and is retried after identity
        # provisioning later in the installer.
        if ! systemctl start anyaicam-capacity-accept.service; then
            log "WARNING: no new capacity snapshot was accepted; the eligible legacy fallback or fail-closed state remains in effect."
        fi
    fi
}
