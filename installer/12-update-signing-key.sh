#!/usr/bin/env bash
# Software Update trust anchor (2026-10-03). Sourced by install.sh.
#
# Provisions the release-signing PUBLIC key the appliance trusts for Software
# Update, and root's own update-state directory. Both live outside every
# directory the unprivileged anyaicam user owns (/etc/anyaicam and
# /var/lib/anyaicam are chowned to it): /etc/anyaicam-update/ and
# /var/lib/anyaicam-update/ are root:root 0755, the key root:root 0644.
# The agent can read the key to pre-check a release; only root can change it.
# The private key never exists on the appliance or in this installer.

UPDATE_KEY_DIR="${ANYAICAM_UPDATE_KEY_DIR:-/etc/anyaicam-update}"
UPDATE_KEY_FILE="$UPDATE_KEY_DIR/trusted_signing_key.pem"
UPDATE_STATE_DIR="${ANYAICAM_UPDATE_STATE_DIR:-/var/lib/anyaicam-update}"

provision_update_signing_key() {
    local key_src="$PAYLOAD_DIR/keys/update-signing-public-key.pem"
    install -d -m 0755 -o root -g root "$UPDATE_STATE_DIR" "$UPDATE_STATE_DIR/results"
    install -d -m 0700 -o root -g root "$UPDATE_STATE_DIR/work"
    if [[ -z "${UPDATE_SIGNING_KEY_SHA256:-}" ]]; then
        log "This release provisions no update-signing key; an existing key (if any) is left unchanged."
        return 0
    fi
    if [[ ! -f "$key_src" ]]; then
        echo "[ERROR] release.env names an update-signing key but the package does not contain it." >&2
        return 1
    fi
    if [[ "$(sha256sum "$key_src" | awk '{print $1}')" != "$UPDATE_SIGNING_KEY_SHA256" ]]; then
        echo "[ERROR] The packaged update-signing key does not match release.env; refusing to trust it." >&2
        return 1
    fi
    if grep -q 'PRIVATE KEY' "$key_src"; then
        echo "[ERROR] The packaged update-signing key is a private key; refusing to install it." >&2
        return 1
    fi
    install -d -m 0755 -o root -g root "$UPDATE_KEY_DIR"
    install -m 0644 -o root -g root "$key_src" "$UPDATE_KEY_FILE"
    log "Provisioned the Software Update signing public key at $UPDATE_KEY_FILE (sha256 ${UPDATE_SIGNING_KEY_SHA256:0:12}...)."
}
