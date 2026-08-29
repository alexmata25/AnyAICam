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

    # These two keys are installer-owned build identity. They are updated on
    # every reinstall/repair while all other customer configuration survives.
    upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_VMS_COMMIT" "$VMS_RELEASE_COMMIT"
    upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_BUILD_ID" "$VMS_RELEASE_COMMIT"
    chown anyaicam:anyaicam "$VMS_ENV_FILE" 2>/dev/null || true
    chmod 0640 "$VMS_ENV_FILE"
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

    install -d -m 0755 -o root -g root "$VMS_INSTALL_ROOT"

    log "Installing exact VMS release $VMS_RELEASE_COMMIT into $VMS_INSTALL_ROOT ..."
    # --delete makes /opt/anyaicam an exact software mirror of the release.
    # Legacy customer state locations are excluded defensively; current state
    # lives under /var/lib or /etc and is never part of this mirror.
    rsync -a --delete \
        --exclude 'recordings/' --exclude 'data/config/' --exclude '.env' \
        "$VMS_PAYLOAD_DIR/" "$VMS_INSTALL_ROOT/"

    ensure_vms_env

    log "Building VMS Docker image for release $VMS_RELEASE_COMMIT ..."
    (cd "$VMS_INSTALL_ROOT" && docker compose build)

    if [[ "$state" == "clean" ]]; then
        log "VMS deployed from exact release payload (clean install)."
    else
        log "VMS replaced with exact release payload (existing install); persistent state untouched."
    fi
}
