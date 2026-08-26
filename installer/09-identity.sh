#!/usr/bin/env bash
# Stable appliance identity: generated once, on a genuinely clean
# install, and never regenerated afterward -- this is exactly what
# "identity/config hashes unchanged" validates across reinstall/repair.

identity_provision() {
    local state="$1"
    if [[ -f "$IDENTITY_FILE" ]]; then
        log "Existing appliance identity found -- preserved, not regenerated (sha256=$(sha256sum "$IDENTITY_FILE" | cut -d' ' -f1))."
        return 0
    fi
    if [[ "$state" != "clean" ]]; then
        log "WARNING: no identity file found on a non-clean install -- this indicates a genuinely partial installation. Generating a new identity as part of repair."
    fi
    local appliance_id
    appliance_id="$(cat /proc/sys/kernel/random/uuid)"
    umask 077
    {
        echo "{"
        echo "  \"appliance_id\": \"$appliance_id\","
        echo "  \"installer_version\": \"$INSTALLER_VERSION\","
        echo "  \"installed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        echo "}"
    } > "$IDENTITY_FILE"
    chown anyaicam:anyaicam "$IDENTITY_FILE"
    chmod 0600 "$IDENTITY_FILE"
    echo "$INSTALLER_VERSION" > "$VERSION_MARKER"
    log "Generated new appliance identity: $appliance_id"
}
