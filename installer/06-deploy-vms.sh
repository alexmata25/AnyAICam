#!/usr/bin/env bash
# Deploy exactly the VMS payload embedded in the built installer artifact.
# No application files are read from the surrounding Git checkout.

migrate_legacy_persistent_data() {
    local old="$1" new="$2" label="$3"
    [[ -d "$old" ]] || return 0
    log "Found legacy $label under $old -- migrating to $new ..."
    mkdir -p "$new"
    chmod 0750 "$new"
    chown anyaicam:anyaicam "$new" 2>/dev/null || true
    rsync -a --ignore-existing "$old/" "$new/"
    local unresolved=0 rel
    while IFS= read -r -d '' f; do
        rel="${f#"$old"/}"
        if [[ -f "$new/$rel" ]] && cmp -s "$f" "$new/$rel"; then
            rm -f "$f"
        else
            log "WARNING: could not verify migration of $label file '$rel' -- leaving it at $old for manual review."
            unresolved=1
        fi
    done < <(find "$old" -type f -print0)
    find "$old" -type d -empty -delete 2>/dev/null || true
    if [[ "$unresolved" -eq 0 ]]; then
        rmdir "$old" 2>/dev/null || true
    fi
}

migrate_legacy_persistent_file() {
    local old="$1" new="$2" label="$3"
    [[ -f "$old" ]] || return 0
    if [[ ! -f "$new" ]]; then
        log "Found legacy $label at $old -- migrating to $new ..."
        mkdir -p "$(dirname "$new")"
        cp -p "$old" "$new"
        chmod 0640 "$new"
        chown anyaicam:anyaicam "$new" 2>/dev/null || true
        rm -f "$old"
    elif cmp -s "$old" "$new"; then
        rm -f "$old"
    else
        log "WARNING: legacy $label differs from existing $new -- preserving both."
    fi
}

upsert_env_key() {
    local file="$1" key="$2" value="$3" tmp
    tmp="$(mktemp)"
    if [[ -f "$file" ]]; then
        awk -F= -v key="$key" '$1 != key { print }' "$file" > "$tmp"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

ensure_vms_env() {
    mkdir -p "$CONFIG_DIR"
    chmod 0750 "$CONFIG_DIR"
    if [[ ! -f "$VMS_ENV_FILE" ]]; then
        if [[ -f "$PAYLOAD_DIR/config/vms.env.template" ]]; then
            log "Creating VMS environment config from the release template (existing configs are never overwritten)."
            cp "$PAYLOAD_DIR/config/vms.env.template" "$VMS_ENV_FILE"
        else
            log "Creating minimal VMS environment config (existing configs are never overwritten)."
            : > "$VMS_ENV_FILE"
        fi
    fi

    grep -q '^ANYAICAM_RUNTIME_ROLE=' "$VMS_ENV_FILE" 2>/dev/null || \
        printf '%s\n' 'ANYAICAM_RUNTIME_ROLE=edge' >> "$VMS_ENV_FILE"
    # Canonical name is ANYAICAM_ENV -- the only variable app/main.py and
    # app/cloud_config.py actually read (DEPLOYMENT_ENV = os.environ.get
    # ("ANYAICAM_ENV", "local")). This installer previously wrote
    # ANYAICAM_ENVIRONMENT here, a different name the app has never read
    # -- every appliance installed that way silently stayed on the
    # "local" default forever, regardless of this line ever running.
    grep -q '^ANYAICAM_ENV=' "$VMS_ENV_FILE" 2>/dev/null || \
        printf '%s\n' 'ANYAICAM_ENV=production' >> "$VMS_ENV_FILE"

    # Live View staging transport (2026-09-13): the existing S3/CloudFront
    # live-relay worker (app/live_relay_uploader.py) already self-gates on
    # this flag and defaults OFF in the app if the key is absent entirely
    # -- this line only makes that default explicit and present in every
    # installed appliance's own env file, the same way ANYAICAM_RUNTIME_
    # ROLE/ANYAICAM_ENV above do, so a future repair-install never has to
    # guess whether an existing appliance already has an opinion here.
    # Never overwrites an existing value -- flipping this to true for a
    # specific pilot appliance (alongside setting AWS_REGION and the
    # cloud-side live_relay_pilot DB flag) is a separate, explicit,
    # per-appliance decision, not something this installer makes for
    # every appliance by default.
    grep -q '^ANYAICAM_LIVE_RELAY_ENABLED=' "$VMS_ENV_FILE" 2>/dev/null || \
        printf '%s\n' 'ANYAICAM_LIVE_RELAY_ENABLED=false' >> "$VMS_ENV_FILE"

    # On-device analytics workers whose code default is off (2026-09-25).
    # Advanced Analytics grants People Counting and LPR per camera from the
    # cloud (cameras.<analytic>_enabled), but these appliance-wide switches
    # defaulted to false and nothing set them, so a fresh appliance never
    # ran either analytic even for a paying customer. Each worker still
    # does nothing for a camera without that camera's entitlement (and,
    # for People Counting / zones / alert lines, a customer-drawn line or
    # zone). Add-if-missing, like every default here: an explicit value,
    # true or false, is never changed. Facial recognition stays an
    # explicit opt-in (biometric processing), not an installer default.
    local analytics_flag
    for analytics_flag in ANYAICAM_LPR_ENABLED PEOPLE_COUNTING_ENABLED CUSTOMER_ANALYTICS_RULES_ENABLED; do
        grep -q "^${analytics_flag}=" "$VMS_ENV_FILE" 2>/dev/null || \
            printf '%s=true\n' "$analytics_flag" >> "$VMS_ENV_FILE"
    done

    # Generated once, per appliance, the first time this file has no
    # value yet -- and, like ANYAICAM_ENV/ANYAICAM_RUNTIME_ROLE above
    # (never like the always-refreshed build-identity keys below), NEVER
    # regenerated once present: rotating it silently on every
    # reinstall/repair would instantly invalidate every existing signed
    # session/cookie. 32 raw bytes (256 bits) of /dev/urandom entropy,
    # hex-encoded with only coreutils (od/tr) -- no new dependency, and
    # deliberately never derived from the appliance ID, hostname, MAC
    # address, or anything else an attacker could predict or observe by
    # other means. Never printed or logged anywhere: the generated value
    # exists only in this command substitution and the file it's
    # redirected into.
    grep -q '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE" 2>/dev/null || \
        printf 'ANYAICAM_APP_SECRETS=%s\n' "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$VMS_ENV_FILE"

    # Camera credential encryption key -- an appliance/installer
    # requirement, not a Samsung-only fix: confirmed live that a fresh
    # edge appliance never provisioned this at all, so ANY attempt to
    # add a discovered camera WITH ONVIF/RTSP credentials failed closed
    # (app/appliance_protocol.py's encrypt_camera_credentials() returns
    # nothing without a key, and app/partner_workspace.py's
    # request_camera_provisioning() 503s rather than ever storing
    # credentials it can't encrypt). Generated once, exactly like
    # ANYAICAM_APP_SECRETS above, and for the same reason NEVER
    # regenerated once present: this key is what every already-stored
    # camera credential (camera_credentials.encrypted_blob) is encrypted
    # with -- silently rotating it on a reinstall/repair would make every
    # existing credential permanently undecryptable, breaking every
    # already-working camera stream. A valid Fernet key (the format
    # app/appliance_protocol.py requires) is 32 random bytes, URL-safe
    # base64-encoded -- reproduced here with only coreutils (head/base64/
    # tr), the same no-new-dependency approach as ANYAICAM_APP_SECRETS,
    # since this must generate correctly before the VMS image (which
    # bundles the `cryptography` package) has even been built yet. Never
    # printed or logged anywhere: the generated value exists only in this
    # command substitution and the file it's redirected into.
    grep -q '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE" 2>/dev/null || \
        printf 'ANYAICAM_CAMERA_CREDENTIAL_KEY=%s\n' "$(head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_')" >> "$VMS_ENV_FILE"

    persist_product_mode

    # These two keys are installer-owned build identity. They are updated on
    # every reinstall/repair while all other customer configuration survives.
    upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_VMS_COMMIT" "$VMS_RELEASE_COMMIT"
    upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_BUILD_ID" "$VMS_RELEASE_COMMIT"
    chown anyaicam:anyaicam "$VMS_ENV_FILE" 2>/dev/null || true
    chmod 0640 "$VMS_ENV_FILE"
}

# The installed VMS software tree must be root-owned: the container
# bind-mounts $VMS_INSTALL_ROOT/app over /app, so whoever can write it
# controls the code the VMS runs. The paths the mirror excludes (legacy
# persistent state, and the separately-installed MediaMTX binary) are
# left exactly as they are.
normalize_vms_install_ownership() {
    local owner="${VMS_INSTALL_OWNER:-root:root}"
    find "$VMS_INSTALL_ROOT" \( -path "$VMS_INSTALL_ROOT/recordings" -o -path "$VMS_INSTALL_ROOT/data/config" \
        -o -path "$VMS_INSTALL_ROOT/.env" -o -path "$VMS_INSTALL_ROOT/mediamtx" \) -prune \
        -o -exec chown -h "$owner" {} +
}

# ---------------------------------------------------------------- rollback point
# (2026-09-25) A repair/upgrade rebuilds anyaicam-vms:latest in place and
# rsync --delete replaces the code, so before this the previous release
# survived only if someone tagged/archived it by hand first. Every
# non-clean install now leaves, BEFORE touching anything:
#   - the previous image tagged anyaicam-vms:rollback-<previous commit>
#   - the previous code tree (no recordings/config/.env/secrets) archived
#   - an online, integrity-checked copy of the VMS database
#   - a manifest (ROLLBACK_DIR/rollback-*.env, latest.env) that rollback.sh
#     restores from.
# Nothing here deletes or rewrites existing data; old rollback points are
# kept (pruning them is left to the operator).
ROLLBACK_DIR="${ANYAICAM_ROLLBACK_DIR:-/var/lib/anyaicam/rollback}"
VMS_IMAGE="${ANYAICAM_VMS_IMAGE:-anyaicam-vms}"
VMS_CONTAINER="${ANYAICAM_VMS_CONTAINER:-anyaicam-vms}"
VMS_DATABASE_NAME="partner_portal.db"

previous_vms_commit() {
    sed -n 's/^ANYAICAM_VMS_COMMIT=//p' "$VMS_ENV_FILE" 2>/dev/null | tail -n 1
}

# Prints the backup's path ("none" when there is no database yet). Online
# and consistent via sqlite3's backup API inside the running container
# (the same method used for every manual Ryzen backup), integrity-checked;
# a plain copy (with its WAL) only when the VMS is not running.
backup_vms_database() {
    local dest_name="$1" db="$VMS_RECORDINGS_DIR/$VMS_DATABASE_NAME"
    if [[ ! -f "$db" ]]; then
        echo "none"
        return 0
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$VMS_CONTAINER" 2>/dev/null)" == "true" ]]; then
        docker exec "$VMS_CONTAINER" python3 -c '
import sqlite3, sys
name = sys.argv[1]
src = sqlite3.connect("file:/app/recordings/'"$VMS_DATABASE_NAME"'?mode=ro", uri=True)
out = sqlite3.connect("/app/recordings/" + name)
src.backup(out); out.close(); src.close()
check = sqlite3.connect("file:/app/recordings/" + name + "?mode=ro", uri=True)
sys.exit(0 if check.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else 3)
' "$dest_name" >&2 || return 1
    else
        cp -p "$db" "$VMS_RECORDINGS_DIR/$dest_name" || return 1
        if [[ -f "$db-wal" ]]; then
            cp -p "$db-wal" "$VMS_RECORDINGS_DIR/$dest_name-wal" || return 1
        fi
    fi
    [[ -s "$VMS_RECORDINGS_DIR/$dest_name" ]] || return 1
    echo "$VMS_RECORDINGS_DIR/$dest_name"
}

create_rollback_point() {
    local previous short stamp image_tag="" archive db_backup manifest root_parent root_name
    previous="$(previous_vms_commit)"
    [[ -n "$previous" ]] || previous="unknown"
    short="${previous:0:12}"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$ROLLBACK_DIR" && chmod 0750 "$ROLLBACK_DIR" || return 1
    if [[ "$(id -u)" == "0" ]]; then chown root:root "$ROLLBACK_DIR"; fi

    if docker image inspect "$VMS_IMAGE:latest" >/dev/null 2>&1; then
        image_tag="$VMS_IMAGE:rollback-$short"
        docker tag "$VMS_IMAGE:latest" "$image_tag" || return 1
    fi

    root_parent="$(dirname "$VMS_INSTALL_ROOT")"
    root_name="$(basename "$VMS_INSTALL_ROOT")"
    archive="$ROLLBACK_DIR/vms-code-$short-$stamp.tar.gz"
    if [[ -d "$VMS_INSTALL_ROOT" ]]; then
        tar -czf "$archive" -C "$root_parent" \
            --exclude="$root_name/recordings" --exclude="$root_name/data/config" --exclude="$root_name/.env" \
            --exclude="$root_name/mediamtx" --exclude="$root_name/app/static/hls" --exclude="$root_name/app/recordings" \
            --exclude="$root_name/app/auto.key" --exclude="$root_name/app/auto.crt" --exclude='__pycache__' \
            "$root_name" || return 1
        gzip -t "$archive" || return 1
        chmod 0640 "$archive"
    else
        archive="none"
    fi

    db_backup="$(backup_vms_database "partner_portal-pre-${VMS_RELEASE_COMMIT:0:12}-$stamp.db")" || return 1

    manifest="$ROLLBACK_DIR/rollback-$short-$stamp.env"
    {
        printf 'ROLLBACK_COMMIT=%s\n' "$previous"
        printf 'ROLLBACK_IMAGE=%s\n' "${image_tag:-none}"
        printf 'ROLLBACK_CODE_ARCHIVE=%s\n' "$archive"
        printf 'ROLLBACK_DATABASE_BACKUP=%s\n' "$db_backup"
        printf 'ROLLBACK_CREATED_AT=%s\n' "$stamp"
        printf 'UPGRADE_TO_COMMIT=%s\n' "$VMS_RELEASE_COMMIT"
    } > "$manifest"
    chmod 0640 "$manifest"
    cp -f "$manifest" "$ROLLBACK_DIR/latest.env"
    log "Rollback point for $previous: image ${image_tag:-none}, code $archive, database $db_backup (manifest $manifest)."
}

deploy_vms() {
    local state="$1"

    [[ -d "$VMS_PAYLOAD_DIR/app" ]] || {
        echo "[ERROR] Missing built VMS payload: $VMS_PAYLOAD_DIR/app" >&2
        return 1
    }

    migrate_legacy_persistent_data "$VMS_INSTALL_ROOT/recordings" "$VMS_RECORDINGS_DIR" "VMS recordings/application state"
    migrate_legacy_persistent_data "$VMS_INSTALL_ROOT/data/config" "$VMS_DATA_CONFIG_DIR" "VMS data/config"
    migrate_legacy_persistent_file "$VMS_INSTALL_ROOT/.env" "$VMS_ENV_FILE" "VMS environment config"

    if [[ "$state" != "clean" ]]; then
        if [[ "${ANYAICAM_SKIP_ROLLBACK_POINT:-}" == "1" ]]; then
            log "WARNING: ANYAICAM_SKIP_ROLLBACK_POINT=1 -- replacing the installed VMS WITHOUT a rollback point."
        elif ! create_rollback_point; then
            echo "[ERROR] Could not create a rollback point for the installed VMS; nothing was changed. Fix the error above, or set ANYAICAM_SKIP_ROLLBACK_POINT=1 to replace it without one." >&2
            return 1
        fi
    fi

    install -d -m 0755 -o root -g root "$VMS_INSTALL_ROOT"

    log "Installing exact VMS release $VMS_RELEASE_COMMIT into $VMS_INSTALL_ROOT ..."
    # --delete makes /opt/anyaicam an exact software mirror of the release.
    # Legacy customer state locations are excluded defensively; current state
    # lives under /var/lib or /etc and is never part of this mirror.
    #
    # 'mediamtx/' is excluded for the same reason (2026-09-17, real bug
    # confirmed live on Ryzen): 10-install-mediamtx.sh places the real
    # MediaMTX binary at $VMS_INSTALL_ROOT/mediamtx, and its own
    # docstring promises an ordinary VMS-only release rebuild (one built
    # without --mediamtx-binary, which never embeds a payload/mediamtx/
    # directory at all) is a complete no-op for that binary -- "changes
    # nothing about live camera behavior". That promise was false: this
    # rsync runs BEFORE install_mediamtx() in install.sh's own pipeline
    # and, with --delete and no exclusion for it, silently deleted the
    # previously-installed, already-validated MediaMTX binary out from
    # under a P2P-enabled appliance the moment any unrelated VMS-only
    # repair (e.g. this session's own LPR/PPE fix) ran, breaking P2P
    # live view with no error anywhere in the install output.
    #
    # --no-owner/--no-group (2026-09-24, real defect confirmed live on
    # Ryzen): plain -a copied the PAYLOAD's own owner/group, and the
    # payload is unpacked by whichever login user extracted the release
    # tarball (uid 1000) -- so a root-run repair left /opt/anyaicam and
    # every VMS source file owned by that user, writable without sudo.
    # normalize_vms_install_ownership() also repairs files an earlier
    # install already left with the wrong owner (rsync skips unchanged
    # files, so --no-owner alone would never fix those).
    rsync -a --no-owner --no-group --delete \
        --exclude 'recordings/' --exclude 'data/config/' --exclude '.env' --exclude 'mediamtx/' \
        "$VMS_PAYLOAD_DIR/" "$VMS_INSTALL_ROOT/"
    normalize_vms_install_ownership

    ensure_vms_env

    log "Building VMS Docker image for release $VMS_RELEASE_COMMIT ..."
    (cd "$VMS_INSTALL_ROOT" && docker compose build)

    if [[ "$state" == "clean" ]]; then
        log "VMS deployed from exact release payload (clean install)."
    else
        log "VMS replaced with exact release payload (existing install); persistent state untouched."
    fi
}
