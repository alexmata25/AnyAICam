#!/usr/bin/env bash
# Product policy is independent of runtime role and development/production.
#
# Reconciliation, 2026-09-21 (see docs/product-mode-local-hybrid-2026-09-21.md):
# this file originally (Codex, isolated installer work, commit
# 59bed3ddf04b0cb9936238d05a89779774d409ba) also materialized four
# individual ANYAICAM_*_ENABLED flags here in persist_product_mode(),
# duplicating the mode -> flag-default mapping app/product_mode.py's
# resolve_cloud_flag()/FLAG_REGISTRY already own as the single runtime
# authority. That four-flag list was also wrong on its own terms against
# this branch's actual app/ code: ANYAICAM_CLOUD_UPLOAD_ENABLED gates
# app/main.py's cloud_upload_worker(), which is explicitly retired and a
# no-op regardless of that flag's value (see its own docstring and
# app/tests/test_cloud_upload_worker_retired.py) -- while three flags
# resolve_cloud_flag() actually governs (ANYAICAM_EVENT_MEDIA_UPLOAD_
# ENABLED, ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED, ANYAICAM_LIVE_P2P_
# ENABLED) were never written here at all. Writing ANYAICAM_PRODUCT_
# MODE=hybrid as a "legacy compatibility label" while only pinning some
# flags false would have silently left those three unpinned flags
# resolving to Hybrid's TRUE default the moment app/product_mode.py's
# own resolver saw that env var -- exactly the activation-during-upgrade
# this feature was designed to prevent.
#
# This version writes ONLY the ANYAICAM_PRODUCT_MODE value here (an
# explicit operator/installer choice) and never materializes any
# individual _ENABLED flag -- resolve_cloud_flag() is the sole place
# that mapping is ever defined, so there is no way for this file and
# the Python runtime to disagree about what a given mode implies. A
# legacy install with no key yet leaves ANYAICAM_PRODUCT_MODE
# completely UNSET (not a guessed "hybrid" label) -- resolve_cloud_
# flag()'s own legacy_default=False already reproduces every governed
# flag's pre-existing false default for exactly this case, with zero
# shell-side duplication, and leaving the env var itself unset is what
# keeps this appliance eligible for the cloud's own future entitlement-
# driven auto-apply (appliance_cloud._queue_product_mode_restart()) the
# first time a real mode is resolved for it -- an explicit env var here
# would otherwise permanently block that (see product_mode.py's
# persist_mode() docstring: an explicit env var always wins), even
# though nobody actually chose Hybrid, they just predate this feature.
# An operator's own already-present explicit per-feature flag (from a
# release template or hand-set for a pilot) is completely untouched
# either way, by construction: this file no longer writes flags at all.
validate_product_mode() {
    case "$1" in
        local|hybrid) return 0 ;;
        *) echo '[ERROR] Product mode must be exactly local or hybrid (not blank).' >&2; return 2 ;;
    esac
}

select_product_mode() {
    local file="$VMS_ENV_FILE" saved="" count=0
    PRODUCT_MODE_LEGACY=false
    [[ -f "$file" ]] || file="$VMS_INSTALL_ROOT/.env"
    if [[ -f "$file" ]]; then
        count=$(grep -c '^ANYAICAM_PRODUCT_MODE=' "$file" || true)
        if (( count > 1 )); then
            echo '[ERROR] Duplicate ANYAICAM_PRODUCT_MODE entries; resolve before installing.' >&2
            return 2
        fi
        if (( count == 1 )); then
            saved=$(sed -n 's/^ANYAICAM_PRODUCT_MODE=//p' "$file")
            validate_product_mode "$saved" || return 2
        fi
    fi
    if [[ ${ANYAICAM_PRODUCT_MODE+x} ]]; then
        validate_product_mode "$ANYAICAM_PRODUCT_MODE" || return 2
        if [[ -n "$saved" && "$saved" != "$ANYAICAM_PRODUCT_MODE" ]]; then
            echo '[ERROR] Installer cannot switch an existing product mode; reconcile its configuration explicitly.' >&2
            return 2
        fi
    elif [[ -n "$saved" ]]; then
        ANYAICAM_PRODUCT_MODE="$saved"
    elif [[ "$INSTALL_STATE" != clean || -f "$file" ]]; then
        # Legacy install, never asked before: leave ANYAICAM_PRODUCT_MODE
        # genuinely unset rather than guessing a value -- see this file's
        # own header comment for why a guessed label here is actively
        # harmful, not merely unnecessary.
        PRODUCT_MODE_LEGACY=true
        unset ANYAICAM_PRODUCT_MODE
        log 'Legacy install: no product mode recorded yet -- leaving unset so existing runtime defaults are preserved exactly and a future cloud-driven entitlement can still apply a real mode.'
        return 0
    elif [[ -t 0 ]]; then
        read -r -p 'Choose product mode (local/hybrid): ' ANYAICAM_PRODUCT_MODE || return 2
        validate_product_mode "$ANYAICAM_PRODUCT_MODE" || return 2
    else
        echo '[ERROR] Fresh unattended install requires --product-mode=local or --product-mode=hybrid.' >&2
        return 2
    fi
    export ANYAICAM_PRODUCT_MODE
}

persist_product_mode() {
    # See this file's own header comment: intentionally a no-op when no
    # real mode was ever chosen (the legacy-install path in select_
    # product_mode() above returns with ANYAICAM_PRODUCT_MODE unset) --
    # never writes a guessed value, and never materializes any
    # individual _ENABLED flag under any circumstance.
    [[ -z "${ANYAICAM_PRODUCT_MODE:-}" ]] && return 0
    validate_product_mode "$ANYAICAM_PRODUCT_MODE" || return 2
    upsert_env_key "$VMS_ENV_FILE" ANYAICAM_PRODUCT_MODE "$ANYAICAM_PRODUCT_MODE"
}
