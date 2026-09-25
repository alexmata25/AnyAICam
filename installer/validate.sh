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

# UDP 8189 must be the VMS container's own publish, with no other program
# on the host holding the port (see 01-preflight.sh's webrtc_port_preflight).
webrtc_port_owned_by_vms() {
    webrtc_port_published_by_vms || return 1
    ! webrtc_udp_listeners | grep -v '"docker-proxy"' | grep -q .
}

version_reports_release() {
    curl -fsS -m 5 http://127.0.0.1:8000/version | grep -Fq "$VMS_RELEASE_COMMIT"
}

vms_health_ok() {
    curl -fsS -m 5 -o /dev/null http://127.0.0.1:8000/health
}

# Startup race (2026-09-24, confirmed live on Ryzen): install.sh restarts
# the VMS as its last step and validate.sh runs straight after, but the
# VMS needs several seconds (YOLO/model imports) before uvicorn is
# listening -- so the three local HTTP checks each got exactly one
# attempt against a process that was still starting, and all failed on a
# healthy install. Each HTTP check now retries until it passes or the
# shared startup deadline (VMS_STARTUP_WAIT_SECONDS, default 120s,
# measured from the first HTTP check) runs out; a VMS that genuinely never
# comes up still fails every one of them.
VMS_STARTUP_WAIT_SECONDS="${VMS_STARTUP_WAIT_SECONDS:-120}"
VMS_STARTUP_POLL_SECONDS="${VMS_STARTUP_POLL_SECONDS:-2}"
VMS_STARTUP_DEADLINE=""

retry_until_vms_started() {
    if [[ -z "$VMS_STARTUP_DEADLINE" ]]; then
        VMS_STARTUP_DEADLINE=$(( $(date +%s) + VMS_STARTUP_WAIT_SECONDS ))
    fi
    until "$@"; do
        (( $(date +%s) < VMS_STARTUP_DEADLINE )) || return 1
        sleep "$VMS_STARTUP_POLL_SECONDS"
    done
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

# MediaMTX packaging regression, permanent guard (2026-09-17, second real
# occurrence -- see docs/PROJECT_CHECKPOINT.md). validate.sh previously had
# zero awareness of MediaMTX at all: a release built without it (an
# --mediamtx-binary flag simply forgotten on the build command, now
# impossible to do silently -- build_release_installer.py requires an
# explicit choice, see its own --no-mediamtx help) could report "0
# failures" on an appliance where P2P live view was completely broken.
#
# Only a hard requirement when this appliance actually has P2P live view
# turned on (ANYAICAM_LIVE_P2P_ENABLED=true in vms.env) -- an appliance
# that never enables it is correctly unaffected, matching every other
# P2P-gated behavior in this codebase (10-install-mediamtx.sh's own
# docstring). Deliberately never executes the binary (no --version
# invocation): this script can run against a REAL appliance where
# webrtc_publisher.py may already have MediaMTX running and bound to its
# real ports, and this project has no confirmed-safe, side-effect-free
# CLI invocation for the real binary to fall back on -- existence,
# the executable bit, and a checksum match against this exact release's
# own recorded hash (when the release build actually embedded one) give
# the same assurance install_mediamtx() itself already relies on, with
# zero execution risk.
mediamtx_required_and_usable() {
    grep -q '^ANYAICAM_LIVE_P2P_ENABLED=true$' "$VMS_ENV_FILE" 2>/dev/null || return 0

    if [[ ! -f "$MEDIAMTX_BINARY_PATH" ]]; then
        echo "MediaMTX binary missing at $MEDIAMTX_BINARY_PATH while ANYAICAM_LIVE_P2P_ENABLED=true -- P2P live view is broken." >&2
        return 1
    fi
    if [[ ! -x "$MEDIAMTX_BINARY_PATH" ]]; then
        echo "MediaMTX binary at $MEDIAMTX_BINARY_PATH exists but is not executable." >&2
        return 1
    fi

    # Cross-check against THIS release's own recorded checksum, when this
    # release build actually embedded one. A release that intentionally
    # did not embed MediaMTX (--no-mediamtx, an ordinary VMS-only rebuild)
    # has nothing to cross-check here -- the presence/executable checks
    # above are what protect an appliance repaired from such a release,
    # since 06-deploy-vms.sh's rsync already excludes mediamtx/ from
    # deletion in that case, leaving a prior release's binary untouched.
    local recorded_sha
    recorded_sha="$(grep -o '"mediamtx_sha256": "[0-9a-f]\{64\}"' "$VMS_RELEASE_MARKER" 2>/dev/null | grep -o '[0-9a-f]\{64\}')"
    if [[ -n "$recorded_sha" ]]; then
        local actual_sha
        actual_sha="$(sha256sum "$MEDIAMTX_BINARY_PATH" | cut -d' ' -f1)"
        if [[ "$actual_sha" != "$recorded_sha" ]]; then
            echo "MediaMTX binary checksum ($actual_sha) does not match this release's recorded checksum ($recorded_sha)." >&2
            return 1
        fi
    fi
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
    # Confirmed live on Ryzen (2026-09-11): a stale systemd drop-in
    # (see appliance-agent/scripts/uninstall.sh's own fix/incident
    # writeup) crash-looped anyaicam-agent.service 600+ times on a
    # freshly-installed, otherwise-passing RC3 -- and validate.sh
    # reported PASS the whole time, because it only ever checked
    # is-enabled, never is-active. A unit stuck in `Restart=always`
    # crash-loop hell IS enabled (systemd re-attempts it forever, by
    # design) but is never actually doing its job; VMS_SERVICE_FILE's
    # own check below already covers both is-enabled AND is-active for
    # exactly this reason, and the agent unit needs the identical
    # coverage, not a narrower one.
    check "anyaicam-agent.service is active" systemctl is-active --quiet anyaicam-agent.service
    check "anyaicam-vms.service is enabled" systemctl is-enabled --quiet anyaicam-vms.service
    check "anyaicam-vms.service is active" systemctl is-active --quiet anyaicam-vms.service
    check "system suspend/hibernate is disabled (appliance must stay online 24/7)" suspend_targets_masked
    log "Waiting up to ${VMS_STARTUP_WAIT_SECONDS}s for the VMS to finish starting before its HTTP checks..."
    check "VMS local health endpoint responds" retry_until_vms_started vms_health_ok
    check "VMS local ready endpoint is reachable and self-test passes (business readiness -- e.g. a camera actually recording -- is intentionally not required at install time)" retry_until_vms_started ready_endpoint_self_test_ok
    check "VMS /version reports exact approved commit" retry_until_vms_started version_reports_release
    check "MediaMTX is present, executable, and checksum-verified when P2P live view is enabled" mediamtx_required_and_usable
    check "WebRTC media port (UDP 8189) is published by the VMS container and by nothing else" webrtc_port_owned_by_vms
    check "WebRTC media port (UDP 8189) is restricted to private/Tailscale sources" "${WEBRTC_FIREWALL_SCRIPT:-/usr/local/sbin/anyaicam-webrtc-firewall}" check
    check "anyaicam-webrtc-firewall.service is enabled" systemctl is-enabled --quiet anyaicam-webrtc-firewall.service

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
