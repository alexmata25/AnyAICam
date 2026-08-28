#!/usr/bin/env bash
# Stable appliance identity + mutable installed-release marker.

identity_provision() {
    local state="$1"
    if [[ -f "$IDENTITY_FILE" ]]; then
        log "Existing appliance identity found -- preserved, not regenerated (sha256=$(sha256sum "$IDENTITY_FILE" | cut -d' ' -f1))."
    else
        if [[ "$state" != "clean" ]]; then
            log "WARNING: no identity file found on a non-clean install; generating one as part of repair."
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
        log "Generated new appliance identity: $appliance_id"
    fi

    # Installer version is mutable metadata; identity remains stable.
    printf '%s\n' "$INSTALLER_VERSION" > "$VERSION_MARKER"
    chmod 0644 "$VERSION_MARKER"
}

stamp_release() {
    umask 022
    {
        echo "{"
        echo "  \"vms_release_commit\": \"$VMS_RELEASE_COMMIT\","
        echo "  \"release_archive_sha256\": \"${VMS_RELEASE_SHA256:-}\","
        echo "  \"installer_source_commit\": \"$INSTALLER_SOURCE_COMMIT\","
        echo "  \"installer_version\": \"$INSTALLER_VERSION\","
        echo "  \"installed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        echo "}"
    } > "$VMS_RELEASE_MARKER"
    chmod 0644 "$VMS_RELEASE_MARKER"
    log "Stamped installed VMS release: $VMS_RELEASE_COMMIT"
}
