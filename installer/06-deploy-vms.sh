#!/usr/bin/env bash
# VMS app source placement, Docker image build, compose config. Reuses
# Dockerfile / Dockerfile.production / docker-compose.yml from this
# repo rather than reinventing the build. Deploys to a canonical
# customer install path ($VMS_INSTALL_ROOT, /opt/anyaicam) instead of
# assuming a developer's home directory -- unlike the current Ryzen's
# ad hoc git-checkout-in-home-dir layout, which this installer
# intentionally does not replicate.
#
# Both Dockerfile and Dockerfile.production must be copied: the
# committed docker-compose.yml uses `build: .`, which Compose resolves
# to a file literally named Dockerfile regardless of any other
# .production-suffixed file present -- confirmed against the real
# production layout, where both files exist side by side and the
# plain Dockerfile is the one actually built from. Deploying only
# Dockerfile.production (the original reconstruction's mistake) left
# `docker compose build` with nothing to read and failed a from-fresh
# clean install outright.
#
# requirements.txt must also be copied: the plain Dockerfile does
# `COPY requirements.txt /tmp/requirements.txt`, and that file is
# tracked only at repo root (not under app/). Inspecting the Dockerfile
# confirms it references exactly two build-context paths beyond itself
# -- requirements.txt and ./app -- nothing else, so this is the last
# file this deploy step was missing.
#
# $VMS_INSTALL_ROOT must contain ONLY replaceable software: default
# uninstall does `rm -rf $VMS_INSTALL_ROOT`. Before this fix,
# docker-compose.yml bind-mounted VMS recordings and config from
# relative paths under $VMS_INSTALL_ROOT (./recordings, ./data/config,
# .env) -- a default uninstall silently destroyed real customer data
# (recordings, the partner_portal.db, ~90 JSON/JSONL application state
# files, camera folders, media, backups) despite its own log message
# claiming they were preserved. Fixed by pointing docker-compose.yml at
# absolute persistent paths instead:
#   - /var/lib/anyaicam/vms/recordings -- protected customer/app state
#     (everything that lived under RECORDINGS_FOLDER in app/main.py)
#   - /var/lib/anyaicam/vms/data-config -- protected config (confirmed
#     unused by current app code, migrated defensively anyway)
#   - /var/lib/anyaicam/vms/hls -- runtime/regenerable streaming
#     output only; no migration guarantee needed, old segments can
#     simply be recreated
#   - /etc/anyaicam/vms.env -- persistent VMS environment config,
#     alongside the agent's own agent.env

migrate_legacy_persistent_data() {
    # Idempotent: once a legacy source is gone, its check here is a
    # no-op on every subsequent run. Never overwrites data already
    # present at the destination (rsync --ignore-existing, plus a
    # per-file verification pass before deleting anything from the
    # legacy location -- a file whose destination copy doesn't match
    # is left in place at the old path for manual review rather than
    # silently dropped).
    local old="$1" new="$2" label="$3"
    [[ -d "$old" ]] || return 0
    log "Found legacy $label under $old -- migrating to $new ..."
    # mkdir+chmod+chown separately, not `install -d -o anyaicam`: the
    # anyaicam user always exists by this point in a real install
    # (provision_users_dirs runs first), but install(1) refuses to
    # create anything at all if the -o user doesn't resolve -- this
    # way directory creation never depends on that resolving, and
    # ownership is still applied whenever it does.
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
            log "WARNING: could not verify migration of $label file '$rel' -- leaving it in place at $old for manual review."
            unresolved=1
        fi
    done < <(find "$old" -type f -print0)
    find "$old" -type d -empty -delete 2>/dev/null || true
    if [[ "$unresolved" -eq 0 ]]; then
        rmdir "$old" 2>/dev/null || true
    fi
    if [[ -d "$old" ]]; then
        log "Legacy $label directory $old still contains unresolved files -- not fully removed."
    else
        log "Legacy $label migration to $new complete and verified."
    fi
}

migrate_legacy_persistent_file() {
    local old="$1" new="$2" label="$3"
    [[ -f "$old" ]] || return 0
    if [[ ! -f "$new" ]]; then
        log "Found legacy $label at $old -- migrating to $new ..."
        mkdir -p "$(dirname "$new")"
        cp -p "$old" "$new"
        chmod 0644 "$new"
        chown anyaicam:anyaicam "$new" 2>/dev/null || true
        rm -f "$old"
        log "Legacy $label migration to $new complete."
    elif cmp -s "$old" "$new"; then
        rm -f "$old"
    else
        log "WARNING: legacy $label at $old differs from existing $new -- leaving $old in place, not overwriting persistent config."
    fi
}

deploy_vms() {
    local state="$1"

    # Migrate any pre-existing persistent data out of the replaceable
    # software directory before anything else touches it. Safe/no-op
    # on a genuinely clean install (nothing to migrate) and on every
    # run after the first successful migration.
    migrate_legacy_persistent_data "$VMS_INSTALL_ROOT/recordings" "$VMS_RECORDINGS_DIR" "VMS recordings/application state"
    migrate_legacy_persistent_data "$VMS_INSTALL_ROOT/data/config" "$VMS_DATA_CONFIG_DIR" "VMS data/config"
    migrate_legacy_persistent_file "$VMS_INSTALL_ROOT/.env" "$VMS_ENV_FILE" "VMS environment config"

    install -d -m 0755 -o root -g root "$VMS_INSTALL_ROOT"

    log "Syncing VMS application source into $VMS_INSTALL_ROOT ..."
    # --update never overwrites a newer/equal destination file with an
    # older source one, and never touches destination-only files --
    # safe for both a clean copy and a reinstall/update in place.
    # recordings/data excluded defensively even though the repo source
    # never contains them; persistent state no longer lives under
    # $VMS_INSTALL_ROOT at all as of this fix.
    rsync -a --update \
        --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
        --exclude 'recordings' --exclude '*.pre-*' --exclude '*.db' \
        "$REPO_ROOT/app" "$REPO_ROOT/Dockerfile" "$REPO_ROOT/Dockerfile.production" \
        "$REPO_ROOT/docker-compose.yml" "$REPO_ROOT/requirements.txt" \
        "$VMS_INSTALL_ROOT/"

    if [[ ! -f "$VMS_ENV_FILE" ]]; then
        log "No existing VMS environment config found -- creating a minimal default (never overwriting an existing one)."
        mkdir -p "$CONFIG_DIR"
        chmod 0750 "$CONFIG_DIR"
        chown anyaicam:anyaicam "$CONFIG_DIR" 2>/dev/null || true
        {
            echo "ANYAICAM_RUNTIME_ROLE=edge"
            echo "ANYAICAM_ENVIRONMENT=production"
        } > "$VMS_ENV_FILE"
        chown anyaicam:anyaicam "$VMS_ENV_FILE" 2>/dev/null || true
        chmod 0640 "$VMS_ENV_FILE"
    fi

    log "Building the VMS Docker image..."
    (cd "$VMS_INSTALL_ROOT" && docker compose build)

    if [[ "$state" == "clean" ]]; then
        log "VMS deployed (clean install)."
    else
        log "VMS image rebuilt in place (existing install) -- persistent recordings, config, and data-config untouched."
    fi
}
