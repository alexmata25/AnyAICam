#!/usr/bin/env bash
# Post-install validator. Does not mutate installation state.
set -euo pipefail
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install.sh
source "$INSTALLER_DIR/install.sh"

FAILURES=0
check() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        log "PASS: $description"
    else
        log "FAIL: $description"
        FAILURES=$((FAILURES + 1))
    fi
}

load_release_metadata
detect_install_state

version_reports_release() {
    curl -fsS -m 5 http://127.0.0.1:8000/version | grep -Fq "$VMS_RELEASE_COMMIT"
}

suspend_targets_masked() {
    # `systemctl is-enabled` exits non-zero for a masked unit (it isn't
    # "enabled"), so check() can't wrap it directly -- confirm the actual
    # state string instead.
    local target
    for target in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
        [[ "$(systemctl is-enabled "$target" 2>/dev/null)" == "masked" ]] || return 1
    done
}

check "install state is not partial" test "$INSTALL_STATE" != "partial"
check "anyaicam user exists" id -u anyaicam
check "config directory exists" test -d "$CONFIG_DIR"
check "identity file exists" test -f "$IDENTITY_FILE"
check "VMS release marker exists" test -f "$VMS_RELEASE_MARKER"
check "installed release marker contains exact approved commit" grep -q "\"vms_release_commit\": \"$VMS_RELEASE_COMMIT\"" "$VMS_RELEASE_MARKER"
check "VMS env contains exact approved commit" grep -q "^ANYAICAM_VMS_COMMIT=$VMS_RELEASE_COMMIT$" "$VMS_ENV_FILE"
# Existence/non-emptiness only -- the actual value is never read, printed,
# or compared here. Without this key, camera provisioning with ONVIF/RTSP
# credentials fails closed (see 06-deploy-vms.sh's ensure_vms_env()).
check "camera credential encryption key is configured" grep -q '^ANYAICAM_CAMERA_CREDENTIAL_KEY=.' "$VMS_ENV_FILE"
check "quarantine directory exists at corrected path ($QUARANTINE_DIR)" test -d "$QUARANTINE_DIR"
check "quarantine directory is owned by anyaicam" test "$(stat -c %U "$QUARANTINE_DIR" 2>/dev/null)" = "anyaicam"
check "quarantine directory permissions are protected (0750)" test "$(stat -c %a "$QUARANTINE_DIR" 2>/dev/null)" = "750"
check "anyaicam-agent.service is enabled" systemctl is-enabled --quiet anyaicam-agent.service
check "anyaicam-vms.service is enabled" systemctl is-enabled --quiet anyaicam-vms.service
check "anyaicam-vms.service is active" systemctl is-active --quiet anyaicam-vms.service
check "system suspend/hibernate is disabled (appliance must stay online 24/7)" suspend_targets_masked
check "VMS local health endpoint responds" curl -fsS -m 5 -o /dev/null http://127.0.0.1:8000/health
check "VMS local ready endpoint responds" curl -fsS -m 5 -o /dev/null http://127.0.0.1:8000/ready
check "VMS /version reports exact approved commit" version_reports_release

if [[ "$FAILURES" -eq 0 ]]; then
    log "Validation PASSED (0 failures; expected VMS release $VMS_RELEASE_COMMIT)."
    exit 0
else
    log "Validation FAILED ($FAILURES failures; expected VMS release $VMS_RELEASE_COMMIT)."
    exit 1
fi
