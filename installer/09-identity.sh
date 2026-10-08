#!/usr/bin/env bash
# Stable appliance identity + mutable installed-release marker.
#
# These files live in $CONFIG_DIR, which the anyaicam user owns, so root
# writes them only through the symlink-safe helper (06-deploy-vms.sh
# agent_file): never through a planted symlink, owner and mode set on the
# new file itself, atomic rename.

identity_provision() {
    local state="$1"
    if [[ -L "$IDENTITY_FILE" ]]; then
        echo "[ERROR] $IDENTITY_FILE is a symbolic link; refusing to use or replace it." >&2
        return 1
    fi
    if [[ -f "$IDENTITY_FILE" ]]; then
        log "Existing appliance identity found -- preserved, not regenerated (sha256=$(sha256sum "$IDENTITY_FILE" | cut -d' ' -f1))."
    else
        if [[ "$state" != "clean" ]]; then
            log "WARNING: no identity file found on a non-clean install; generating one as part of repair."
        fi
        local appliance_id
        appliance_id="$(cat /proc/sys/kernel/random/uuid)"
        {
            printf 'op write\npath %s\nmode 0600\nowner anyaicam\nif_missing\ncontent\n' "$IDENTITY_FILE"
            echo "{"
            echo "  \"appliance_id\": \"$appliance_id\","
            echo "  \"installer_version\": \"$INSTALLER_VERSION\","
            echo "  \"installed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
            echo "}"
        } | agent_file
        log "Generated new appliance identity: $appliance_id"
    fi

    # Installer version is mutable metadata; identity remains stable.
    printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n%s\n' "$VERSION_MARKER" "$INSTALLER_VERSION" | agent_file
}

# --------------------------------------------------------------------------
# Headless label claim (2026-10-07). Every unit gets a claim code when it is
# imaged; it is printed on the unit's label (text + QR) so the owner can claim
# the appliance from a phone or computer with no monitor, keyboard or terminal
# (see app/appliance_claims.py "Headless label claim" and
# appliance-agent/anyaicam_agent/headless_claim.py).
#   * The plaintext code exists only in $CLAIM_LABEL_FILE (root-only, for the
#     label printer and for support to reprint a lost label).
#   * The agent gets only its verifier, sha256("anyaicam-label-claim-v1:"+code),
#     in $CONFIG_DIR/label_claim.json; the cloud stores only a hash of that.
#   * Kept across repairs and reinstalls (the printed label stays valid); the
#     verifier is rewritten from the root-only file every run so they cannot
#     drift apart.
CLAIM_LABEL_DIR="${CLAIM_LABEL_DIR:-/var/lib/anyaicam-label}"
CLAIM_LABEL_FILE="${CLAIM_LABEL_FILE:-$CLAIM_LABEL_DIR/claim-label.txt}"
LABEL_ALPHABET=0123456789ABCDEFGHJKMNPQRSTVWXYZ  # Crockford base32: no I, L, O, U

# 12 characters, each from one /dev/urandom byte mod 32 (256 is a multiple of
# 32, so every character is uniform): 60 bits.
label_code_generate() {
    local code="" byte
    for byte in $(od -An -N12 -tu1 /dev/urandom); do
        code+="${LABEL_ALPHABET:$((byte % 32)):1}"
    done
    [[ ${#code} -eq 12 ]] || return 1
    printf '%s' "$code"
}

label_code_valid() {
    [[ "$1" =~ ^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{12}$ ]]
}

label_verifier() {
    printf '%s' "anyaicam-label-claim-v1:$1" | sha256sum | cut -d' ' -f1
}

label_code_pretty() {
    printf '%s-%s-%s' "${1:0:4}" "${1:4:4}" "${1:8:4}"
}

# The code saved on this unit, compact form, or nothing.
label_code_saved() {
    [[ -f "$CLAIM_LABEL_FILE" && ! -L "$CLAIM_LABEL_FILE" ]] || return 0
    sed -n 's/^Claim code: *//p' "$CLAIM_LABEL_FILE" | head -1 | tr -d ' -' | tr '[:lower:]' '[:upper:]'
}

claim_label_provision() {
    local code appliance_id portal tmp
    if [[ -L "$CLAIM_LABEL_DIR" || -L "$CLAIM_LABEL_FILE" ]]; then
        echo "[ERROR] $CLAIM_LABEL_DIR or $CLAIM_LABEL_FILE is a symbolic link; refusing to use it." >&2
        return 1
    fi
    if [[ $EUID -eq 0 ]]; then
        install -d -m 0700 -o root -g root "$CLAIM_LABEL_DIR"
    else  # only the tests run this unprivileged
        mkdir -p "$CLAIM_LABEL_DIR" && chmod 0700 "$CLAIM_LABEL_DIR"
    fi
    code="$(label_code_saved)"
    if label_code_valid "$code"; then
        log "Existing claim label kept (the printed label stays valid)."
    else
        [[ -z "$code" ]] || log "WARNING: the saved claim label was unreadable; generating a new one (reprint the label)."
        code="$(label_code_generate)" || { echo "[ERROR] could not generate a claim label code." >&2; return 1; }
        appliance_id="$(sed -n 's/.*"appliance_id": *"\([^"]*\)".*/\1/p' "$IDENTITY_FILE" | head -1)"
        portal="${ANYAICAM_INSTALL_PORTAL_URL:-https://app.anyaicam.com}"
        tmp="$(mktemp "$CLAIM_LABEL_DIR/.claim-label.XXXXXX")"
        chmod 0600 "$tmp"
        {
            echo "AnyAiCam appliance claim label -- print on the unit's label. Keep private."
            echo "Claim code: $(label_code_pretty "$code")"
            echo "QR code: ${portal%/}/claim#label=$(label_code_pretty "$code")"
            echo "Appliance ID: $appliance_id"
            echo "Created: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        } > "$tmp"
        mv -f "$tmp" "$CLAIM_LABEL_FILE"
        log "Generated a new claim label (root-only, $CLAIM_LABEL_FILE); print it on the unit's label."
    fi
    # Never logged: the code itself, or its verifier.
    {
        printf 'op write\npath %s\nmode 0600\nowner anyaicam\ncontent\n' "${LABEL_CLAIM_JSON:-$CONFIG_DIR/label_claim.json}"
        printf '{"version": 1, "verifier": "%s"}\n' "$(label_verifier "$code")"
    } | agent_file
}

# --portal-url (factory imaging): the AnyAiCam cloud this appliance claims
# itself with. Refused before the install changes anything.
validate_install_portal_url() {
    local url="$1" rest host
    [[ "$url" == https://* ]] || { echo "[ERROR] --portal-url must be an https:// address (got '$url')." >&2; return 2; }
    rest="${url#https://}"; host="${rest%%/*}"; host="${host%%:*}"
    if [[ -z "$host" || "$url" =~ [[:space:]] || "$host" == localhost || "$host" =~ ^127\. || "$host" == 0.0.0.0 ]]; then
        echo "[ERROR] --portal-url must be the AnyAiCam cloud address, for example https://app.anyaicam.com (got '$url')." >&2
        return 2
    fi
}

# Zero-terminal onboarding (2026-10-08): a customer install links itself to
# the AnyAiCam cloud from the desktop "AnyAiCam Setup" page, so without
# --portal-url the agent now points at the AnyAiCam cloud by default -- unless
# agent.env already names another valid https cloud (kept, e.g. a repair).
ANYAICAM_DEFAULT_PORTAL_URL="${ANYAICAM_DEFAULT_PORTAL_URL:-https://app.anyaicam.com}"

current_agent_portal_url() {
    local file="${AGENT_ENV_FILE:-$CONFIG_DIR/agent.env}"
    [[ -f "$file" && ! -L "$file" ]] || return 0
    sed -n 's/^ANYAICAM_PORTAL_URL=//p' "$file" | tail -1 | tr -d '"'"'"
}

cloud_portal_provision() {
    local portal="${ANYAICAM_INSTALL_PORTAL_URL:-}" current
    if [[ -z "$portal" ]]; then
        current="$(current_agent_portal_url)"
        if [[ -n "$current" ]] && validate_install_portal_url "$current" >/dev/null 2>&1; then
            log "Agent cloud portal kept: ${current%/}."
            return 0
        fi
        portal="$ANYAICAM_DEFAULT_PORTAL_URL"
    fi
    printf 'op update_env\npath %s\nmode 0600\nowner anyaicam\noverride ANYAICAM_PORTAL_URL=%s\noverride ANYAICAM_AGENT_MODE=production\n' \
        "${AGENT_ENV_FILE:-$CONFIG_DIR/agent.env}" "${portal%/}" | agent_file
    log "Agent cloud portal set to ${portal%/} (production)."
}

stamp_release() {
    local record
    record="$(
        echo "{"
        echo "  \"vms_release_commit\": \"$VMS_RELEASE_COMMIT\","
        echo "  \"release_archive_sha256\": \"${VMS_RELEASE_SHA256:-}\","
        echo "  \"installer_source_commit\": \"$INSTALLER_SOURCE_COMMIT\","
        echo "  \"mediamtx_included\": \"${MEDIAMTX_INCLUDED:-unknown}\","
        echo "  \"mediamtx_sha256\": \"${MEDIAMTX_SHA256:-}\","
        echo "  \"installer_version\": \"$INSTALLER_VERSION\","
        echo "  \"release_version\": \"${RELEASE_VERSION:-}\","
        echo "  \"installed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        echo "}"
    )"
    printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n%s\n' "$VMS_RELEASE_MARKER" "$record" | agent_file
    # Software Update keeps its own root-owned copy for the downgrade check
    # (the marker above sits in a directory the agent user owns). It is
    # written from the same content, never copied back out of that directory.
    if [[ -n "${RELEASE_VERSION:-}" && -d "${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}" ]]; then
        printf 'op write\npath %s\nmode 0644\nowner root\ncontent\n%s\n' \
            "${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}/installed_release.json" "$record" | agent_file
    fi
    log "Stamped installed VMS release: $VMS_RELEASE_COMMIT"
}
