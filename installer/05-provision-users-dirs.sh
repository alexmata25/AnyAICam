#!/usr/bin/env bash
# System user + full-stack directory structure + permissions. Extends
# the existing agent-only pattern (appliance-agent/scripts/install.sh)
# to also cover the VMS's own state tree. Every operation here is
# create-only-if-missing -- never touches an existing directory's
# contents, so this is safe to re-run on an existing install.

provision_users_dirs() {
    local state="$1"
    id -u anyaicam >/dev/null 2>&1 || \
        useradd --system --home /var/lib/anyaicam --shell /usr/sbin/nologin anyaicam

    # install -d is idempotent: creates only what's missing.
    # /var/lib/anyaicam/vms/{recordings,hls,data-config} are the VMS's
    # persistent/runtime state tree (see 06-deploy-vms.sh and
    # docker-compose.yml) -- recordings and data-config hold protected
    # customer/config data, hls holds regenerable streaming output that
    # still needs a safe home outside the replaceable /opt/anyaicam
    # software directory.
    install -d -m 0750 -o anyaicam -g anyaicam \
        /opt/anyaicam-agent "$CONFIG_DIR" \
        /var/lib/anyaicam /var/lib/anyaicam/recordings \
        /var/lib/anyaicam/vms "$VMS_HLS_DIR" "$VMS_RECORDINGS_DIR" \
        "$VMS_DATA_CONFIG_DIR" \
        /var/log/anyaicam

    # Quarantine directory: defined and created in exactly one place
    # (here), at the corrected path. validate.sh checks this same
    # constant ($QUARANTINE_DIR, from install.sh) -- nothing else in
    # this installer creates or references a different path.
    install -d -m 0750 -o anyaicam -g anyaicam "$QUARANTINE_DIR"

    if [[ "$state" == "clean" ]]; then
        log "Provisioned anyaicam user and directory structure (clean install)."
    else
        log "Verified anyaicam user and directory structure (existing install) -- no existing files touched."
    fi
}
