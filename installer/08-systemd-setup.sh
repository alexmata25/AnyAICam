#!/usr/bin/env bash
# Install the VMS unit bundled in the exact installer artifact.

systemd_setup() {
    local unit_source="$RUNTIME_DIR/anyaicam-vms.service"
    [[ -f "$unit_source" ]] || {
        echo "[ERROR] Missing bundled VMS systemd unit: $unit_source" >&2
        return 1
    }
    log "Installing anyaicam-vms.service..."
    install -m 0644 "$unit_source" "$VMS_SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable anyaicam-vms.service
    systemctl restart anyaicam-vms.service
    log "anyaicam-vms.service enabled for multi-user.target and started."
}
