#!/usr/bin/env bash
# Product policy is independent of runtime role and development/production.
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
        ANYAICAM_PRODUCT_MODE=hybrid
        log 'Legacy install: assigning hybrid compatibility mode; preserving cloud feature behavior.'
    elif [[ -t 0 ]]; then
        read -r -p 'Choose product mode (local/hybrid): ' ANYAICAM_PRODUCT_MODE || return 2
        validate_product_mode "$ANYAICAM_PRODUCT_MODE" || return 2
    else
        echo '[ERROR] Fresh unattended install requires --product-mode=local or --product-mode=hybrid.' >&2
        return 2
    fi
    if [[ -z "$saved" && ( "$INSTALL_STATE" != clean || -f "$file" ) ]]; then
        PRODUCT_MODE_LEGACY=true
    fi
    export ANYAICAM_PRODUCT_MODE
}

persist_product_mode() {
    validate_product_mode "$ANYAICAM_PRODUCT_MODE" || return 2
    upsert_env_key "$VMS_ENV_FILE" ANYAICAM_PRODUCT_MODE "$ANYAICAM_PRODUCT_MODE"
    local value=false key
    # Missing flags in legacy releases were false. Pin that behavior during
    # migration, even when the selected compatibility label is hybrid.
    if [[ "$ANYAICAM_PRODUCT_MODE" == hybrid && "$PRODUCT_MODE_LEGACY" == false ]]; then
        value=true
    fi
    for key in ANYAICAM_CLOUD_UPLOAD_ENABLED ANYAICAM_LIVE_RELAY_ENABLED \
               ANYAICAM_RECORDING_UPLOAD_ENABLED ANYAICAM_ANALYTICS_SYNC_ENABLED; do
        grep -q "^${key}=" "$VMS_ENV_FILE" || printf '%s=%s\n' "$key" "$value" >> "$VMS_ENV_FILE"
    done
}
