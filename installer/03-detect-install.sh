#!/usr/bin/env bash
# Existing-install detection -- the single source of truth for whether
# this is a clean install, a valid existing install (reinstall/repair
# candidate), or a partial/broken one. Sourced by install.sh,
# uninstall.sh, and validate.sh so all three always agree on the same
# answer instead of each re-implementing their own guess.

detect_install_state() {
    local present=0 total=5

    [[ -d "$CONFIG_DIR" ]] && present=$((present + 1))
    [[ -f "$VERSION_MARKER" ]] && present=$((present + 1))
    id -u anyaicam >/dev/null 2>&1 && present=$((present + 1))
    { [[ -f "$VMS_SERVICE_FILE" ]] || [[ -f "$VMS_INSTALL_ROOT/docker-compose.yml" ]]; } && present=$((present + 1))
    docker image inspect anyaicam-vms >/dev/null 2>&1 && present=$((present + 1))

    if [[ "$present" -eq 0 ]]; then
        INSTALL_STATE="clean"
    elif [[ "$present" -eq "$total" ]]; then
        INSTALL_STATE="existing"
    else
        # Some markers present, not all -- a genuinely partial install.
        # Never silently classified as clean: that would mean skipping
        # the strict 100GB clean-install storage requirement on a box
        # that may still need a from-scratch amount of working space,
        # and never treating it as fully "existing" either, since
        # something is verifiably missing/broken.
        INSTALL_STATE="partial"
    fi
    log "Install state detection: $present/$total markers present -> $INSTALL_STATE"
    export INSTALL_STATE
}
