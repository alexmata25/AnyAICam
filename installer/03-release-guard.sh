#!/usr/bin/env bash
# Manual-install downgrade protection (Linux 1.2.5, 2026-10-08).
#
# Before 1.2.5, `sudo ./install.sh` from ANY installer folder replaced the
# running release without comparing versions: re-running an older installer
# (e.g. a forgotten ~/AnyAiCam-Update-1.2.4) silently put old code back over
# newer code. Software Update already refused that (updater/state_machine.py
# "downgrade or replay rejected"); this is the same rule for the manual
# installer, checked before anything on the machine is changed.
#
#   newer release            -> installs
#   same release, same build -> installs (reinstall / --repair)
#   older release            -> refused
#   same release, other build-> refused (cannot tell which is newer)
#   unreadable installed one -> refused (never guesses)
#
# The controlled rollback path is the rollback command every install sets up
# (`sudo anyaicam-rollback`, installer/rollback.sh): it restores the previous
# release's image, code and installed-release record from the rollback point
# taken before the upgrade. `--allow-downgrade` exists for support when no
# rollback point is usable; the deploy step still takes a rollback point of
# the release it replaces, so that downgrade can itself be undone.
#
# The installed release is read from root's own record first
# ($UPDATE_STATE_DIR/installed_release.json, owned by root and trusted by
# Software Update); $VMS_RELEASE_MARKER sits in a directory the agent user
# owns, so it is used only when root's record does not exist yet.

# Prints "<release_version> <vms_release_commit>" of the installed release
# ("- -" when there is none). Exit 1 when a record exists but cannot be read.
installed_release_identity() {
    local record="" source=""
    # Resolved here, not when sourced: UPDATE_STATE_DIR may be set later.
    local root_record="${RELEASE_GUARD_ROOT_RECORD:-${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}/installed_release.json}"
    local root_uid="${RELEASE_GUARD_ROOT_UID:-0}"   # tests run unprivileged
    if [[ -e "$root_record" || -L "$root_record" ]]; then
        if [[ -L "$root_record" || ! -f "$root_record" || "$(stat -c %u "$root_record")" != "$root_uid" ]]; then
            echo "[ERROR] $root_record is not a regular root-owned file." >&2
            return 1
        fi
        record="$root_record"; source="root"
    elif [[ -e "$VMS_RELEASE_MARKER" || -L "$VMS_RELEASE_MARKER" ]]; then
        if [[ -L "$VMS_RELEASE_MARKER" || ! -f "$VMS_RELEASE_MARKER" ]]; then
            echo "[ERROR] $VMS_RELEASE_MARKER is not a regular file." >&2
            return 1
        fi
        record="$VMS_RELEASE_MARKER"; source="marker"
    else
        echo "- -"
        return 0
    fi
    python3 - "$record" <<'PY' || { echo "[ERROR] Could not read the installed release from $record ($source)." >&2; return 1; }
import json, re, sys
with open(sys.argv[1], "rb") as handle:
    data = json.loads(handle.read(65536))
if not isinstance(data, dict):
    sys.exit(1)
version = str(data.get("release_version") or "")
commit = str(data.get("vms_release_commit") or "")
if version and not re.fullmatch(r"\d{1,4}(\.\d{1,4}){1,3}", version):
    sys.exit(1)
if commit and not re.fullmatch(r"[0-9a-f]{40}", commit):
    sys.exit(1)
print(f"{version or '-'} {commit or '-'}")
PY
}

# Prints newer / same / older: how $1 compares with $2 (dotted versions).
compare_release_versions() {
    python3 - "$1" "$2" <<'PY'
import sys
a, b = ([int(part) for part in value.split(".")] for value in sys.argv[1:3])
width = max(len(a), len(b))
a, b = a + [0] * (width - len(a)), b + [0] * (width - len(b))
print("newer" if a > b else "same" if a == b else "older")
PY
}

# $1 = 1 when --allow-downgrade was given. Returns non-zero to refuse.
release_downgrade_guard() {
    local allow="${1:-0}" identity installed_version installed_commit relation
    identity="$(installed_release_identity)" || {
        if [[ "$allow" == "1" ]]; then
            log "WARNING: the installed release could not be read; continuing because --allow-downgrade was given."
            return 0
        fi
        echo "[ERROR] The installed release could not be verified, so this installer cannot tell whether it would downgrade it. Nothing was changed." >&2
        return 1
    }
    read -r installed_version installed_commit <<<"$identity"

    if [[ "$installed_version" == "-" ]]; then
        # No installed release (clean machine) or one from before release
        # versions existed: nothing to protect by version.
        log "Release check: no recorded release version on this machine; installing ${RELEASE_VERSION:-unversioned}."
        return 0
    fi
    if [[ -z "${RELEASE_VERSION:-}" ]]; then
        relation="unknown"
    else
        relation="$(compare_release_versions "$RELEASE_VERSION" "$installed_version")" || relation="unknown"
    fi
    if [[ "$relation" == "same" && "$installed_commit" == "$VMS_RELEASE_COMMIT" ]]; then
        log "Release check: reinstalling the installed release $installed_version ($installed_commit)."
        return 0
    fi
    if [[ "$relation" == "newer" ]]; then
        log "Release check: upgrading $installed_version ($installed_commit) -> $RELEASE_VERSION ($VMS_RELEASE_COMMIT)."
        return 0
    fi

    local what
    case "$relation" in
        older) what="an OLDER release ($RELEASE_VERSION) than the one installed ($installed_version)" ;;
        same) what="a different build of $installed_version ($VMS_RELEASE_COMMIT, installed: $installed_commit)" ;;
        *) what="a release whose version cannot be compared with the installed $installed_version" ;;
    esac
    if [[ "$allow" == "1" ]]; then
        log "WARNING: --allow-downgrade: installing $what. A rollback point of the installed release is taken first."
        return 0
    fi
    {
        echo "[ERROR] This installer carries $what. Nothing was changed."
        echo "        To return to the previous release, use the controlled rollback: sudo anyaicam-rollback"
        echo "        (it restores the release that was running before the last upgrade)."
        echo "        Support only, when no rollback point is usable: sudo ./install.sh --allow-downgrade"
    } >&2
    return 1
}
