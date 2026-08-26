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

deploy_vms() {
    local state="$1"
    install -d -m 0755 -o root -g root "$VMS_INSTALL_ROOT"

    log "Syncing VMS application source into $VMS_INSTALL_ROOT ..."
    # --update never overwrites a newer/equal destination file with an
    # older source one, and never touches destination-only files (a
    # locally-generated .env, for instance) -- safe for both a clean
    # copy and a reinstall/update in place.
    rsync -a --update \
        --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
        --exclude 'recordings' --exclude '*.pre-*' --exclude '*.db' \
        "$REPO_ROOT/app" "$REPO_ROOT/Dockerfile" "$REPO_ROOT/Dockerfile.production" \
        "$REPO_ROOT/docker-compose.yml" "$REPO_ROOT/requirements.txt" \
        "$VMS_INSTALL_ROOT/"

    if [[ ! -f "$VMS_INSTALL_ROOT/.env" ]]; then
        log "No existing .env found -- creating a minimal default (never overwriting an existing one)."
        {
            echo "ANYAICAM_RUNTIME_ROLE=edge"
            echo "ANYAICAM_ENVIRONMENT=production"
        } > "$VMS_INSTALL_ROOT/.env"
    fi

    log "Building the VMS Docker image..."
    (cd "$VMS_INSTALL_ROOT" && docker compose build)

    if [[ "$state" == "clean" ]]; then
        log "VMS deployed (clean install)."
    else
        log "VMS image rebuilt in place (existing install) -- .env and recordings untouched."
    fi
}
