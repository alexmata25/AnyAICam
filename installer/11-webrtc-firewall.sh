#!/usr/bin/env bash
# Installs and applies the WebRTC media-port restriction BEFORE the VMS is
# (re)started with UDP 8189 published (see runtime/anyaicam-webrtc-firewall).

WEBRTC_FIREWALL_SCRIPT="${WEBRTC_FIREWALL_SCRIPT:-/usr/local/sbin/anyaicam-webrtc-firewall}"
WEBRTC_FIREWALL_UNIT="${WEBRTC_FIREWALL_UNIT:-/etc/systemd/system/anyaicam-webrtc-firewall.service}"

install_webrtc_firewall() {
    local script_source="$RUNTIME_DIR/anyaicam-webrtc-firewall" unit_source="$RUNTIME_DIR/anyaicam-webrtc-firewall.service"
    [[ -f "$script_source" && -f "$unit_source" ]] || {
        echo "[ERROR] Missing bundled WebRTC firewall files in $RUNTIME_DIR" >&2
        return 1
    }
    log "Installing WebRTC media-port restriction (UDP 8189: private/Tailscale sources only)..."
    install -m 0755 -o root -g root "$script_source" "$WEBRTC_FIREWALL_SCRIPT"
    install -m 0644 -o root -g root "$unit_source" "$WEBRTC_FIREWALL_UNIT"
    systemctl daemon-reload
    systemctl enable anyaicam-webrtc-firewall.service
    systemctl restart anyaicam-webrtc-firewall.service
    "$WEBRTC_FIREWALL_SCRIPT" check || {
        echo "[ERROR] WebRTC media-port restriction did not apply; refusing to publish UDP 8189." >&2
        return 1
    }
    log "WebRTC media-port restriction active."
}
