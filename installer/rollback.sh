#!/usr/bin/env bash
# Restores the VMS to a rollback point created by install.sh (see
# create_rollback_point() in 06-deploy-vms.sh).
#
#   sudo ./rollback.sh [MANIFEST] [--restore-database] [--yes]
#
# MANIFEST defaults to /var/lib/anyaicam/rollback/latest.env (the point
# taken before the most recent repair/upgrade). By default this restores
# the previous image, code and recorded release commit and KEEPS the
# current database (recordings, events and settings made since the
# upgrade stay). --restore-database also puts back the database copy taken
# at upgrade time; the current database is first saved beside it
# (partner_portal-before-rollback-<time>.db), never deleted.
# Recordings, configuration (/etc/anyaicam) and credentials are never
# touched. Stops and restarts anyaicam-vms.service.
set -euo pipefail

VMS_INSTALL_ROOT="${VMS_INSTALL_ROOT:-/opt/anyaicam}"
VMS_ENV_FILE="${VMS_ENV_FILE:-/etc/anyaicam/vms.env}"
VMS_RECORDINGS_DIR="${VMS_RECORDINGS_DIR:-/var/lib/anyaicam/vms/recordings}"
ROLLBACK_DIR="${ANYAICAM_ROLLBACK_DIR:-/var/lib/anyaicam/rollback}"
VMS_IMAGE="${ANYAICAM_VMS_IMAGE:-anyaicam-vms}"
VMS_SERVICE="${ANYAICAM_VMS_SERVICE:-anyaicam-vms.service}"
VMS_DATABASE_NAME="partner_portal.db"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

manifest="$ROLLBACK_DIR/latest.env"
restore_database=0
assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --restore-database) restore_database=1 ;;
        --yes) assume_yes=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        -*) die "Unknown option: $arg" ;;
        *) manifest="$arg" ;;
    esac
done

[[ "${ANYAICAM_ROLLBACK_ALLOW_NON_ROOT:-}" == "1" || "$(id -u)" == "0" ]] || die "Run as root (sudo)."
[[ -f "$manifest" ]] || die "Rollback manifest not found: $manifest"

# Parsed, never sourced: only these keys, only KEY=value lines.
manifest_value() { sed -n "s/^$1=//p" "$manifest" | tail -n 1; }
ROLLBACK_COMMIT="$(manifest_value ROLLBACK_COMMIT)"
ROLLBACK_IMAGE="$(manifest_value ROLLBACK_IMAGE)"
ROLLBACK_CODE_ARCHIVE="$(manifest_value ROLLBACK_CODE_ARCHIVE)"
ROLLBACK_DATABASE_BACKUP="$(manifest_value ROLLBACK_DATABASE_BACKUP)"

[[ "$ROLLBACK_COMMIT" =~ ^[0-9a-f]{7,40}$ ]] || die "Manifest has no valid ROLLBACK_COMMIT."
[[ "$ROLLBACK_IMAGE" != "none" && -n "$ROLLBACK_IMAGE" ]] || die "Manifest has no rollback image (nothing was running before that upgrade)."
docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 || die "Rollback image $ROLLBACK_IMAGE no longer exists."
[[ -f "$ROLLBACK_CODE_ARCHIVE" ]] || die "Rollback code archive missing: $ROLLBACK_CODE_ARCHIVE"
gzip -t "$ROLLBACK_CODE_ARCHIVE" || die "Rollback code archive is corrupt: $ROLLBACK_CODE_ARCHIVE"
if [[ "$restore_database" == "1" ]]; then
    [[ -f "$ROLLBACK_DATABASE_BACKUP" ]] || die "No database backup in this rollback point (--restore-database impossible)."
fi

log "Rolling back the VMS to $ROLLBACK_COMMIT (image $ROLLBACK_IMAGE, code $ROLLBACK_CODE_ARCHIVE, database: $([[ $restore_database == 1 ]] && echo "restore $ROLLBACK_DATABASE_BACKUP" || echo 'keep current'))."
if [[ "$assume_yes" != "1" ]]; then
    read -r -p "Proceed? This stops and restarts the VMS. [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || die "Cancelled; nothing was changed."
fi

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
tar -xzf "$ROLLBACK_CODE_ARCHIVE" -C "$staging"
root_name="$(basename "$VMS_INSTALL_ROOT")"
[[ -d "$staging/$root_name/app" ]] || die "Rollback code archive does not contain $root_name/app."

systemctl stop "$VMS_SERVICE"

# Same exclusions as deploy_vms(): persistent state and secrets stay put.
# --checksum: the restored files can have the same size and a timestamp
# within the same second as the current ones, which rsync's default
# size+mtime check would silently skip.
rsync -a --checksum --delete \
    --exclude 'recordings/' --exclude 'data/config/' --exclude '.env' --exclude 'mediamtx/' \
    --exclude 'app/static/hls/' --exclude 'app/recordings/' --exclude 'app/auto.key' --exclude 'app/auto.crt' \
    "$staging/$root_name/" "$VMS_INSTALL_ROOT/"
docker tag "$ROLLBACK_IMAGE" "$VMS_IMAGE:latest"

for key in ANYAICAM_VMS_COMMIT ANYAICAM_BUILD_ID; do
    if grep -q "^$key=" "$VMS_ENV_FILE"; then
        sed -i "s/^$key=.*/$key=$ROLLBACK_COMMIT/" "$VMS_ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$ROLLBACK_COMMIT" >> "$VMS_ENV_FILE"
    fi
done

if [[ "$restore_database" == "1" ]]; then
    current="$VMS_RECORDINGS_DIR/$VMS_DATABASE_NAME"
    kept="$VMS_RECORDINGS_DIR/partner_portal-before-rollback-$(date -u +%Y%m%dT%H%M%SZ).db"
    for suffix in "" "-wal" "-shm"; do
        if [[ -f "$current$suffix" ]]; then mv "$current$suffix" "$kept$suffix"; fi
    done
    cp -p "$ROLLBACK_DATABASE_BACKUP" "$current"
    if [[ -f "$ROLLBACK_DATABASE_BACKUP-wal" ]]; then cp -p "$ROLLBACK_DATABASE_BACKUP-wal" "$current-wal"; fi
    log "Database restored from $ROLLBACK_DATABASE_BACKUP; the database it replaced is kept as $kept."
fi

systemctl start "$VMS_SERVICE"
log "Rollback to $ROLLBACK_COMMIT complete. Check: curl -fsS http://127.0.0.1:8000/version (build_id $ROLLBACK_COMMIT) and /health."
