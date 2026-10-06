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

# The inverse of install_webrtc_firewall() and of the script's `apply`
# (2026-10-06, Green: uninstall left the unit, the script and the
# ANYAICAM-WEBRTC chain behind). Only what `apply` added is removed: its
# DOCKER-USER jump and its own chain. DOCKER-USER itself belongs to Docker.
remove_webrtc_firewall() {
    local ipt="${ANYAICAM_IPTABLES:-iptables}" port="${ANYAICAM_WEBRTC_UDP_PORT:-8189}" chain="ANYAICAM-WEBRTC"
    local jump=(-p udp -m conntrack --ctorigdstport "$port" --ctdir ORIGINAL -j "$chain")
    systemctl disable --now anyaicam-webrtc-firewall.service 2>/dev/null || true
    rm -f "$WEBRTC_FIREWALL_UNIT" "$WEBRTC_FIREWALL_SCRIPT"
    rm -rf "${WEBRTC_FIREWALL_UNIT}.d"
    while "$ipt" -w -C DOCKER-USER "${jump[@]}" 2>/dev/null; do
        "$ipt" -w -D DOCKER-USER "${jump[@]}" || break
    done
    "$ipt" -w -F "$chain" 2>/dev/null || true
    "$ipt" -w -X "$chain" 2>/dev/null || true
}
