#!/usr/bin/env bash
# Stable appliance identity + mutable installed-release marker.
#
# These files live in $CONFIG_DIR, which the anyaicam user owns, so root
# writes them only through the symlink-safe helper (06-deploy-vms.sh
# agent_file): never through a planted symlink, owner and mode set on the
# new file itself, atomic rename.

identity_provision() {
    local state="$1"
    if [[ -L "$IDENTITY_FILE" ]]; then
        echo "[ERROR] $IDENTITY_FILE is a symbolic link; refusing to use or replace it." >&2
        return 1
    fi
    if [[ -f "$IDENTITY_FILE" ]]; then
        log "Existing appliance identity found -- preserved, not regenerated (sha256=$(sha256sum "$IDENTITY_FILE" | cut -d' ' -f1))."
    else
        if [[ "$state" != "clean" ]]; then
            log "WARNING: no identity file found on a non-clean install; generating one as part of repair."
        fi
        local appliance_id
        appliance_id="$(cat /proc/sys/kernel/random/uuid)"
        {
            printf 'op write\npath %s\nmode 0600\nowner anyaicam\nif_missing\ncontent\n' "$IDENTITY_FILE"
            echo "{"
            echo "  \"appliance_id\": \"$appliance_id\","
            echo "  \"installer_version\": \"$INSTALLER_VERSION\","
            echo "  \"installed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
            echo "}"
        } | agent_file
        log "Generated new appliance identity: $appliance_id"
    fi

    # Installer version is mutable metadata; identity remains stable.
    printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n%s\n' "$VERSION_MARKER" "$INSTALLER_VERSION" | agent_file
}

stamp_release() {
    local record
    record="$(
        echo "{"
        echo "  \"vms_release_commit\": \"$VMS_RELEASE_COMMIT\","
        echo "  \"release_archive_sha256\": \"${VMS_RELEASE_SHA256:-}\","
        echo "  \"installer_source_commit\": \"$INSTALLER_SOURCE_COMMIT\","
        echo "  \"mediamtx_included\": \"${MEDIAMTX_INCLUDED:-unknown}\","
        echo "  \"mediamtx_sha256\": \"${MEDIAMTX_SHA256:-}\","
        echo "  \"installer_version\": \"$INSTALLER_VERSION\","
        echo "  \"release_version\": \"${RELEASE_VERSION:-}\","
        echo "  \"installed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        echo "}"
    )"
    printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n%s\n' "$VMS_RELEASE_MARKER" "$record" | agent_file
    # Software Update keeps its own root-owned copy for the downgrade check
    # (the marker above sits in a directory the agent user owns). It is
    # written from the same content, never copied back out of that directory.
    if [[ -n "${RELEASE_VERSION:-}" && -d "${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}" ]]; then
        printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n%s\n' \
            "${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}/installed_release.json" "$record" | agent_file
    fi
    log "Stamped installed VMS release: $VMS_RELEASE_COMMIT"
}
