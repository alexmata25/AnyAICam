#!/usr/bin/env bash
# Paid-feature entitlement trust anchor (2026-10-05). Sourced by install.sh.
#
# Provisions the cloud's entitlement-signing PUBLIC keyset the appliance
# trusts for Talk Down / AAC Voice Call (app/appliance_entitlements.py), the
# same way 12-update-signing-key.sh provisions the Software Update key: the
# keyset comes only from this release package, its SHA-256 must match
# release.env, and it is installed root:root 0644 in /etc/anyaicam-update/
# (root:root 0755), outside everything the anyaicam user owns. The VMS
# container reads it through a read-only bind mount (docker-compose.yml).
# Nothing the cloud sends at runtime can add or replace a key. The private
# signing key never exists on the appliance or in this installer.
#
# A release without a keyset leaves an existing one unchanged; an appliance
# that has none runs the VMS normally but denies every paid feature.

# Fixed: the exact path the VMS (app/appliance_entitlements.py) and the
# Software Update applier use. Deliberately not taken from the environment.
ENTITLEMENT_KEYS_DIR="/etc/anyaicam-update"
ENTITLEMENT_KEYS_FILE="$ENTITLEMENT_KEYS_DIR/entitlement_signing_keys.json"

provision_entitlement_signing_keys() {
    local keys_src="$PAYLOAD_DIR/keys/entitlement-signing-public-keys.json"
    install -d -m 0755 -o root -g root "$ENTITLEMENT_KEYS_DIR"
    if [[ -z "${ENTITLEMENT_SIGNING_KEYS_SHA256:-}" ]]; then
        if [[ -f "$ENTITLEMENT_KEYS_FILE" ]]; then
            log "This release provisions no entitlement-signing keyset; the existing one is left unchanged."
        else
            log "[WARN] No entitlement-signing keyset is provisioned: Talk Down and AAC Voice Call stay unavailable on this appliance (the VMS is unaffected)."
        fi
        return 0
    fi
    if [[ ! -f "$keys_src" ]]; then
        echo "[ERROR] release.env names an entitlement-signing keyset but the package does not contain it." >&2
        return 1
    fi
    if [[ "$(sha256sum "$keys_src" | awk '{print $1}')" != "$ENTITLEMENT_SIGNING_KEYS_SHA256" ]]; then
        echo "[ERROR] The packaged entitlement-signing keyset does not match release.env; refusing to trust it." >&2
        return 1
    fi
    if grep -qiE 'PRIVATE|BEGIN|OPENSSH' "$keys_src"; then
        echo "[ERROR] The packaged entitlement-signing keyset contains a key container or private key material; refusing to install it." >&2
        return 1
    fi
    install -m 0644 -o root -g root "$keys_src" "$ENTITLEMENT_KEYS_FILE"
    log "Provisioned the entitlement-signing public keyset at $ENTITLEMENT_KEYS_FILE (sha256 ${ENTITLEMENT_SIGNING_KEYS_SHA256:0:12}...)."
}
