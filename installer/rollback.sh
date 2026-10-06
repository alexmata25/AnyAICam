#!/usr/bin/env bash
# Restores the VMS to the previous release from a rollback point.
#
#   sudo anyaicam-rollback [MANIFEST] [--restore-database] [--allow-stale] [--yes]
#   (the same script is also ./rollback.sh in every installer folder)
#
# Rollback points are written by the installer (create_rollback_point() in
# 06-deploy-vms.sh, before a repair/upgrade) and by Software Update (the root
# applier, after a validated in-app update). MANIFEST defaults to the newest
# one: ROLLBACK_DIR/latest.env (/var/lib/anyaicam-update/rollback, root-owned),
# or the legacy /var/lib/anyaicam/rollback/latest.env when only that exists.
#
# By default this restores the previous image, code and release identity
# (build, version and the installed-release record) and KEEPS the current
# database (recordings, events and settings made since the upgrade stay).
# --restore-database also puts back the database copy taken at upgrade time;
# the current database (with its -wal/-shm) is first moved, root-only, to
# ROLLBACK_DIR/replaced-database-<time>-<random>/, never deleted.
#
# A rollback point belongs to the release that replaced it (UPGRADE_TO_COMMIT).
# If the appliance runs a different build now -- for example an in-app update
# happened after an installer rollback point -- restoring it would skip
# releases, so it is refused unless --allow-stale is given (2026-10-04).
# Re-applying a point while its rollback build is running is allowed.
#
# Recordings, configuration (/etc/anyaicam) and credentials are never
# touched. Stops and restarts anyaicam-vms.service, then checks /version.
set -euo pipefail

VMS_INSTALL_ROOT="${VMS_INSTALL_ROOT:-/opt/anyaicam}"
VMS_ENV_FILE="${VMS_ENV_FILE:-/etc/anyaicam/vms.env}"
VMS_RECORDINGS_DIR="${VMS_RECORDINGS_DIR:-/var/lib/anyaicam/vms/recordings}"
VMS_RELEASE_MARKER="${VMS_RELEASE_MARKER:-/etc/anyaicam/vms_release.json}"
UPDATE_STATE_DIR="${ANYAICAM_UPDATE_STATE_DIR:-/var/lib/anyaicam-update}"
ROLLBACK_DIR="${ANYAICAM_ROLLBACK_DIR:-$UPDATE_STATE_DIR/rollback}"
LEGACY_ROLLBACK_DIR="${ANYAICAM_LEGACY_ROLLBACK_DIR:-/var/lib/anyaicam/rollback}"
VMS_IMAGE="${ANYAICAM_VMS_IMAGE:-anyaicam-vms}"
VMS_SERVICE="${ANYAICAM_VMS_SERVICE:-anyaicam-vms.service}"
VMS_URL="${ANYAICAM_VMS_URL:-http://127.0.0.1:8000}"
VALIDATE_SECONDS="${ANYAICAM_ROLLBACK_VALIDATE_SECONDS:-180}"
VMS_DATABASE_NAME="partner_portal.db"
# Root edits vms.env and the release marker inside a folder the anyaicam user
# owns: through the applier's symlink-safe routines (agent_file_main), never
# sed -i / cp / redirection there.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILE_HELPER_DIR="${ANYAICAM_FILE_HELPER_DIR:-}"
if [[ -z "$FILE_HELPER_DIR" ]]; then
    for _candidate in /opt/anyaicam-agent/privileged "$_here/../appliance-agent/system"; do
        if [[ -f "$_candidate/apply_release.py" ]]; then FILE_HELPER_DIR="$_candidate"; break; fi
    done
fi
agent_file() {
    [[ -n "$FILE_HELPER_DIR" && -f "$FILE_HELPER_DIR/apply_release.py" ]] || { echo "[ERROR] file helper missing" >&2; return 1; }
    python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import apply_release; sys.exit(apply_release.agent_file_main())' "$FILE_HELPER_DIR"
}

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

manifest=""
restore_database=0
assume_yes=0
allow_stale=0
for arg in "$@"; do
    case "$arg" in
        --restore-database) restore_database=1 ;;
        --allow-stale) allow_stale=1 ;;
        --yes) assume_yes=1 ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        -*) die "Unknown option: $arg" ;;
        *) manifest="$arg" ;;
    esac
done

non_root_test=0
[[ "${ANYAICAM_ROLLBACK_ALLOW_NON_ROOT:-}" == "1" ]] && non_root_test=1
[[ "$non_root_test" == "1" || "$(id -u)" == "0" ]] || die "Run as root (sudo)."

if [[ -z "$manifest" ]]; then
    if [[ -f "$ROLLBACK_DIR/latest.env" ]]; then
        manifest="$ROLLBACK_DIR/latest.env"
    elif [[ -f "$LEGACY_ROLLBACK_DIR/latest.env" ]]; then
        manifest="$LEGACY_ROLLBACK_DIR/latest.env"
        log "Using the legacy rollback point in $LEGACY_ROLLBACK_DIR (no newer one in $ROLLBACK_DIR)."
    else
        manifest="$ROLLBACK_DIR/latest.env"
    fi
fi

# A file root will act on must be a regular file owned by root: the legacy
# rollback folder sits inside a directory the unprivileged agent user owns.
trusted_file() {
    local path="$1" what="$2"
    [[ -f "$path" && ! -L "$path" ]] || die "$what is missing or not a regular file: $path"
    if [[ "$non_root_test" != "1" && "$(stat -c %u "$path")" != "0" ]]; then
        die "$what is not owned by root; refusing to use it: $path"
    fi
}

[[ -f "$manifest" ]] || die "Rollback manifest not found: $manifest"
trusted_file "$manifest" "Rollback manifest"
# Parsed, never sourced: only these keys, only KEY=value lines.
manifest_value() { sed -n "s/^$1=//p" "$manifest" | tail -n 1; }
ROLLBACK_COMMIT="$(manifest_value ROLLBACK_COMMIT)"
ROLLBACK_IMAGE="$(manifest_value ROLLBACK_IMAGE)"
ROLLBACK_CODE_ARCHIVE="$(manifest_value ROLLBACK_CODE_ARCHIVE)"
ROLLBACK_DATABASE_BACKUP="$(manifest_value ROLLBACK_DATABASE_BACKUP)"
ROLLBACK_VERSION="$(manifest_value ROLLBACK_VERSION)"
ROLLBACK_MARKER="$(manifest_value ROLLBACK_MARKER)"
UPGRADE_TO_COMMIT="$(manifest_value UPGRADE_TO_COMMIT)"
[[ "$ROLLBACK_COMMIT" =~ ^[0-9a-f]{7,40}$ ]] || die "Manifest has no valid ROLLBACK_COMMIT."
[[ -z "$ROLLBACK_VERSION" || "$ROLLBACK_VERSION" =~ ^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$ ]] || die "Manifest has an invalid ROLLBACK_VERSION."
[[ "$ROLLBACK_IMAGE" != "none" && -n "$ROLLBACK_IMAGE" ]] || die "Manifest has no rollback image (nothing was running before that upgrade)."
docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 || die "Rollback image $ROLLBACK_IMAGE no longer exists."
trusted_file "$ROLLBACK_CODE_ARCHIVE" "Rollback code archive"
gzip -t "$ROLLBACK_CODE_ARCHIVE" || die "Rollback code archive is corrupt: $ROLLBACK_CODE_ARCHIVE"
if [[ "$restore_database" == "1" ]]; then
    [[ -n "$ROLLBACK_DATABASE_BACKUP" && "$ROLLBACK_DATABASE_BACKUP" != "none" ]] \
        || die "No database backup in this rollback point (--restore-database impossible)."
fi
if [[ -n "$ROLLBACK_MARKER" && "$ROLLBACK_MARKER" != "none" ]]; then
    trusted_file "$ROLLBACK_MARKER" "Saved release record"
fi

# ---------------------------------------------------------------- checked before anything changes (2026-10-06)
# Every file this rollback will write is checked through the same hardened
# helper that later writes it -- before vms.env is even read: an unsafe
# vms.env (symlink, hard link, group/other-writable, not a file) or an
# unwritable marker stops here, with the VMS untouched and still running.
printf 'op check\npath %s\nrequire_existing\n' "$VMS_ENV_FILE" | agent_file \
    || die "$VMS_ENV_FILE is not safe to update; nothing was changed."
printf 'op check\npath %s\n' "$VMS_RELEASE_MARKER" | agent_file \
    || die "$VMS_RELEASE_MARKER cannot be written safely; nothing was changed."

# The database backup (and its -wal/-shm) sits in the recordings area the
# service user owns, so it could be swapped for a symlink or another file
# before the restore. With --restore-database each is checked (never a
# symlink, hard link or non-file; owned by root or anyaicam; not group/
# other-writable) and copied now into a folder only root can write; the
# restore later reads only those copies.
db_stage=""
if [[ "$restore_database" == "1" ]]; then
    db_stage="$(mktemp -d)"
    chmod 0700 "$db_stage"
    trap 'rm -rf "$db_stage"' EXIT
    for suffix in "" "-wal" "-shm"; do
        stage_status=0
        printf 'op stage\nsource %s\npath %s\n' "$ROLLBACK_DATABASE_BACKUP$suffix" "$db_stage/db$suffix" | agent_file \
            || stage_status=$?
        if [[ "$stage_status" == "3" && -n "$suffix" ]]; then
            continue  # no -wal/-shm with this backup
        elif [[ "$stage_status" == "3" ]]; then
            die "No database backup in this rollback point (--restore-database impossible)."
        elif [[ "$stage_status" != "0" ]]; then
            die "The database backup $ROLLBACK_DATABASE_BACKUP$suffix is not safe to restore from; nothing was changed."
        fi
    done
fi

running="$(sed -n 's/^ANYAICAM_BUILD_ID=//p' "$VMS_ENV_FILE" 2>/dev/null | tail -n 1)"
# Running the rollback build already (re-applying the same point, e.g. to add
# --restore-database afterwards) is allowed; any other build is not.
if [[ -n "$UPGRADE_TO_COMMIT" && -n "$running" && "$running" != "$UPGRADE_TO_COMMIT" && "$running" != "$ROLLBACK_COMMIT" ]]; then
    if [[ "$allow_stale" != "1" ]]; then
        die "This rollback point was taken when ${UPGRADE_TO_COMMIT:0:12} was installed, but this appliance now runs ${running:0:12}. Restoring it would skip releases. Use the rollback point for the running release, or pass --allow-stale if you are sure."
    fi
    log "WARNING: --allow-stale: restoring a rollback point taken for ${UPGRADE_TO_COMMIT:0:12} while ${running:0:12} is running."
fi

log "Rolling back the VMS to ${ROLLBACK_VERSION:-an unversioned release} (build $ROLLBACK_COMMIT; image $ROLLBACK_IMAGE, code $ROLLBACK_CODE_ARCHIVE, database: $([[ $restore_database == 1 ]] && echo "restore $ROLLBACK_DATABASE_BACKUP" || echo 'keep current'))."
if [[ "$assume_yes" != "1" ]]; then
    read -r -p "Proceed? This stops and restarts the VMS. [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || die "Cancelled; nothing was changed."
fi

staging="$(mktemp -d)"
record="$(mktemp)"
snapshot="$(mktemp -d)"
chmod 0700 "$staging" "$snapshot"
mutating=0
finished=0
cleanup() { rm -rf "$staging" "$record" "$snapshot" ${db_stage:+"$db_stage"}; }
trap cleanup EXIT

# Same exclusions as deploy_vms(): persistent state and secrets stay put.
RSYNC_EXCLUDES=(--exclude 'recordings/' --exclude 'data/config/' --exclude '.env' --exclude 'mediamtx/'
    --exclude 'app/static/hls/' --exclude 'app/recordings/' --exclude 'app/auto.key' --exclude 'app/auto.crt')

# The installed-release record: the agent reports it to the cloud, and root's
# copy is what Software Update trusts for its downgrade check -- after a
# rollback the newer release must be installable again.
if [[ -n "$ROLLBACK_MARKER" && "$ROLLBACK_MARKER" != "none" ]]; then
    cp "$ROLLBACK_MARKER" "$record"
else
    printf '{\n  "vms_release_commit": "%s",\n  "release_version": "%s",\n  "installer_version": "%s"\n}\n' \
        "$ROLLBACK_COMMIT" "$ROLLBACK_VERSION" "$ROLLBACK_VERSION" > "$record"
fi
python3 - "$record" "$ROLLBACK_COMMIT" "$ROLLBACK_VERSION" <<'PY'
import json, sys
from datetime import datetime, timezone
path, commit, version = sys.argv[1:4]
data = json.load(open(path, encoding="utf-8"))
if not isinstance(data, dict) or data.get("vms_release_commit") != commit:
    sys.exit("The saved release record does not belong to the rollback build.")
data["release_version"] = version
data["installer_version"] = data.get("installer_version") or version
data["restored_by"] = "rollback"
data["restored_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
PY
tar -xzf "$ROLLBACK_CODE_ARCHIVE" -C "$staging"
root_name="$(basename "$VMS_INSTALL_ROOT")"
[[ -d "$staging/$root_name/app" ]] || die "Rollback code archive does not contain $root_name/app."

# The state this rollback replaces, so a failure part-way can put it back:
# the application tree, the image the VMS runs, vms.env, the release marker
# and root's installed-release record -- and whether the VMS was running.
was_active=0
systemctl is-active --quiet "$VMS_SERVICE" && was_active=1
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$VMS_INSTALL_ROOT/" "$snapshot/tree/"
previous_image=""
if docker image inspect "$VMS_IMAGE:latest" >/dev/null 2>&1; then
    previous_image="$VMS_IMAGE:before-rollback"
    docker tag "$VMS_IMAGE:latest" "$previous_image" || die "Could not keep the current image; nothing was changed."
fi
printf 'op read\npath %s\n' "$VMS_ENV_FILE" | agent_file > "$snapshot/vms.env" \
    || die "Could not read $VMS_ENV_FILE safely; nothing was changed."
marker_saved=0
if printf 'op read\npath %s\n' "$VMS_RELEASE_MARKER" | agent_file > "$snapshot/marker"; then
    marker_saved=1
elif [[ "$?" != "3" ]]; then
    die "Could not read $VMS_RELEASE_MARKER safely; nothing was changed."
fi
installed_record="$UPDATE_STATE_DIR/installed_release.json"
installed_saved=0
if [[ -f "$installed_record" && ! -L "$installed_record" ]]; then
    cp -p "$installed_record" "$snapshot/installed_release.json"
    installed_saved=1
fi
db_swapped=0
db_broken=0
keep_dir=""

# Puts back everything recorded above after a failure part-way through, and
# restarts the VMS if it was running. Best effort, step by step: it reports
# what it could not restore rather than stopping at the first problem.
restore_previous() {
    set +e
    local problems=()
    echo "[ERROR] The rollback failed after it began changing the release; putting back the previous state..." >&2
    systemctl stop "$VMS_SERVICE" >/dev/null 2>&1
    rsync -a --checksum --delete "${RSYNC_EXCLUDES[@]}" "$snapshot/tree/" "$VMS_INSTALL_ROOT/" || problems+=("application files")
    if [[ -n "$previous_image" ]]; then
        docker tag "$previous_image" "$VMS_IMAGE:latest" || problems+=("image $VMS_IMAGE:latest")
    fi
    { printf 'op write\npath %s\nmode 0640\nowner anyaicam\ncontent\n' "$VMS_ENV_FILE"; cat "$snapshot/vms.env"; } | agent_file \
        || problems+=("$VMS_ENV_FILE")
    if [[ "$marker_saved" == "1" ]]; then
        { printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n' "$VMS_RELEASE_MARKER"; cat "$snapshot/marker"; } | agent_file \
            || problems+=("$VMS_RELEASE_MARKER")
    fi
    if [[ -d "$UPDATE_STATE_DIR" ]]; then
        if [[ "$installed_saved" == "1" ]]; then
            cp -p "$snapshot/installed_release.json" "$installed_record" || problems+=("$installed_record")
        else
            rm -f "$installed_record"
        fi
    fi
    local database_ok=1
    if [[ "$db_broken" == "1" ]]; then
        database_ok=0
        problems+=("database $current (see the error above; the originals that were moved are in $keep_dir)")
    elif [[ "$db_swapped" == "1" ]]; then
        if { printf 'op undo_db_restore\npath %s\n' "$current"; cat "$snapshot/db-swap"; } | agent_file >&2; then
            rmdir "$keep_dir" 2>/dev/null
        else
            database_ok=0
            problems+=("database $current (the originals that were moved are in $keep_dir)")
        fi
    fi
    if [[ "$was_active" == "1" && "$database_ok" != "1" ]]; then
        problems+=("$VMS_SERVICE was left stopped: its database is not consistent")
    elif [[ "$was_active" == "1" ]]; then
        systemctl start "$VMS_SERVICE" || problems+=("starting $VMS_SERVICE")
    fi
    if (( ${#problems[@]} )); then
        echo "[ERROR] Could not restore: ${problems[*]}. Check: systemctl status $VMS_SERVICE; the previous image is tagged ${previous_image:-(none)}." >&2
    else
        echo "[ERROR] The previous release was put back$([[ $was_active == 1 ]] && echo ' and the VMS restarted'); nothing was rolled back." >&2
    fi
}
on_exit() {
    local status=$?
    if [[ "$mutating" == "1" && "$finished" != "1" ]]; then
        restore_previous
        status=1
    fi
    cleanup
    exit "$status"
}
trap on_exit EXIT

# ---------------------------------------------------------------- the change
mutating=1
systemctl stop "$VMS_SERVICE"
# --checksum: the restored files can have the same size and a timestamp
# within the same second as the current ones, which rsync's default
# size+mtime check would silently skip.
rsync -a --checksum --delete "${RSYNC_EXCLUDES[@]}" "$staging/$root_name/" "$VMS_INSTALL_ROOT/"
docker tag "$ROLLBACK_IMAGE" "$VMS_IMAGE:latest"

env_edit=("op update_env" "path $VMS_ENV_FILE" "mode 0640" "owner anyaicam"
          "override ANYAICAM_VMS_COMMIT=$ROLLBACK_COMMIT" "override ANYAICAM_BUILD_ID=$ROLLBACK_COMMIT")
if [[ -n "$ROLLBACK_VERSION" ]]; then
    env_edit+=("override ANYAICAM_VERSION=$ROLLBACK_VERSION")
else
    # The previous release had no product version: let its code report its own.
    env_edit+=("remove ANYAICAM_VERSION")
fi
printf '%s\n' "${env_edit[@]}" | agent_file || die "Could not update $VMS_ENV_FILE safely."

{ printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n' "$VMS_RELEASE_MARKER"; cat "$record"; } | agent_file \
    || die "Could not write $VMS_RELEASE_MARKER safely."
if [[ -d "$UPDATE_STATE_DIR" ]]; then
    if [[ -n "$ROLLBACK_VERSION" ]]; then
        if [[ "$non_root_test" == "1" ]]; then
            cp "$record" "$installed_record"
        else
            install -m 0644 -o root -g root "$record" "$installed_record"
        fi
    else
        rm -f "$installed_record"
    fi
fi
if [[ "$restore_database" == "1" ]]; then
    current="$VMS_RECORDINGS_DIR/$VMS_DATABASE_NAME"
    # The service user owns the recordings folder and can plant or swap names
    # in it at any moment, so neither mv nor cp touches it, and the replaced
    # database is not kept there: the helper moves the current database,
    # -wal and -shm by one rename each into keep_dir -- a new folder only
    # root can enter, beside the rollback points -- and writes the checked
    # copies (taken before the VMS was stopped) through one no-follow handle
    # on the recordings folder, each as a new temporary file renamed into
    # place. It is one transaction: on failure it puts back what it changed
    # itself (exit 4: it could not); on success it prints which files it
    # moved and placed, so a later failure undoes exactly those.
    keep_dir="$(mktemp -d "$ROLLBACK_DIR/replaced-database-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")" \
        || die "Could not create a folder for the replaced database; the current database was left in place."
    chmod 0700 "$keep_dir"
    db_status=0
    printf 'op restore_db\npath %s\nsource %s\nkeep %s\n' "$current" "$db_stage" "$keep_dir" | agent_file > "$snapshot/db-swap" \
        || db_status=$?
    if [[ "$db_status" == "4" ]]; then
        db_broken=1
        die "The database restore failed and the previous database could not be put back."
    elif [[ "$db_status" != "0" ]]; then
        rmdir "$keep_dir" 2>/dev/null || true
        die "Could not restore the database safely; the current database was left in place."
    fi
    db_swapped=1
    log "Database restored from $ROLLBACK_DATABASE_BACKUP; the database it replaced is kept (root only) in $keep_dir."
fi
systemctl start "$VMS_SERVICE"
# The release is now consistently the rollback one; a VMS that then fails
# validation is reported (exit 3), not reverted.
finished=1

if [[ "$VALIDATE_SECONDS" == "0" ]]; then
    log "Rollback to $ROLLBACK_COMMIT applied (validation skipped). Check: curl -fsS $VMS_URL/version and /health."
    exit 0
fi
log "Waiting up to ${VALIDATE_SECONDS}s for the VMS to report build ${ROLLBACK_COMMIT:0:12}..."
deadline=$(( $(date +%s) + VALIDATE_SECONDS ))
while :; do
    if python3 - "$VMS_URL" "$ROLLBACK_COMMIT" "$ROLLBACK_VERSION" <<'PY'
import json, sys, urllib.request
url, build, version = sys.argv[1:4]
try:
    with urllib.request.urlopen(url + "/version", timeout=5) as response:
        reported = json.loads(response.read())
    with urllib.request.urlopen(url + "/health", timeout=5) as response:
        healthy = response.status == 200
except Exception:
    sys.exit(1)
ok = reported.get("build_id") == build and (not version or reported.get("version") == version) and healthy
sys.exit(0 if ok else 1)
PY
    then
        log "Rollback complete: the VMS reports ${ROLLBACK_VERSION:-its own version} build ${ROLLBACK_COMMIT:0:12} and /health passes."
        exit 0
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[ERROR] The VMS did not report build ${ROLLBACK_COMMIT:0:12} and pass /health within ${VALIDATE_SECONDS}s. The files are restored; check: systemctl status $VMS_SERVICE; curl $VMS_URL/version" >&2
        exit 3
    fi
    sleep 3
done
