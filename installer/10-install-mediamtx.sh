#!/usr/bin/env bash
# P2P live-view foundation (2026-09-17), Phase 2: installs the MediaMTX
# binary this appliance's webrtc_publisher.py (app/webrtc_publisher.py)
# bridges to, from the release payload embedded at build time by
# build_release_installer.py -- the same self-contained pattern
# install_agent() already uses for the appliance-agent, deliberately NOT a
# live download from GitHub at install time on a customer's own network.
#
# This step ONLY places the binary and verifies its checksum. It never:
#   - creates a systemd unit for MediaMTX (no anyaicam-mediamtx.service --
#     webrtc_publisher.py owns MediaMTX's process lifecycle itself, the
#     same way this appliance's own VMS process already owns FFmpeg's,
#     and only ever spawns it when ANYAICAM_LIVE_P2P_ENABLED is set)
#   - starts the MediaMTX process
#   - touches ANYAICAM_LIVE_P2P_ENABLED or any other vms.env value
#   - restarts or otherwise disturbs the running anyaicam-vms.service
#
# A fresh install or repair with this step therefore changes nothing
# about live camera behavior -- confirmed by installer/tests/run_tests.sh.

MEDIAMTX_INSTALL_DIR=/opt/anyaicam/mediamtx
MEDIAMTX_BINARY_PATH="$MEDIAMTX_INSTALL_DIR/mediamtx"

install_mediamtx() {
    local state="$1"

    # MediaMTX embedding is opt-in at BUILD time (build_release_installer.py's
    # --mediamtx-binary/--mediamtx-sha256), deliberately not a hard
    # requirement of every release the way the VMS/agent payloads are --
    # this feature is still mid-rollout (P2P stays feature-flagged off
    # regardless), and an ordinary VMS-only rebuild that never touched P2P
    # must keep installing/repairing exactly as it always has. A missing
    # payload is a normal, silent no-op here; a PRESENT-but-corrupt payload
    # (fails its own checksum) is a real build/transfer problem and still
    # fails loudly below -- those are different failure classes and must
    # not be conflated.
    if [[ ! -f "$MEDIAMTX_PAYLOAD_DIR/mediamtx" ]]; then
        log "MediaMTX not included in this release build -- skipping (P2P live view stays unavailable on this appliance until a release that includes it is installed)."
        return 0
    fi
    if [[ ! -f "$MEDIAMTX_PAYLOAD_DIR/mediamtx.sha256" ]]; then
        echo "[ERROR] MediaMTX payload is present but its checksum file is missing." >&2
        return 1
    fi

    log "Verifying MediaMTX payload checksum..."
    (cd "$MEDIAMTX_PAYLOAD_DIR" && sha256sum -c mediamtx.sha256 --status) || {
        echo "[ERROR] MediaMTX payload failed checksum verification -- refusing to install a binary that doesn't match the release manifest." >&2
        return 1
    }

    if [[ -f "$MEDIAMTX_BINARY_PATH" ]] && cmp -s "$MEDIAMTX_PAYLOAD_DIR/mediamtx" "$MEDIAMTX_BINARY_PATH"; then
        log "MediaMTX binary already installed and matches this release (state=$state) -- nothing to do."
        return 0
    fi

    log "Installing MediaMTX binary to $MEDIAMTX_BINARY_PATH..."
    # Ownership (root:root) is set as a separate, best-effort step after
    # the mode-set copy -- matching 06-deploy-vms.sh's own established
    # chown-with-fallback pattern -- rather than baked into one `install
    # -o root -g root` call. A copy this appliance's own installed
    # binary depends on must never be an all-or-nothing failure just
    # because ownership couldn't be changed (e.g. certain restricted or
    # non-standard runtime environments); mode 0755 alone is what
    # actually matters for webrtc_publisher.py to spawn it.
    install -d -m 0755 "$MEDIAMTX_INSTALL_DIR"
    install -m 0755 "$MEDIAMTX_PAYLOAD_DIR/mediamtx" "$MEDIAMTX_BINARY_PATH"
    chown root:root "$MEDIAMTX_INSTALL_DIR" "$MEDIAMTX_BINARY_PATH" 2>/dev/null || true
    log "MediaMTX binary installed (not started -- owned and spawned only by webrtc_publisher.py when ANYAICAM_LIVE_P2P_ENABLED is set)."
}
