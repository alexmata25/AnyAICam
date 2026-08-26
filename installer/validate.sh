#!/usr/bin/env bash
# Post-install validator. Standalone -- run separately after
# install.sh, or independently at any time to check current state.
# This is where the original quarantine-path bug lived: the check
# below tests the CORRECTED path ($QUARANTINE_DIR, from install.sh)
# only -- there is no second, alternate path defined anywhere in this
# installer for it to disagree with.
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

detect_install_state

check "install state is not partial" test "$INSTALL_STATE" != "partial"
check "anyaicam user exists" id -u anyaicam
check "config directory exists" test -d "$CONFIG_DIR"
check "identity file exists" test -f "$IDENTITY_FILE"
check "quarantine directory exists at the corrected path ($QUARANTINE_DIR)" test -d "$QUARANTINE_DIR"
check "quarantine directory is owned by anyaicam" test "$(stat -c %U "$QUARANTINE_DIR" 2>/dev/null)" = "anyaicam"
check "quarantine directory permissions are protected (0750)" test "$(stat -c %a "$QUARANTINE_DIR" 2>/dev/null)" = "750"
check "anyaicam-agent.service is enabled" systemctl is-enabled --quiet anyaicam-agent.service
check "anyaicam-vms.service is enabled" systemctl is-enabled --quiet anyaicam-vms.service
check "anyaicam-vms.service is active" systemctl is-active --quiet anyaicam-vms.service
check "VMS local health endpoint responds" curl -fsS -m 5 -o /dev/null http://127.0.0.1:8000/health

if [[ "$FAILURES" -eq 0 ]]; then
    log "Validation PASSED (0 failures)."
    exit 0
else
    log "Validation FAILED ($FAILURES failures)."
    exit 1
fi
