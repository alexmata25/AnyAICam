#!/usr/bin/env bash
# Storage preflight -- the known release blocker's fix. Sourced by
# install.sh, called AFTER detect_install_state() has already run, so
# it can branch on the real install state instead of applying one
# unconditional threshold to every run regardless of context (the
# original bug: a second/idempotent run failed with only ~98GB free
# because the installer still demanded the clean-install 100GB minimum
# even though a valid install -- consuming real disk for the Docker
# image/dependencies -- already existed).

CLEAN_INSTALL_MIN_FREE_GB=100
# Working-space estimate for a reinstall/repair: room for the VMS
# image pull/rebuild plus a safety margin -- NOT a repeat of the
# bare-metal minimum a from-scratch OS needs. Deliberately much
# smaller than 100GB; an existing install already has its dependencies
# and only needs room to pull/build an update in place.
EXISTING_INSTALL_MIN_WORKING_GB=15
# A reinstall/repair must never be allowed on a disk that's shrunk
# dramatically since the original install (e.g. an accidentally
# reattached/misconfigured smaller disk) -- 90% of the recorded
# original total is the floor before this fails closed.
TOTAL_CAPACITY_FLOOR_PERCENT=90

free_gb_root() {
    df --output=avail -B1G / | tail -1 | tr -d ' '
}

total_gb_root() {
    df --output=size -B1G / | tail -1 | tr -d ' '
}

storage_preflight() {
    local state="$1" free total
    free="$(free_gb_root)"
    total="$(total_gb_root)"

    if [[ "$state" == "clean" ]]; then
        log "Storage preflight (clean install): ${free}GB free, requiring >= ${CLEAN_INSTALL_MIN_FREE_GB}GB"
        if (( free < CLEAN_INSTALL_MIN_FREE_GB )); then
            echo "[ERROR] Only ${free}GB free storage detected; at least ${CLEAN_INSTALL_MIN_FREE_GB}GB are required for a clean install." >&2
            exit 1
        fi
        # Establishes the total-capacity baseline future reinstalls/
        # repairs are sanity-checked against. Only ever written here,
        # on a clean install -- never overwritten afterward.
        mkdir -p "$CONFIG_DIR"
        echo "$total" > "$CONFIG_DIR/disk_capacity_gb"
    else
        log "Storage preflight (existing install, state=$state): ${free}GB free, requiring >= ${EXISTING_INSTALL_MIN_WORKING_GB}GB working space"
        if (( free < EXISTING_INSTALL_MIN_WORKING_GB )); then
            echo "[ERROR] Only ${free}GB free storage detected; at least ${EXISTING_INSTALL_MIN_WORKING_GB}GB of working space are required to update an existing installation." >&2
            exit 1
        fi
        if [[ -f "$CONFIG_DIR/disk_capacity_gb" ]]; then
            local recorded_total floor
            recorded_total="$(cat "$CONFIG_DIR/disk_capacity_gb")"
            floor=$(( recorded_total * TOTAL_CAPACITY_FLOOR_PERCENT / 100 ))
            if (( total < floor )); then
                echo "[ERROR] Total disk capacity (${total}GB) is significantly smaller than recorded at original install (${recorded_total}GB). Refusing to proceed -- verify the correct disk is attached." >&2
                exit 1
            fi
        fi
    fi
    log "Storage preflight passed (state=$state, free=${free}GB, total=${total}GB)."
}
