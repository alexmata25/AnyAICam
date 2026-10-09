#!/usr/bin/env bash
# Signed camera capacity: one-time migration (Linux 1.2.5, 2026-10-09).
# Sourced by install.sh.
#
# From 1.2.5 the VMS enforces only a camera capacity the cloud signed
# (app/vms_capacity.py); the unsigned vms_capacity.json, which the anyaicam
# user can edit, is no longer trusted. An appliance upgraded from an earlier
# release may have nothing signed yet -- and may be offline -- so this step
# carries the capacity it was already running with into a root-owned file the
# VMS reads through its read-only /etc/anyaicam-update mount:
#
#   /etc/anyaicam-update/vms_capacity_fallback.json   root:root 0644
#
# The VMS uses it only until the first signed capacity, and only while the
# appliance identity still matches the one it was migrated for.
#
#   * Only when the release being replaced predates signed capacity (or has
#     no recorded release), read as release_downgrade_guard reads it: root's
#     installed_release.json first. A later upgrade never migrates, so a value
#     forged into vms_capacity.json after 1.2.5 cannot be laundered in as root.
#   * Never overwritten; never on a clean install; the unsigned file is left
#     in place (a rollback to 1.2.4 keeps using it).
#   * The write is the Software Update applier's migrate_capacity_fallback()
#     (appliance-agent/system/apply_release.py, shipped in this package): the
#     same code for both paths.
#
# Runs after deploy_vms (the old VMS is stopped and legacy data moved to
# /var/lib/anyaicam/vms) and before systemd_setup starts the new VMS. So it
# NEVER fails the install (Orange, 2026-10-09: aborting here left the VMS
# stopped): an unsafe existing fallback or any migration problem is a
# warning, and the VMS -- which checks the fallback itself -- fails closed
# until the cloud's first signed capacity.

migrate_capacity_fallback_step() {
    local state="$1" identity from_version outcome
    if [[ "$state" == "clean" ]]; then
        log "Camera capacity: clean install, nothing to migrate."
        return 0
    fi
    identity="$(installed_release_identity)" || {
        # release_downgrade_guard already refused an unreadable record unless
        # --allow-downgrade was given; never migrate on a guess.
        log "[WARN] Camera capacity: the installed release could not be read; nothing migrated."
        return 0
    }
    from_version="${identity%% *}"
    [[ "$from_version" == "-" ]] && from_version=""
    outcome="$(python3 - "$PAYLOAD_DIR/agent/system" "$from_version" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import apply_release
sys.exit(apply_release.migrate_capacity_fallback_main(sys.argv[2:]))
PYEOF
)" || outcome="error"
    case "$outcome" in
        migrated) log "Camera capacity: carried the running capacity into the root-owned fallback (used until the cloud's first signed capacity)." ;;
        exists) log "Camera capacity: the root-owned fallback already exists; left unchanged." ;;
        not_applicable) log "Camera capacity: the replaced release already used signed capacity; nothing to migrate." ;;
        no_capacity) log "Camera capacity: no capacity recorded yet; cameras start after the cloud's first signed capacity." ;;
        no_identity) log "Camera capacity: this appliance is not activated; cameras start after it is linked and the cloud signs its capacity." ;;
        unsafe) log "[WARN] Camera capacity: an existing fallback is unsafe and was left alone; the VMS will ignore it. Cameras start after the cloud's first signed capacity." ;;
        *) log "[WARN] Camera capacity: the migration did not complete ('$outcome'); the install continues and cameras start after the cloud's first signed capacity." ;;
    esac
    return 0
}
