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

# Confirmed live on Ryzen (2026-09-11, golden-foundation-rc2): a clean,
# correctly-installed, unclaimed appliance with zero cameras -- exactly
# the state every brand-new appliance is in immediately after install.sh,
# before claim/activation or camera discovery ever run -- legitimately
# returns HTTP 503 from GET /ready. This is main.py's readiness_snapshot()
# working exactly as designed: for RUNTIME_ROLE=edge, `ready` requires
# recording>0, which is structurally impossible before any camera exists.
# The OLD check here (`curl -fsS ... /ready`) used curl's -f flag, which
# treats ANY non-2xx status as failure -- so validate.sh could never pass
# on a genuinely fresh install, only on one where the operator had
# already, out of band, discovered/configured a camera first. That is
# not what a post-INSTALL validator should require: install.sh's own job
# ends before claim/camera-discovery even begin (see install.sh's own
# run_install(), which never calls either), so validate.sh cannot
# legitimately demand business-readiness state that install.sh itself
# never establishes.
#
# What validate.sh actually needs to confirm here is "the VMS process
# started correctly and every one of its own critical startup checks
# passed" -- main.py's self_test.ok, not the separate, deliberately
# stricter `ready` field. Fetching without -f (so a 503 still yields the
# response body, not a suppressed one) and checking self_test.ok in the
# JSON directly gives exactly that, and still correctly fails if the VMS
# is genuinely unreachable (curl produces no matching output) or actually
# broken (self_test.ok is false, exactly as it was for RC1's real
# defect -- see docs/PROJECT_CHECKPOINT.md).
ready_endpoint_self_test_ok() {
    curl -sS -m 5 http://127.0.0.1:8000/ready 2>/dev/null | grep -Fq '"self_test":{"ok":true'
}

run_validate() {
    load_release_metadata
    detect_install_state

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
    check "VMS local ready endpoint is reachable and self-test passes (business readiness -- e.g. a camera actually recording -- is intentionally not required at install time)" ready_endpoint_self_test_ok
    check "VMS /version reports exact approved commit" version_reports_release

    if [[ "$FAILURES" -eq 0 ]]; then
        log "Validation PASSED (0 failures; expected VMS release $VMS_RELEASE_COMMIT)."
        return 0
    else
        log "Validation FAILED ($FAILURES failures; expected VMS release $VMS_RELEASE_COMMIT)."
        return 1
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_validate "$@"
fi
