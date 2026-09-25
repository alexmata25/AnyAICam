#!/usr/bin/env bash
# Local, non-destructive, non-root test harness for the installer's
# decision logic: detect_install_state() and storage_preflight().
#
# This sources the REAL production functions from ../03-detect-install.sh,
# ../02-storage-check.sh, and ../06-deploy-vms.sh unmodified -- no
# duplicated/rewritten logic that could drift from what actually ships.
# Everything these functions touch is redirected into a disposable
# tmpdir via the same path constants install.sh already defines
# ($CONFIG_DIR, $VMS_INSTALL_ROOT, $VERSION_MARKER, $VMS_SERVICE_FILE)
# -- nothing under the real /etc, /opt, or /var is read or written.
# `id`, `docker`, and `df` (the external commands these functions call)
# are shadowed with shell functions of the same name so each scenario
# can fake their output without root and without a real Docker/systemd
# present.
#
# Usage: bash installer/tests/run_tests.sh
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_DIR="$(cd "$TESTS_DIR/.." && pwd)"

PASS=0
FAIL=0

# --- fixture root -----------------------------------------------------
FIXTURE_ROOT="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

reset_fixture() {
    ANYAICAM_PRODUCT_MODE=local
    PRODUCT_MODE_LEGACY=false
    rm -rf "$FIXTURE_ROOT"
    mkdir -p "$FIXTURE_ROOT"
    # Redirect every path constant the two functions under test read or
    # write into the disposable fixture root.
    CONFIG_DIR="$FIXTURE_ROOT/etc/anyaicam"
    VMS_INSTALL_ROOT="$FIXTURE_ROOT/opt/anyaicam"
    VMS_SERVICE_FILE="$FIXTURE_ROOT/etc/systemd/system/anyaicam-vms.service"
    VERSION_MARKER="$CONFIG_DIR/installed_version"
    IDENTITY_FILE="$CONFIG_DIR/appliance_identity.json"
    # VMS persistent/runtime state tree -- same fixture-redirection
    # pattern as every other path constant here, so migrate_*() and
    # run_uninstall() never touch the real /var/lib or /etc.
    VMS_RECORDINGS_DIR="$FIXTURE_ROOT/var/lib/anyaicam/vms/recordings"
    VMS_DATA_CONFIG_DIR="$FIXTURE_ROOT/var/lib/anyaicam/vms/data-config"
    VMS_HLS_DIR="$FIXTURE_ROOT/var/lib/anyaicam/vms/hls"
    VMS_ENV_FILE="$CONFIG_DIR/vms.env"
    QUARANTINE_DIR="$VMS_RECORDINGS_DIR/quarantine"
    # Release-driven deploy_vms() constants (installer: make VMS payload
    # release-driven) -- same fixture-redirection pattern as every other
    # path constant here. VMS_RELEASE_COMMIT is a fixed, fake-but-valid
    # 40-char lowercase hex value (the SHA-1 of an empty string) so every
    # test starts from a real, format-valid commit unless it deliberately
    # overrides it.
    VMS_PAYLOAD_DIR="$FIXTURE_ROOT/installer-payload/vms"
    VMS_RELEASE_COMMIT="da39a3ee5e6b4b0d3255bfef95601890afd80709"
    # Release-driven install_agent()/run_uninstall() constants -- same
    # release-driven rewrite as VMS_PAYLOAD_DIR above (installer: make
    # VMS payload release-driven). install_agent() no longer reads
    # $REPO_ROOT/appliance-agent at all; it requires the agent payload
    # at $AGENT_PAYLOAD_DIR and installs a self-contained copy under
    # $AGENT_SOURCE_ROOT (a subdirectory of $AGENT_INSTALL_ROOT).
    AGENT_PAYLOAD_DIR="$FIXTURE_ROOT/installer-payload/agent"
    AGENT_INSTALL_ROOT="$FIXTURE_ROOT/opt/anyaicam-agent"
    AGENT_SOURCE_ROOT="$AGENT_INSTALL_ROOT/source"
    # P2P live-view foundation, Phase 2 -- same release-driven,
    # fixture-redirected pattern as AGENT_PAYLOAD_DIR/AGENT_INSTALL_ROOT
    # above. install_mediamtx() reads MEDIAMTX_PAYLOAD_DIR (optional --
    # a missing payload is a silent no-op, see 10-install-mediamtx.sh's
    # own comment) and installs to MEDIAMTX_INSTALL_DIR/MEDIAMTX_BINARY_PATH.
    MEDIAMTX_PAYLOAD_DIR="$FIXTURE_ROOT/installer-payload/mediamtx"
    MEDIAMTX_INSTALL_DIR="$FIXTURE_ROOT/opt/anyaicam/mediamtx"
    MEDIAMTX_BINARY_PATH="$MEDIAMTX_INSTALL_DIR/mediamtx"
    rm -rf "$MEDIAMTX_PAYLOAD_DIR"
    # MediaMTX packaging-regression guard (2026-09-17, second real
    # occurrence) -- mediamtx_required_and_usable() in validate.sh reads
    # both of these; redirected into the fixture the same way every other
    # path constant here is, so its tests below never touch the real
    # /etc/anyaicam.
    VMS_RELEASE_MARKER="$CONFIG_DIR/vms_release.json"
    MEDIAMTX_INCLUDED="false"
    MEDIAMTX_SHA256=""
    mkdir -p "$(dirname "$VMS_SERVICE_FILE")"
    # id/docker mocks default to "absent" until a test overrides them.
    ID_MOCK_EXIT=1
    DOCKER_IMAGE_MOCK_EXIT=1
    DOCKER_COMPOSE_BUILD_MARKER="$FIXTURE_ROOT/.docker-compose-build-called"
    rm -f "$DOCKER_COMPOSE_BUILD_MARKER"
    export APT_PYTHON_VENV_MARKER="$FIXTURE_ROOT/.apt-python3.12-venv-installed"
    export FAKE_AGENT_INSTALL_MARKER="$FIXTURE_ROOT/.fake-agent-install-ran"
    export FAKE_AGENT_UNINSTALL_MARKER="$FIXTURE_ROOT/.fake-agent-uninstall-ran"
    rm -f "$APT_PYTHON_VENV_MARKER" "$FAKE_AGENT_INSTALL_MARKER" "$FAKE_AGENT_UNINSTALL_MARKER"
    DF_AVAIL_GB=999999
    DF_TOTAL_GB=999999
    SYSTEMCTL_CALL_LOG="$FIXTURE_ROOT/.systemctl-calls"
    rm -f "$SYSTEMCTL_CALL_LOG"
    USERDEL_CALL_LOG="$FIXTURE_ROOT/.userdel-calls"
    rm -f "$USERDEL_CALL_LOG"
}

# A minimal fake built appliance-agent release payload for
# install_agent()/run_uninstall() to install/uninstall from --
# release-driven install_agent() no longer reads
# $REPO_ROOT/appliance-agent at all (see AGENT_PAYLOAD_DIR/
# AGENT_SOURCE_ROOT above); this replaces the old make_fake_repo_root(),
# which built these same fake scripts at a path nothing reads anymore.
make_fake_agent_payload() {
    # A fake stand-in for the reused appliance-agent/scripts/install.sh
    # -- deliberately fails unless the python3.12-venv prerequisite was
    # already installed first, so the test both proves install_agent()
    # installs it AND proves the ordering (prereq before the wrapped
    # script runs), without ever touching a real venv/pip/network.
    mkdir -p "$AGENT_PAYLOAD_DIR/scripts"
    cat > "$AGENT_PAYLOAD_DIR/scripts/install.sh" <<'FAKE_AGENT_INSTALL'
#!/usr/bin/env bash
set -euo pipefail
if [[ ! -f "$APT_PYTHON_VENV_MARKER" ]]; then
    echo "python3.12-venv prerequisite missing -- would fail for real here" >&2
    exit 1
fi
touch "$FAKE_AGENT_INSTALL_MARKER"
FAKE_AGENT_INSTALL
    chmod 755 "$AGENT_PAYLOAD_DIR/scripts/install.sh"
    # A fake stand-in for the reused appliance-agent/scripts/uninstall.sh
    # -- just proves run_uninstall() actually invoked it.
    cat > "$AGENT_PAYLOAD_DIR/scripts/uninstall.sh" <<'FAKE_AGENT_UNINSTALL'
#!/usr/bin/env bash
touch "$FAKE_AGENT_UNINSTALL_MARKER"
FAKE_AGENT_UNINSTALL
    chmod 755 "$AGENT_PAYLOAD_DIR/scripts/uninstall.sh"
}

# A minimal fake built VMS release payload for deploy_vms() to mirror
# from -- release-driven deploy_vms() no longer reads $REPO_ROOT at all
# (install_agent()/run_uninstall() had the exact same $REPO_ROOT-\>
# payload-dir rewrite; see make_fake_agent_payload() above for their
# side of it). Matches build_release_installer.py's
# REQUIRED_RELEASE_PATHS exactly: app, requirements.txt, Dockerfile,
# Dockerfile.production, docker-compose.yml.
make_fake_mediamtx_payload() {
    local content="${1:-fake mediamtx binary}"
    mkdir -p "$MEDIAMTX_PAYLOAD_DIR"
    printf '%s' "$content" > "$MEDIAMTX_PAYLOAD_DIR/mediamtx"
    (cd "$MEDIAMTX_PAYLOAD_DIR" && sha256sum mediamtx > mediamtx.sha256)
}

make_fake_vms_payload() {
    mkdir -p "$VMS_PAYLOAD_DIR/app"
    echo 'print("fake app")' > "$VMS_PAYLOAD_DIR/app/main.py"
    echo 'FROM python:3.12-slim' > "$VMS_PAYLOAD_DIR/Dockerfile"
    echo 'FROM python:3.12-slim AS production' > "$VMS_PAYLOAD_DIR/Dockerfile.production"
    echo 'services: {}' > "$VMS_PAYLOAD_DIR/docker-compose.yml"
    echo 'fastapi' > "$VMS_PAYLOAD_DIR/requirements.txt"
}

# Shadow the three external commands the functions under test call.
# Real /usr/bin/id, docker, df are never invoked by this harness.
id() {
    if [[ "$1" == "-u" && "$2" == "anyaicam" ]]; then
        return "$ID_MOCK_EXIT"
    fi
    command id "$@"
}
docker() {
    if [[ "$1" == "image" && "$2" == "inspect" ]]; then
        return "$DOCKER_IMAGE_MOCK_EXIT"
    fi
    if [[ "$1" == "compose" && "$2" == "build" ]]; then
        # deploy_vms() only needs to know the build step ran; the
        # actual image build is exercised for real in Phase 4 on a
        # genuine Ubuntu host, not here. deploy_vms() invokes this
        # inside a `( cd ... && docker compose build )` subshell, so a
        # plain variable assignment here would not survive back to the
        # caller -- a marker file is used instead.
        touch "$DOCKER_COMPOSE_BUILD_MARKER"
        return 0
    fi
    if [[ "$1" == "compose" && "$2" == "down" ]]; then
        return 0
    fi
    if [[ "$1" == "image" && "$2" == "rm" ]]; then
        return 0
    fi
    command docker "$@"
}
systemctl() {
    # run_uninstall() only needs these to not fail; no real systemd is
    # present or should be touched by this harness. Every invocation is
    # also appended to SYSTEMCTL_CALL_LOG (when set) so a test can assert
    # on exactly what disable_system_suspend() asked systemd to do,
    # without needing a real systemd to actually do it.
    [[ -n "${SYSTEMCTL_CALL_LOG:-}" ]] && printf '%s\n' "$*" >>"$SYSTEMCTL_CALL_LOG"
    return 0
}
apt-get() {
    if [[ "$1" == "update" ]]; then
        return 0
    fi
    if [[ "$1" == "install" ]]; then
        shift
        for arg in "$@"; do
            [[ "$arg" == "python3.12-venv" ]] && touch "$APT_PYTHON_VENV_MARKER"
        done
        return 0
    fi
    command apt-get "$@"
}
userdel() {
    # run_uninstall()'s --purge-all branch only needs this to not fail;
    # no real user should be touched by this harness. Logged so a test
    # can assert it was actually called, without needing a real system
    # user to exist.
    [[ -n "${USERDEL_CALL_LOG:-}" ]] && printf '%s\n' "$*" >>"$USERDEL_CALL_LOG"
    return 0
}
df() {
    # Only the two exact invocations storage_preflight() makes are
    # mocked; anything else falls through to the real df.
    if [[ "$*" == "--output=avail -B1G /" ]]; then
        printf '%s\n%s\n' "Avail" "$DF_AVAIL_GB"
        return 0
    fi
    if [[ "$*" == "--output=size -B1G /" ]]; then
        printf '%s\n%s\n' "Size" "$DF_TOTAL_GB"
        return 0
    fi
    command df "$@"
}

log() { :; } # silence log() output during tests; assertions do the talking

# shellcheck source=../03-detect-install.sh
source "$INSTALLER_DIR/03-detect-install.sh"
# shellcheck source=../02-storage-check.sh
source "$INSTALLER_DIR/02-storage-check.sh"
# shellcheck source=../06-deploy-vms.sh
source "$INSTALLER_DIR/06-deploy-vms.sh"
# shellcheck source=../07-install-agent.sh
source "$INSTALLER_DIR/07-install-agent.sh"
# shellcheck source=../10-install-mediamtx.sh
source "$INSTALLER_DIR/10-install-mediamtx.sh"
# shellcheck source=../uninstall.sh
# Sourcing this pulls in its own `source install.sh` internally, which
# re-defines the real-path constants (harmless -- every test below
# calls reset_fixture() afterward, which always re-applies the fixture
# overrides last) but ALSO re-runs install.sh's own `set -euo pipefail`
# in this shell, since `source` never sandboxes `set` options. Left
# alone, that silently turns on errexit for the rest of this script,
# so a single non-fatal command failure inside e.g. deploy_vms()
# (expected here -- this harness runs non-root, so chown/install -o
# calls routinely "fail" and are meant to be tolerated, same as
# elsewhere in this suite) would abort the whole test run instead of
# just that one command. `set -uo pipefail` alone would NOT undo this
# -- it only adds options, it never clears -e -- so errexit must be
# turned off explicitly before restoring this harness's own options.
source "$INSTALLER_DIR/uninstall.sh"
set +e
set -uo pipefail

# shellcheck source=../validate.sh
# Same re-source/set -e caveat as uninstall.sh above -- validate.sh's
# own actual check-running (run_validate()) is guarded behind a
# BASH_SOURCE-is-the-entrypoint check, so sourcing it here only defines
# its functions (ready_endpoint_self_test_ok() is what this harness
# actually exercises below); it never runs a real validation pass
# against this machine.
source "$INSTALLER_DIR/validate.sh"
set +e
set -uo pipefail

assert_eq() {
    local description="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (expected '$expected', got '$actual')"
        FAIL=$((FAIL + 1))
    fi
}

assert_exit() {
    local description="$1" expected_exit="$2"; shift 2
    local actual_exit=0
    ( "$@" ) >/dev/null 2>&1 || actual_exit=$?
    assert_eq "$description" "$expected_exit" "$actual_exit"
}

echo "== detect_install_state() =="

# 1. Nothing present -> clean
reset_fixture
detect_install_state
assert_eq "no markers present -> clean" "clean" "$INSTALL_STATE"

# 2. All 5 markers present -> existing
reset_fixture
mkdir -p "$CONFIG_DIR"
echo "1.0.0" > "$VERSION_MARKER"
ID_MOCK_EXIT=0
mkdir -p "$VMS_INSTALL_ROOT"
touch "$VMS_INSTALL_ROOT/docker-compose.yml"
DOCKER_IMAGE_MOCK_EXIT=0
detect_install_state
assert_eq "all 5 markers present -> existing" "existing" "$INSTALL_STATE"

# 3. Some but not all markers -> partial (never silently clean)
reset_fixture
mkdir -p "$CONFIG_DIR"
echo "1.0.0" > "$VERSION_MARKER"
# anyaicam user absent, no vms service/compose file, no docker image
detect_install_state
assert_eq "2/5 markers present -> partial, not clean" "partial" "$INSTALL_STATE"

# 4. Exactly one marker present (the systemd/compose OR-branch alone) -> partial
reset_fixture
mkdir -p "$VMS_INSTALL_ROOT"
touch "$VMS_INSTALL_ROOT/docker-compose.yml"
detect_install_state
assert_eq "1/5 markers present (compose file only) -> partial, not clean" "partial" "$INSTALL_STATE"

# 5. 4/5 markers present (docker image missing) -> partial, never "existing"
reset_fixture
mkdir -p "$CONFIG_DIR"
echo "1.0.0" > "$VERSION_MARKER"
ID_MOCK_EXIT=0
mkdir -p "$VMS_INSTALL_ROOT"
touch "$VMS_INSTALL_ROOT/docker-compose.yml"
# DOCKER_IMAGE_MOCK_EXIT left at default (1) = image absent
detect_install_state
assert_eq "4/5 markers present -> partial, never existing" "partial" "$INSTALL_STATE"

echo
echo "== storage_preflight() =="

# 6. Clean install, plenty of free space -> passes and records baseline
reset_fixture
DF_AVAIL_GB=150
DF_TOTAL_GB=500
assert_exit "clean install, 150GB free (>=100GB) -> passes" 0 storage_preflight clean
reset_fixture
DF_AVAIL_GB=150
DF_TOTAL_GB=500
storage_preflight clean >/dev/null 2>&1
assert_eq "clean install baseline recorded to disk_capacity_gb" "500" "$(cat "$CONFIG_DIR/disk_capacity_gb" 2>/dev/null)"

# 7. Clean install, insufficient free space -> fails closed (this is the
#    strict 100GB clean-install requirement; must never be relaxed)
reset_fixture
DF_AVAIL_GB=99
DF_TOTAL_GB=500
assert_exit "clean install, 99GB free (<100GB) -> fails" 1 storage_preflight clean

# 8. Existing install, the exact reported release blocker: ~98GB free
#    used to fail against the clean-install 100GB minimum. Must now pass
#    against the much smaller existing-install working-space threshold.
reset_fixture
DF_AVAIL_GB=98
DF_TOTAL_GB=500
assert_exit "existing install, 98GB free (>=15GB working space) -> passes (regression check for the known release blocker)" 0 storage_preflight existing

# 9. Existing install, below the 15GB working-space minimum -> fails closed
reset_fixture
DF_AVAIL_GB=10
DF_TOTAL_GB=500
assert_exit "existing install, 10GB free (<15GB working space) -> fails" 1 storage_preflight existing

# 10. Repair (partial), same working-space threshold as existing
reset_fixture
DF_AVAIL_GB=20
DF_TOTAL_GB=500
assert_exit "partial/repair, 20GB free (>=15GB working space) -> passes" 0 storage_preflight partial

# 11. Existing install, total capacity has NOT shrunk below the 90% floor -> passes
reset_fixture
mkdir -p "$CONFIG_DIR"
echo "500" > "$CONFIG_DIR/disk_capacity_gb"
DF_AVAIL_GB=50
DF_TOTAL_GB=460   # 92% of recorded 500GB baseline
assert_exit "existing install, total capacity at 92% of recorded baseline -> passes" 0 storage_preflight existing

# 12. Existing install, total capacity HAS shrunk below the 90% floor ->
#     fails closed (guards against a shrunk/misattached disk)
reset_fixture
mkdir -p "$CONFIG_DIR"
echo "500" > "$CONFIG_DIR/disk_capacity_gb"
DF_AVAIL_GB=50
DF_TOTAL_GB=400   # 80% of recorded 500GB baseline
assert_exit "existing install, total capacity at 80% of recorded baseline (<90% floor) -> fails closed" 1 storage_preflight existing

# 13. Existing install, no recorded baseline yet (e.g. upgrading from a
#     pre-baseline install) -> capacity-floor check is skipped, only the
#     working-space threshold applies
reset_fixture
DF_AVAIL_GB=50
DF_TOTAL_GB=200
assert_exit "existing install, no recorded baseline -> capacity-floor check skipped, passes on working space alone" 0 storage_preflight existing

echo
echo "== deploy_vms() =="

# 14-16. Regression tests for the two clean-install release blockers
#     found in live Phase 4 validation: (1) docker-compose.yml's
#     `build: .` resolves to a file literally named Dockerfile, but the
#     original deploy_vms() only copied Dockerfile.production, leaving
#     nothing for `docker compose build` to read on a genuinely fresh
#     install; (2) the plain Dockerfile does
#     `COPY requirements.txt /tmp/requirements.txt`, and that file is
#     tracked only at repo root, not under app/. All of Dockerfile,
#     Dockerfile.production, and requirements.txt must land in
#     VMS_INSTALL_ROOT -- every build-context path the plain Dockerfile
#     actually references (confirmed by inspecting its content: exactly
#     requirements.txt and ./app beyond itself).
reset_fixture
make_fake_vms_payload
deploy_vms clean >/dev/null 2>&1
assert_exit "plain Dockerfile is copied into VMS_INSTALL_ROOT" 0 test -f "$VMS_INSTALL_ROOT/Dockerfile"
assert_exit "Dockerfile.production is also copied into VMS_INSTALL_ROOT" 0 test -f "$VMS_INSTALL_ROOT/Dockerfile.production"
assert_exit "requirements.txt is copied into VMS_INSTALL_ROOT" 0 test -f "$VMS_INSTALL_ROOT/requirements.txt"
assert_exit "docker compose build was invoked" 0 test -f "$DOCKER_COMPOSE_BUILD_MARKER"

# 14a. Missing release payload -> deploy_vms fails closed, never silently
#      proceeds to build/deploy a nonexistent app, and never touches
#      VMS_INSTALL_ROOT at all.
reset_fixture
assert_exit "deploy_vms fails when the built VMS payload is missing" 1 deploy_vms clean
assert_exit "VMS_INSTALL_ROOT is never created when the payload is missing" 1 test -d "$VMS_INSTALL_ROOT"

# 14b. Exact payload deployment (--delete mirror): a stale file left
#      over in VMS_INSTALL_ROOT from a previous release, but no longer
#      part of the current payload, must be removed -- this is the
#      whole point of switching from `rsync --update` (additive,
#      never removes) to `rsync --delete` (exact software mirror).
#      Persistent state that was never part of any payload (recordings,
#      data/config, .env) must survive untouched regardless.
reset_fixture
make_fake_vms_payload
mkdir -p "$VMS_INSTALL_ROOT"
echo "leftover from a previous release" > "$VMS_INSTALL_ROOT/stale-removed-file.py"
mkdir -p "$VMS_RECORDINGS_DIR" "$VMS_DATA_CONFIG_DIR"
echo "real-recording-data" > "$VMS_RECORDINGS_DIR/clip1.mp4"
echo "real-data-config" > "$VMS_DATA_CONFIG_DIR/settings.json"
deploy_vms clean >/dev/null 2>&1
assert_exit "a file no longer in the release payload is removed from VMS_INSTALL_ROOT (exact mirror, not additive)" 1 test -f "$VMS_INSTALL_ROOT/stale-removed-file.py"
assert_eq "persistent recordings untouched by the exact-mirror deploy" "real-recording-data" "$(cat "$VMS_RECORDINGS_DIR/clip1.mp4" 2>/dev/null)"
assert_eq "persistent data-config untouched by the exact-mirror deploy" "real-data-config" "$(cat "$VMS_DATA_CONFIG_DIR/settings.json" 2>/dev/null)"

# 14n. Real bug confirmed live on Ryzen (2026-09-17): a previously-
#      installed, already-validated MediaMTX binary at
#      VMS_INSTALL_ROOT/mediamtx sits directly inside the tree this same
#      rsync --delete mirrors -- an ordinary VMS-only repair (this
#      session's own LPR/PPE fix), built with no --mediamtx-binary
#      payload at all, silently deleted it, because 'mediamtx/' was not
#      in this rsync's own exclude list even though 10-install-
#      mediamtx.sh's own docstring explicitly promises this exact
#      scenario "changes nothing about live camera behavior". This
#      broke a P2P-enabled appliance's live view with no error anywhere
#      in the install/repair output -- deploy_vms() runs BEFORE
#      install_mediamtx() in install.sh's own pipeline, and
#      install_mediamtx() itself correctly no-ops when no payload is
#      present, so nothing downstream ever got a chance to notice or
#      restore what this rsync had already removed.
reset_fixture
make_fake_vms_payload
mkdir -p "$MEDIAMTX_INSTALL_DIR"
printf 'real previously-installed mediamtx binary' > "$MEDIAMTX_BINARY_PATH"
deploy_vms repair >/dev/null 2>&1
assert_eq "a previously-installed MediaMTX binary survives an ordinary VMS-only repair deploy untouched" "real previously-installed mediamtx binary" "$(cat "$MEDIAMTX_BINARY_PATH" 2>/dev/null)"

echo
echo "== ensure_vms_env() / build-identity stamping =="

# 14c. Canonical ANYAICAM_ENV (not ANYAICAM_ENVIRONMENT) is written on a
#      fresh env file -- app/main.py and app/cloud_config.py only ever
#      read ANYAICAM_ENV; a previous version of this installer wrote
#      ANYAICAM_ENVIRONMENT here, a name the app never reads, so every
#      appliance installed that way silently stayed on the "local"
#      default forever regardless of this line having run.
reset_fixture
ensure_vms_env >/dev/null 2>&1
assert_eq "ANYAICAM_ENV=production is written to a fresh env file" "1" "$(grep -c '^ANYAICAM_ENV=production$' "$VMS_ENV_FILE" 2>/dev/null)"
assert_eq "the old, never-read ANYAICAM_ENVIRONMENT key is never written" "0" "$(grep -c '^ANYAICAM_ENVIRONMENT=' "$VMS_ENV_FILE" 2>/dev/null)"

# 14d. An operator-customized ANYAICAM_ENV (e.g. staging) is never
#      overwritten by a reinstall/repair -- same never-clobber contract
#      as ANYAICAM_RUNTIME_ROLE already has.
reset_fixture
mkdir -p "$CONFIG_DIR"
printf 'ANYAICAM_ENV=staging\n' > "$VMS_ENV_FILE"
ensure_vms_env >/dev/null 2>&1
assert_eq "an existing, customized ANYAICAM_ENV value is preserved across reinstall" "ANYAICAM_ENV=staging" "$(grep '^ANYAICAM_ENV=' "$VMS_ENV_FILE" 2>/dev/null)"

# 14d2. Live View staging transport (2026-09-13): ANYAICAM_LIVE_RELAY_ENABLED
#      defaults to false on a fresh env file -- the S3/CloudFront relay
#      worker must never attempt to run on a brand-new appliance that has
#      no AWS configuration and is not part of the pilot -- and, like
#      ANYAICAM_ENV/RUNTIME_ROLE above, an operator-flipped true is never
#      clobbered back to false by a later reinstall/repair.
reset_fixture
ensure_vms_env >/dev/null 2>&1
assert_eq "ANYAICAM_LIVE_RELAY_ENABLED=false is written to a fresh env file" "1" "$(grep -c '^ANYAICAM_LIVE_RELAY_ENABLED=false$' "$VMS_ENV_FILE" 2>/dev/null)"

reset_fixture
mkdir -p "$CONFIG_DIR"
printf 'ANYAICAM_LIVE_RELAY_ENABLED=true\n' > "$VMS_ENV_FILE"
ensure_vms_env >/dev/null 2>&1
assert_eq "an operator-enabled ANYAICAM_LIVE_RELAY_ENABLED=true is preserved across reinstall" "ANYAICAM_LIVE_RELAY_ENABLED=true" "$(grep '^ANYAICAM_LIVE_RELAY_ENABLED=' "$VMS_ENV_FILE" 2>/dev/null)"
assert_eq "exactly one ANYAICAM_LIVE_RELAY_ENABLED line exists (never duplicated)" "1" "$(grep -c '^ANYAICAM_LIVE_RELAY_ENABLED=' "$VMS_ENV_FILE" 2>/dev/null)"

# 14e/14f. ANYAICAM_BUILD_ID and ANYAICAM_VMS_COMMIT are installer-owned
#      build identity -- unlike ANYAICAM_ENV/RUNTIME_ROLE, these two ARE
#      meant to be refreshed on every reinstall/repair (a repair with a
#      newer release commit must update the stamped identity, not keep
#      pointing at the old one).
reset_fixture
mkdir -p "$CONFIG_DIR"
printf 'ANYAICAM_BUILD_ID=stale-previous-release\nANYAICAM_VMS_COMMIT=stale-previous-release\n' > "$VMS_ENV_FILE"
VMS_RELEASE_COMMIT="1111111111111111111111111111111111111a"
ensure_vms_env >/dev/null 2>&1
assert_eq "ANYAICAM_BUILD_ID is refreshed to the current release commit, not left stale" "ANYAICAM_BUILD_ID=1111111111111111111111111111111111111a" "$(grep '^ANYAICAM_BUILD_ID=' "$VMS_ENV_FILE" 2>/dev/null)"
assert_eq "ANYAICAM_VMS_COMMIT is refreshed to the current release commit, not left stale" "ANYAICAM_VMS_COMMIT=1111111111111111111111111111111111111a" "$(grep '^ANYAICAM_VMS_COMMIT=' "$VMS_ENV_FILE" 2>/dev/null)"
assert_eq "exactly one ANYAICAM_BUILD_ID line exists (upsert, not append-a-duplicate)" "1" "$(grep -c '^ANYAICAM_BUILD_ID=' "$VMS_ENV_FILE" 2>/dev/null)"

# 14g. ANYAICAM_APP_SECRETS is generated on a fresh env file: real
#      release blocker -- cloud_config.py's edge_production profile
#      still requires a strong, non-default secret (by design), but
#      nothing ever provisioned one for a fresh edge install before
#      this fix, so every from-scratch appliance would otherwise hit
#      that check and refuse to start. At least 32 bytes of entropy
#      (64 lowercase hex characters from /dev/urandom) -- never derived
#      from the appliance ID, hostname, MAC, or anything else
#      predictable.
reset_fixture
ensure_vms_env >/dev/null 2>&1
GENERATED_SECRET="$(grep '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE" | cut -d= -f2)"
assert_eq "a fresh env file gets a 64-hex-character (32-byte) ANYAICAM_APP_SECRETS" "64" "${#GENERATED_SECRET}"
assert_exit "the generated secret is lowercase hex only" 0 bash -c "[[ '$GENERATED_SECRET' =~ ^[0-9a-f]{64}\$ ]]"
assert_eq "exactly one ANYAICAM_APP_SECRETS line exists" "1" "$(grep -c '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE" 2>/dev/null)"

# 14h. Repair/reinstall preserves the exact same secret -- unlike
#      ANYAICAM_BUILD_ID/ANYAICAM_VMS_COMMIT above, this must NEVER be
#      regenerated on a second run: rotating it silently on every
#      reinstall would instantly invalidate every existing signed
#      session/cookie.
ensure_vms_env >/dev/null 2>&1
SECOND_RUN_SECRET="$(grep '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE" | cut -d= -f2)"
assert_eq "ANYAICAM_APP_SECRETS is unchanged across a second ensure_vms_env() run (repair/reinstall)" "$GENERATED_SECRET" "$SECOND_RUN_SECRET"

# 14i. A pre-existing, customer-set secret is never overwritten.
reset_fixture
mkdir -p "$CONFIG_DIR"
printf 'ANYAICAM_APP_SECRETS=my-existing-custom-secret-value-1234567890\n' > "$VMS_ENV_FILE"
ensure_vms_env >/dev/null 2>&1
assert_eq "a pre-existing ANYAICAM_APP_SECRETS value is never overwritten" "ANYAICAM_APP_SECRETS=my-existing-custom-secret-value-1234567890" "$(grep '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE")"
assert_eq "exactly one ANYAICAM_APP_SECRETS line exists (never appended as a duplicate)" "1" "$(grep -c '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE")"

echo
echo "== ensure_vms_env() / camera credential encryption key =="

# 14j. ANYAICAM_CAMERA_CREDENTIAL_KEY is generated on a fresh env file:
#      confirmed live on a real edge appliance -- nothing ever
#      provisioned this key, so any attempt to add a discovered camera
#      WITH ONVIF/RTSP credentials failed closed with a 503
#      (app/appliance_protocol.py's encrypt_camera_credentials() has no
#      key to encrypt with). Fixed the same way ANYAICAM_APP_SECRETS
#      already is, one section up. Deliberately never asserts the raw
#      generated value in any pass/fail message below (assert_eq prints
#      both sides on a mismatch) -- only its length, its format via a
#      regex exit code, and content-free line counts, and hashes rather
#      than raw values wherever two runs must be compared, so this
#      suite can never leak real key material into test output.
reset_fixture
ensure_vms_env >/dev/null 2>&1
GENERATED_KEY="$(grep '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE" | cut -d= -f2-)"
assert_eq "a fresh env file gets a 44-character ANYAICAM_CAMERA_CREDENTIAL_KEY (32 raw bytes, base64-encoded)" "44" "${#GENERATED_KEY}"
assert_exit "the generated key is URL-safe base64 (Fernet's required format)" 0 bash -c "[[ '$GENERATED_KEY' =~ ^[A-Za-z0-9_-]{43}=\$ ]]"
assert_eq "exactly one ANYAICAM_CAMERA_CREDENTIAL_KEY line exists" "1" "$(grep -c '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE" 2>/dev/null)"

# 14k. Repair/reinstall preserves the exact same key -- never
#      regenerated on a second run: rotating it silently would make
#      every already-stored camera credential permanently
#      undecryptable, breaking every already-working camera stream.
#      Compared by hash, never by raw value.
GENERATED_KEY_HASH="$(printf '%s' "$GENERATED_KEY" | sha256sum | cut -d' ' -f1)"
ensure_vms_env >/dev/null 2>&1
SECOND_RUN_KEY="$(grep '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE" | cut -d= -f2-)"
SECOND_RUN_KEY_HASH="$(printf '%s' "$SECOND_RUN_KEY" | sha256sum | cut -d' ' -f1)"
assert_eq "ANYAICAM_CAMERA_CREDENTIAL_KEY is unchanged across a second ensure_vms_env() run (repair/reinstall)" "$GENERATED_KEY_HASH" "$SECOND_RUN_KEY_HASH"

# 14l. A pre-existing key (e.g. carried over from a prior install, or
#      set by an operator) is never overwritten -- an upgrade must
#      backfill a MISSING key, never touch one that's already there.
reset_fixture
mkdir -p "$CONFIG_DIR"
EXISTING_KEY="a-previously-generated-fernet-style-key-value=="
printf 'ANYAICAM_CAMERA_CREDENTIAL_KEY=%s\n' "$EXISTING_KEY" > "$VMS_ENV_FILE"
# Hashed via the exact same grep|cut extraction both before and after --
# not "$EXISTING_KEY" itself -- so a trailing-newline difference between
# how the shell variable and the file line are read can never produce a
# false-positive mismatch here.
BEFORE_KEY_HASH="$(grep '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE" | cut -d= -f2- | sha256sum | cut -d' ' -f1)"
ensure_vms_env >/dev/null 2>&1
AFTER_KEY_HASH="$(grep '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE" | cut -d= -f2- | sha256sum | cut -d' ' -f1)"
assert_eq "a pre-existing ANYAICAM_CAMERA_CREDENTIAL_KEY value is never overwritten" "$BEFORE_KEY_HASH" "$AFTER_KEY_HASH"
assert_eq "exactly one ANYAICAM_CAMERA_CREDENTIAL_KEY line exists (never appended as a duplicate)" "1" "$(grep -c '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE")"

# 14m. Upgrade of an existing install missing only this key backfills
#      it safely, without disturbing any other already-configured value
#      in the same file (the exact "existing install, key introduced by
#      a later installer version" scenario).
reset_fixture
mkdir -p "$CONFIG_DIR"
printf 'ANYAICAM_ENV=production\nANYAICAM_APP_SECRETS=already-set-app-secret\n' > "$VMS_ENV_FILE"
ensure_vms_env >/dev/null 2>&1
assert_eq "an existing install missing the key gets it backfilled" "1" "$(grep -c '^ANYAICAM_CAMERA_CREDENTIAL_KEY=.' "$VMS_ENV_FILE" 2>/dev/null)"
assert_eq "backfilling the key does not disturb an already-configured, unrelated value" "ANYAICAM_APP_SECRETS=already-set-app-secret" "$(grep '^ANYAICAM_APP_SECRETS=' "$VMS_ENV_FILE")"

echo
echo "== install_agent() =="

# 17. Regression test for the fourth clean-install release blocker
#     found in live Phase 4 validation: `python3 -m venv` inside the
#     reused appliance-agent/scripts/install.sh fails on a fresh Ubuntu
#     24.04 host because python3.12-venv (which provides ensurepip)
#     isn't installed by default. install_agent() must apt-get install
#     it BEFORE invoking the wrapped script -- the fake wrapped script
#     in make_fake_agent_payload() itself fails unless that ordering
#     held, so this proves both "installed" and "installed first" in
#     one assertion.
reset_fixture
make_fake_agent_payload
assert_exit "install_agent succeeds (prereq installed before the wrapped script ran)" 0 install_agent clean
assert_exit "python3.12-venv was apt-get installed" 0 test -f "$APT_PYTHON_VENV_MARKER"
assert_exit "the wrapped appliance-agent install.sh ran" 0 test -f "$FAKE_AGENT_INSTALL_MARKER"

echo
echo "== install_mediamtx() =="

# P2P live-view foundation, Phase 2 (2026-09-17): MediaMTX embedding is
# opt-in at build time (build_release_installer.py's --mediamtx-binary),
# not a hard requirement of every release -- an ordinary VMS-only rebuild
# that never included MediaMTX must keep installing/repairing exactly as
# it always did, never fail because a P2P-specific payload is absent.
reset_fixture
assert_exit "no MediaMTX payload in this release -- silent no-op, not a failure" 0 install_mediamtx clean
assert_exit "nothing gets installed when there is no payload" 1 test -f "$MEDIAMTX_BINARY_PATH"

# A present-but-corrupt payload (checksum mismatch) is a real build/
# transfer problem, and MUST fail loudly -- never install a binary that
# doesn't match the release manifest.
reset_fixture
make_fake_mediamtx_payload
echo "corrupted after the checksum was written" >> "$MEDIAMTX_PAYLOAD_DIR/mediamtx"
assert_exit "a payload that fails its own checksum is refused" 1 install_mediamtx clean
assert_exit "nothing gets installed from a checksum-failed payload" 1 test -f "$MEDIAMTX_BINARY_PATH"

# The real, correct case: a present, checksum-valid payload is installed
# byte-identical and made executable -- but never started, and nothing
# about ANYAICAM_LIVE_P2P_ENABLED or the running VMS service is touched
# by this function at all (it doesn't reference either).
reset_fixture
make_fake_mediamtx_payload "real fake mediamtx contents for this test"
assert_exit "a valid payload installs successfully" 0 install_mediamtx clean
assert_exit "the binary is installed at the expected path" 0 test -f "$MEDIAMTX_BINARY_PATH"
assert_eq "installed binary content is byte-identical to the payload" "$(cat "$MEDIAMTX_PAYLOAD_DIR/mediamtx")" "$(cat "$MEDIAMTX_BINARY_PATH")"
# Not asserted here: test -x on the installed binary. This Windows/MSYS
# test harness's filesystem does not honor chmod's execute bit at all
# (confirmed directly: chmod 0755 on a plain file here leaves it
# -rw-r--r--, and test -x reports false, regardless of what install -m
# 0755 was actually given) -- a real Linux target (where this installer
# actually runs) does not have this limitation. install -m 0755's own
# mode argument is not conditional on platform, so this is a test-
# environment gap, not something this script can compensate for.

# Repair-safe/idempotent: re-running against an already-installed,
# matching binary must succeed without re-copying (and without ever
# needing to touch a running MediaMTX process, since this function never
# starts one in the first place).
BEFORE_MTIME="$(stat -c %Y "$MEDIAMTX_BINARY_PATH" 2>/dev/null || stat -f %m "$MEDIAMTX_BINARY_PATH")"
sleep 1
assert_exit "re-running install_mediamtx (repair) on an already-current binary succeeds" 0 install_mediamtx repair
AFTER_MTIME="$(stat -c %Y "$MEDIAMTX_BINARY_PATH" 2>/dev/null || stat -f %m "$MEDIAMTX_BINARY_PATH")"
assert_eq "an already-current binary is left untouched, not re-copied" "$BEFORE_MTIME" "$AFTER_MTIME"

# 17. Regression test for a real bug found in live P2P verification on
#     Ryzen (2026-09-17): install_mediamtx() places the binary on the
#     HOST at MEDIAMTX_INSTALL_DIR (/opt/anyaicam/mediamtx), but the VMS
#     process that actually needs to spawn it (webrtc_publisher.py, via
#     subprocess.Popen(MEDIAMTX_BINARY, ...)) runs INSIDE the vms
#     container -- and nothing had ever mounted that host directory into
#     the container. Confirmed live: ANYAICAM_LIVE_P2P_ENABLED=true, the
#     binary correctly installed and checksum-verified on the host, and
#     webrtc_publisher_state still went straight to 'spawn_failed'
#     (FileNotFoundError) because /opt/anyaicam/mediamtx simply didn't
#     exist inside the container. install_mediamtx()'s own unit tests
#     above only ever exercised the host-side copy/checksum step in
#     isolation and could never have caught this -- this test instead
#     checks the actual repo docker-compose.yml (the exact file every
#     release payload ships and 06-deploy-vms.sh installs verbatim) for
#     a volume mount making MEDIAMTX_INSTALL_DIR reachable inside the
#     container at the same path, so this exact gap can never silently
#     reappear.
echo
echo "== docker-compose.yml / MediaMTX mount consistency =="
# The real, unmodified constant -- not $MEDIAMTX_INSTALL_DIR, which the
# fixture setup above (re)pointed at $FIXTURE_ROOT/opt/anyaicam/mediamtx
# for sandboxing install_mediamtx() itself; that shadowing must never
# leak into this check, which needs the actual host path 10-install-
# mediamtx.sh installs to on a real appliance.
REAL_MEDIAMTX_INSTALL_DIR="/opt/anyaicam/mediamtx"
REPO_COMPOSE_FILE="$INSTALLER_DIR/../docker-compose.yml"
assert_exit "repo docker-compose.yml exists" 0 test -f "$REPO_COMPOSE_FILE"
assert_exit "docker-compose.yml mounts the real MediaMTX install dir into the container (so webrtc_publisher.py's subprocess.Popen can actually find the binary it verified was installed)" \
    0 grep -qF "$REAL_MEDIAMTX_INSTALL_DIR:$REAL_MEDIAMTX_INSTALL_DIR" "$REPO_COMPOSE_FILE"

echo
echo "== mediamtx_required_and_usable() (installer/validate.sh) =="
# MediaMTX packaging regression, permanent guard (2026-09-17, second real
# occurrence -- see docs/PROJECT_CHECKPOINT.md). Before this, validate.sh
# had zero awareness of MediaMTX at all, so a release built without it
# (--mediamtx-binary simply forgotten on the build command) could report
# "0 failures" on an appliance where P2P live view was completely broken.

# 17a. P2P not enabled at all -- correctly a no-op pass regardless of
#      whether MediaMTX exists, matching every other P2P-gated behavior
#      in this codebase (10-install-mediamtx.sh's own docstring).
reset_fixture
mkdir -p "$CONFIG_DIR"
: > "$VMS_ENV_FILE"
assert_exit "P2P disabled (no vms.env key at all) -> pass regardless of MediaMTX" 0 mediamtx_required_and_usable

reset_fixture
mkdir -p "$CONFIG_DIR"
echo "ANYAICAM_LIVE_P2P_ENABLED=false" > "$VMS_ENV_FILE"
assert_exit "P2P explicitly disabled -> pass regardless of MediaMTX" 0 mediamtx_required_and_usable

# 17b. The real regression this guards against: P2P enabled, but the
#      binary is genuinely missing (a release built without
#      --mediamtx-binary, or an install that never got one).
reset_fixture
mkdir -p "$CONFIG_DIR"
echo "ANYAICAM_LIVE_P2P_ENABLED=true" > "$VMS_ENV_FILE"
assert_exit "P2P enabled + MediaMTX binary missing -> FAIL (the real 2026-09-17 regression)" 1 mediamtx_required_and_usable

# 17c. P2P enabled, binary present, but this release's own recorded
#      checksum (persisted into VMS_RELEASE_MARKER by stamp_release())
#      does not match -- a corrupt/wrong/tampered binary, not merely a
#      missing one.
reset_fixture
mkdir -p "$CONFIG_DIR" "$MEDIAMTX_INSTALL_DIR"
echo "ANYAICAM_LIVE_P2P_ENABLED=true" > "$VMS_ENV_FILE"
printf 'not the real binary' > "$MEDIAMTX_BINARY_PATH"
cat > "$VMS_RELEASE_MARKER" <<'EOF'
{
  "vms_release_commit": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
  "mediamtx_included": "true",
  "mediamtx_sha256": "0000000000000000000000000000000000000000000000000000000000000"
}
EOF
assert_exit "P2P enabled + MediaMTX present but checksum mismatches this release's recorded hash -> FAIL" 1 mediamtx_required_and_usable

# 17d. P2P enabled, binary present, checksum matches this release's own
#      recorded hash exactly -- the genuine, correct, working case.
#      (The executable-bit half of mediamtx_required_and_usable() is
#      deliberately not exercised here -- this Windows/MSYS test harness
#      does not honor chmod's execute bit at all, the identical,
#      already-documented platform gap install_mediamtx()'s own tests
#      above work around; confirmed real Linux targets do not have this
#      limitation.)
reset_fixture
mkdir -p "$CONFIG_DIR" "$MEDIAMTX_INSTALL_DIR"
echo "ANYAICAM_LIVE_P2P_ENABLED=true" > "$VMS_ENV_FILE"
printf 'the real mediamtx binary contents' > "$MEDIAMTX_BINARY_PATH"
REAL_SHA="$(sha256sum "$MEDIAMTX_BINARY_PATH" | cut -d' ' -f1)"
cat > "$VMS_RELEASE_MARKER" <<EOF
{
  "vms_release_commit": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
  "mediamtx_included": "true",
  "mediamtx_sha256": "$REAL_SHA"
}
EOF
chmod 755 "$MEDIAMTX_BINARY_PATH" 2>/dev/null || true
if [[ -x "$MEDIAMTX_BINARY_PATH" ]]; then
    assert_exit "P2P enabled + MediaMTX present + checksum matches -> pass" 0 mediamtx_required_and_usable
else
    echo "SKIP: P2P enabled + MediaMTX present + checksum matches -> pass (this platform's filesystem does not honor the execute bit; not testable here, see install_mediamtx()'s own identical documented gap above)"
fi

# 17e. P2P enabled, binary present and checksum-correct, but this
#      release did NOT embed MediaMTX at all (--no-mediamtx, an
#      ordinary VMS-only rebuild) -- nothing to cross-check against, so
#      only presence/executable matter; a prior release's still-present
#      binary (protected from deletion by 06-deploy-vms.sh's rsync
#      --exclude 'mediamtx/') must not be flagged just because this
#      release's own manifest has no recorded hash.
reset_fixture
mkdir -p "$CONFIG_DIR" "$MEDIAMTX_INSTALL_DIR"
echo "ANYAICAM_LIVE_P2P_ENABLED=true" > "$VMS_ENV_FILE"
printf 'a prior releases still-installed binary' > "$MEDIAMTX_BINARY_PATH"
cat > "$VMS_RELEASE_MARKER" <<'EOF'
{
  "vms_release_commit": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
  "mediamtx_included": "false",
  "mediamtx_sha256": ""
}
EOF
chmod 755 "$MEDIAMTX_BINARY_PATH" 2>/dev/null || true
if [[ -x "$MEDIAMTX_BINARY_PATH" ]]; then
    assert_exit "P2P enabled + this release has no recorded checksum (--no-mediamtx) + a prior binary is present -> pass, nothing to cross-check" 0 mediamtx_required_and_usable
else
    echo "SKIP: --no-mediamtx-release prior-binary pass case (execute bit not testable on this platform, see above)"
fi

echo
echo "== migrate_legacy_persistent_data() / migrate_legacy_persistent_file() =="

# 18. Old-layout -> new-layout migration preserves hashes: a legacy
#     directory with real content, migrated into a not-yet-existing
#     destination, must land byte-identical and the legacy copy must
#     be removed only after that's verified.
reset_fixture
OLD_DIR="$FIXTURE_ROOT/legacy/recordings"
NEW_DIR="$FIXTURE_ROOT/persistent/recordings"
mkdir -p "$OLD_DIR/camera1"
echo "recording-a" > "$OLD_DIR/camera1/clip1.mp4"
echo '{"sentinel":true}' > "$OLD_DIR/partner_portal.db"
HASH_BEFORE_1="$(sha256sum "$OLD_DIR/camera1/clip1.mp4" | cut -d' ' -f1)"
HASH_BEFORE_2="$(sha256sum "$OLD_DIR/partner_portal.db" | cut -d' ' -f1)"
migrate_legacy_persistent_data "$OLD_DIR" "$NEW_DIR" "test data" >/dev/null 2>&1
assert_exit "migrated file exists at new location" 0 test -f "$NEW_DIR/camera1/clip1.mp4"
assert_eq "migrated file content preserved exactly (hash match)" "$HASH_BEFORE_1" "$(sha256sum "$NEW_DIR/camera1/clip1.mp4" 2>/dev/null | cut -d' ' -f1)"
assert_eq "migrated db file content preserved exactly (hash match)" "$HASH_BEFORE_2" "$(sha256sum "$NEW_DIR/partner_portal.db" 2>/dev/null | cut -d' ' -f1)"
assert_exit "legacy directory removed after verified migration" 1 test -d "$OLD_DIR"

# 19. Never overwrites existing persistent data: destination already
#     has different content for the same relative path -- migration
#     must leave the destination untouched AND retain the legacy copy
#     (never silently drop data it couldn't safely reconcile).
reset_fixture
OLD_DIR="$FIXTURE_ROOT/legacy2/recordings"
NEW_DIR="$FIXTURE_ROOT/persistent2/recordings"
mkdir -p "$OLD_DIR" "$NEW_DIR"
echo "legacy-content" > "$OLD_DIR/conflict.json"
echo "already-persistent-content" > "$NEW_DIR/conflict.json"
migrate_legacy_persistent_data "$OLD_DIR" "$NEW_DIR" "test data" >/dev/null 2>&1
assert_eq "existing destination content is never overwritten" "already-persistent-content" "$(cat "$NEW_DIR/conflict.json" 2>/dev/null)"
assert_eq "unresolved legacy file is retained, not deleted" "legacy-content" "$(cat "$OLD_DIR/conflict.json" 2>/dev/null)"

# 20. Repeated migration is idempotent: running it again after a
#     successful migration is a safe no-op (legacy dir already gone).
reset_fixture
OLD_DIR="$FIXTURE_ROOT/legacy3/recordings"
NEW_DIR="$FIXTURE_ROOT/persistent3/recordings"
mkdir -p "$OLD_DIR"
echo "data" > "$OLD_DIR/file.json"
migrate_legacy_persistent_data "$OLD_DIR" "$NEW_DIR" "test data" >/dev/null 2>&1
HASH_AFTER_FIRST="$(sha256sum "$NEW_DIR/file.json" | cut -d' ' -f1)"
assert_exit "migration runs a second time without error" 0 migrate_legacy_persistent_data "$OLD_DIR" "$NEW_DIR" "test data"
assert_eq "second run left the migrated file unchanged" "$HASH_AFTER_FIRST" "$(sha256sum "$NEW_DIR/file.json" 2>/dev/null | cut -d' ' -f1)"
assert_exit "legacy directory still absent after second run" 1 test -d "$OLD_DIR"

# 21. migrate_legacy_persistent_file: moves a single legacy config file
#     (the .env case) when the new location doesn't have one yet.
reset_fixture
OLD_FILE="$FIXTURE_ROOT/legacy4/.env"
NEW_FILE="$FIXTURE_ROOT/persistent4/vms.env"
mkdir -p "$FIXTURE_ROOT/legacy4"
echo "ANYAICAM_ENVIRONMENT=production" > "$OLD_FILE"
migrate_legacy_persistent_file "$OLD_FILE" "$NEW_FILE" "VMS env" >/dev/null 2>&1
assert_eq "legacy .env content moved to the new persistent location" "ANYAICAM_ENVIRONMENT=production" "$(cat "$NEW_FILE" 2>/dev/null)"
assert_exit "legacy .env removed after migration" 1 test -f "$OLD_FILE"

# 22. migrate_legacy_persistent_file never overwrites a differing
#     existing destination config.
reset_fixture
OLD_FILE="$FIXTURE_ROOT/legacy5/.env"
NEW_FILE="$FIXTURE_ROOT/persistent5/vms.env"
mkdir -p "$FIXTURE_ROOT/legacy5" "$FIXTURE_ROOT/persistent5"
echo "OLD_VALUE=1" > "$OLD_FILE"
echo "NEW_VALUE=2" > "$NEW_FILE"
migrate_legacy_persistent_file "$OLD_FILE" "$NEW_FILE" "VMS env" >/dev/null 2>&1
assert_eq "existing persistent config is never overwritten" "NEW_VALUE=2" "$(cat "$NEW_FILE" 2>/dev/null)"
assert_exit "conflicting legacy config is retained, not deleted" 0 test -f "$OLD_FILE"

echo
echo "== run_uninstall() (default, no --purge-all) =="

# 23-25. Regression coverage for the Phase 7 release blocker: default
#     uninstall must remove only replaceable software
#     ($VMS_INSTALL_ROOT, the VMS systemd unit, the agent venv/unit via
#     the wrapped script) while leaving every persistent path -- VMS
#     recordings, VMS data-config, and identity/config under
#     $CONFIG_DIR -- completely untouched. Before the persistent-layout
#     fix, recordings lived under $VMS_INSTALL_ROOT and were destroyed
#     by this same `rm -rf`.
reset_fixture
make_fake_agent_payload
mkdir -p "$VMS_INSTALL_ROOT/app"
echo 'print("fake app")' > "$VMS_INSTALL_ROOT/app/main.py"
mkdir -p "$VMS_RECORDINGS_DIR/camera1" "$VMS_DATA_CONFIG_DIR" "$CONFIG_DIR"
echo "real-recording-data" > "$VMS_RECORDINGS_DIR/camera1/clip1.mp4"
echo "real-data-config" > "$VMS_DATA_CONFIG_DIR/settings.json"
echo '{"appliance_id":"test-fixture-id"}' > "$IDENTITY_FILE"
RECORDINGS_HASH_BEFORE="$(sha256sum "$VMS_RECORDINGS_DIR/camera1/clip1.mp4" | cut -d' ' -f1)"
IDENTITY_HASH_BEFORE="$(sha256sum "$IDENTITY_FILE" | cut -d' ' -f1)"

assert_exit "run_uninstall (default) succeeds" 0 run_uninstall

assert_exit "replaceable VMS_INSTALL_ROOT software is removed" 1 test -d "$VMS_INSTALL_ROOT"
assert_exit "the wrapped appliance-agent uninstall script ran" 0 test -f "$FAKE_AGENT_UNINSTALL_MARKER"
assert_exit "VMS recordings directory still exists after default uninstall" 0 test -d "$VMS_RECORDINGS_DIR"
assert_eq "VMS recordings content is byte-identical after default uninstall" "$RECORDINGS_HASH_BEFORE" "$(sha256sum "$VMS_RECORDINGS_DIR/camera1/clip1.mp4" 2>/dev/null | cut -d' ' -f1)"
assert_exit "VMS data-config directory still exists after default uninstall" 0 test -d "$VMS_DATA_CONFIG_DIR"
assert_exit "appliance identity file still exists after default uninstall" 0 test -f "$IDENTITY_FILE"
assert_eq "appliance identity is byte-identical after default uninstall" "$IDENTITY_HASH_BEFORE" "$(sha256sum "$IDENTITY_FILE" 2>/dev/null | cut -d' ' -f1)"

# 26a. Concrete defect found live on Ryzen (2026-09-11): run_uninstall()
#      removed the VMS unit FILE but never its `.service.d/` drop-in
#      DIRECTORY -- a stale drop-in from any prior source (hand-created,
#      an old installer version, anything) survives uninstall untouched
#      and silently reattaches to the fresh unit the next install
#      writes under the same name. This crash-looped the agent unit
#      600+ times on real hardware before being noticed (see
#      appliance-agent/scripts/uninstall.sh's own fix and full incident
#      writeup); this covers the VMS unit against the identical defect
#      class, using the one occurrence of this bug this harness can
#      exercise behaviorally (VMS_SERVICE_FILE is fixture-redirected;
#      the agent's own inline-fallback equivalent uses a hardcoded
#      absolute /etc path and is covered by a structural check below,
#      same testing tradeoff already established for this script family
#      -- see appliance-agent/tests/test_uninstall_script_removes_drop_ins.py).
reset_fixture
make_fake_agent_payload
mkdir -p "$(dirname "$VMS_SERVICE_FILE")" "${VMS_SERVICE_FILE}.d"
echo "[Service]" > "${VMS_SERVICE_FILE}.d/stale-dropin.conf"
assert_exit "run_uninstall removes a stale VMS unit drop-in directory" 0 run_uninstall
assert_exit "VMS service drop-in directory is gone after uninstall" 1 test -d "${VMS_SERVICE_FILE}.d"

echo
echo "== run_uninstall() --purge-all =="

# 26. Concrete defect found while reviewing the full fresh-install/
#     uninstall/reinstall workflow ahead of Samsung deployment:
#     --purge-all removed every persistent directory but never the
#     anyaicam system user -- confirmed live that `id -u anyaicam`
#     still succeeds afterward, so detect_install_state() never reports
#     0/5 ("clean") again post-purge, and the very next install run
#     goes through the existing/repair path (a much looser storage-
#     preflight floor) instead of the strict 100GB clean-install check
#     that path is meant to enforce -- even though every other trace of
#     the appliance is genuinely gone. A true "start over from scratch"
#     reinstall needs the system user gone too, not just its data.
reset_fixture
make_fake_agent_payload
mkdir -p "$CONFIG_DIR" "$VMS_RECORDINGS_DIR"
assert_exit "run_uninstall --purge-all succeeds" 0 run_uninstall --purge-all
assert_eq "userdel anyaicam was called" "1" "$(grep -c '^anyaicam$' "$USERDEL_CALL_LOG" 2>/dev/null)"
assert_exit "CONFIG_DIR is removed by purge" 1 test -d "$CONFIG_DIR"

echo
echo "== disable_system_suspend() =="

# Confirmed live on Samsung: a genuinely fresh Ubuntu desktop install
# suspended itself mid-validation (LAN/SSH/Tailscale/VMS all dropped
# simultaneously) because of GNOME's own idle-suspend policy -- an
# appliance meant to run unattended 24/7 must never do this, regardless
# of which desktop-environment setting is responsible. disable_
# system_suspend() (08-systemd-setup.sh, sourced transitively via
# uninstall.sh's own `source install.sh` above) is the fix: masking
# these four systemd targets is confirmed live to make
# `systemctl suspend` itself fail ("Access denied") rather than
# actually sleeping, regardless of what tries to trigger it.
reset_fixture
disable_system_suspend >/dev/null 2>&1
assert_eq "masks exactly the four sleep/suspend/hibernate targets" \
    "mask sleep.target suspend.target hibernate.target hybrid-sleep.target" \
    "$(cat "$SYSTEMCTL_CALL_LOG" 2>/dev/null)"

reset_fixture
disable_system_suspend >/dev/null 2>&1
assert_exit "running it again (repair/reinstall) is still safe" 0 disable_system_suspend

echo
echo "== ready_endpoint_self_test_ok() (installer/validate.sh) =="

# Confirmed live on Ryzen (2026-09-11, golden-foundation-rc1 -> rc2): a
# clean, correctly-installed, unclaimed appliance with zero cameras --
# exactly the state every brand-new appliance is in right after
# install.sh, before claim/activation or camera discovery ever run --
# legitimately returns HTTP 503 from GET /ready (main.py's
# readiness_snapshot(): for RUNTIME_ROLE=edge, `ready` requires
# recording>0, structurally impossible before any camera exists). The
# OLD validate.sh check (`curl -fsS ... /ready`) used curl's -f flag,
# which treats ANY non-2xx status as failure, so validate.sh could
# never pass on a genuinely fresh install. ready_endpoint_self_test_ok()
# fixes this by checking main.py's own self_test.ok field directly
# (fetched without -f, so a 503 still yields its body) instead of the
# HTTP status code -- self_test.ok is what "the VMS started correctly"
# actually means; `ready` is deliberately a stricter, business-state
# check validate.sh was never meant to require.
#
# `curl` is shadowed the same way `id`/`docker`/`df` are shadowed above
# -- ready_endpoint_self_test_ok() is called directly (not through
# run_validate(), which would also require every other check's real
# system state), so only calls to the literal word `curl` inside that
# one function are exercised here.
curl() {
    printf '%s' "$CURL_READY_MOCK_BODY"
    return "${CURL_READY_MOCK_EXIT:-0}"
}

reset_fixture
# 1. The exact Ryzen/RC2 clean-install shape: self_test.ok=true, but
#    HTTP 503 because ready=false (zero cameras). Must PASS.
CURL_READY_MOCK_BODY='{"ready":false,"environment":"production","runtime_role":"edge","self_test":{"ok":true,"checks":[]},"cameras_online":0,"cameras_total":0,"recording_workers":0}'
CURL_READY_MOCK_EXIT=0
assert_exit "self_test.ok=true with HTTP 503 (fresh, zero-camera install) -> PASS" 0 ready_endpoint_self_test_ok

reset_fixture
# 2. The exact RC1 real defect shape: self_test.ok=false (critical
#    configuration_valid failure). Must still FAIL -- this fix must not
#    paper over an actually-broken VMS.
CURL_READY_MOCK_BODY='{"ready":false,"environment":"production","runtime_role":"edge","self_test":{"ok":false,"checks":[]},"cameras_online":0,"cameras_total":0,"recording_workers":0}'
CURL_READY_MOCK_EXIT=0
assert_exit "self_test.ok=false (RC1's real defect) -> FAIL" 1 ready_endpoint_self_test_ok

reset_fixture
# 3. A fully ready appliance (cameras already recording, HTTP 200) must
#    still PASS -- this fix only widens what's accepted, it never
#    narrows the previously-passing case.
CURL_READY_MOCK_BODY='{"ready":true,"environment":"production","runtime_role":"edge","self_test":{"ok":true,"checks":[]},"cameras_online":5,"cameras_total":5,"recording_workers":5}'
CURL_READY_MOCK_EXIT=0
assert_exit "self_test.ok=true with HTTP 200 (fully ready) -> PASS" 0 ready_endpoint_self_test_ok

reset_fixture
# 4. The VMS is genuinely unreachable (connection refused/timeout) --
#    curl itself fails and produces no body. Must still FAIL: this fix
#    must not turn "the app never started" into a false pass.
CURL_READY_MOCK_BODY=''
CURL_READY_MOCK_EXIT=7
assert_exit "curl connection failure (VMS unreachable) -> FAIL" 1 ready_endpoint_self_test_ok

reset_fixture
# 5. A malformed/unexpected body (e.g. an HTML error page from a crash
#    outside FastAPI's own handler) has no matching self_test.ok
#    substring. Must FAIL.
CURL_READY_MOCK_BODY='<html><body>502 Bad Gateway</body></html>'
CURL_READY_MOCK_EXIT=0
assert_exit "malformed/non-JSON response body -> FAIL" 1 ready_endpoint_self_test_ok

echo
echo "== validate.sh: anyaicam-agent.service must be enabled AND active =="

# Confirmed live on Ryzen (2026-09-11): validate.sh checked
# `anyaicam-agent.service is enabled` but never `is-active` -- a unit
# stuck crash-looping under `Restart=always` (see appliance-agent/
# scripts/uninstall.sh's own stale-drop-in incident writeup) IS enabled
# (systemd keeps retrying it forever, by design) but was never actually
# running, and validate.sh reported PASS the entire time regardless.
# Exercises the real check()/FAILURES machinery validate.sh itself
# uses (already sourced above), with `systemctl` locally overridden to
# answer is-enabled/is-active independently and controllably for
# anyaicam-agent.service specifically -- the global systemctl() shadow
# earlier in this harness always returns 0 for everything, which can't
# distinguish "enabled" from "active" the way this defect requires.
systemctl() {
    case "$*" in
        "is-enabled --quiet anyaicam-agent.service") return "${AGENT_ENABLED_MOCK_EXIT:-0}" ;;
        "is-active --quiet anyaicam-agent.service") return "${AGENT_ACTIVE_MOCK_EXIT:-0}" ;;
        *) return 0 ;;
    esac
}

FAILURES=0
AGENT_ENABLED_MOCK_EXIT=0
AGENT_ACTIVE_MOCK_EXIT=0
check "anyaicam-agent.service is enabled" systemctl is-enabled --quiet anyaicam-agent.service
check "anyaicam-agent.service is active" systemctl is-active --quiet anyaicam-agent.service
assert_eq "enabled + active -> both checks pass, zero failures" "0" "$FAILURES"

FAILURES=0
AGENT_ENABLED_MOCK_EXIT=0
AGENT_ACTIVE_MOCK_EXIT=1
check "anyaicam-agent.service is enabled" systemctl is-enabled --quiet anyaicam-agent.service
check "anyaicam-agent.service is active" systemctl is-active --quiet anyaicam-agent.service
assert_eq "enabled + inactive/crash-looping -> the new is-active check fails validation" "1" "$FAILURES"

FAILURES=0
AGENT_ENABLED_MOCK_EXIT=1
AGENT_ACTIVE_MOCK_EXIT=1
check "anyaicam-agent.service is enabled" systemctl is-enabled --quiet anyaicam-agent.service
check "anyaicam-agent.service is active" systemctl is-active --quiet anyaicam-agent.service
assert_eq "neither enabled nor active -> existing enabled check still catches it too (2 failures, not silently reduced to 1)" "2" "$FAILURES"

echo
echo "== uninstall.sh: agent inline-fallback drop-in removal (structural check) =="

# The agent's inline-fallback branch (installer/uninstall.sh's own
# `else` clause, used only when neither wrapped agent uninstall script
# exists) removes /etc/systemd/system/anyaicam-agent.service by a
# hardcoded absolute path, not a fixture-redirected variable -- the same
# testing constraint already documented in
# appliance-agent/tests/test_uninstall_script_removes_drop_ins.py for
# that script's own agent-unit removal. A structural source-text check
# instead of a behavioral run, matching that established precedent.
assert_exit "installer/uninstall.sh's agent fallback removes the .service.d drop-in directory" 0 \
    grep -q 'rm -rf /etc/systemd/system/anyaicam-agent.service.d' "$INSTALLER_DIR/uninstall.sh"

# Confirms run_validate() itself actually wires in the new is-active
# check proven correct in isolation above -- not just that the
# check()/systemctl mechanism CAN detect this, but that validate.sh's
# real check list actually calls it.
assert_exit "run_validate() itself calls the new anyaicam-agent.service is-active check" 0 \
    grep -q 'check "anyaicam-agent.service is active" systemctl is-active --quiet anyaicam-agent.service' "$INSTALLER_DIR/validate.sh"

echo
echo "== VMS install ownership (06-deploy-vms.sh) =="
# 2026-09-24, real defect confirmed live on Ryzen: `rsync -a` copied the
# payload's own owner (the login user who unpacked the release tarball)
# onto /opt/anyaicam, leaving the VMS source tree -- bind-mounted over the
# container's /app -- writable without sudo. find -exec runs the real
# chown binary, so it is shadowed with a logging stub on PATH, not a shell
# function; nothing is actually chowned.
reset_fixture
make_fake_vms_payload
mkdir -p "$MEDIAMTX_INSTALL_DIR"
printf 'real previously-installed mediamtx binary' > "$MEDIAMTX_BINARY_PATH"
mkdir -p "$VMS_INSTALL_ROOT/app"
printf 'left user-owned by an earlier install' > "$VMS_INSTALL_ROOT/app/unchanged_module.py"
cp "$VMS_INSTALL_ROOT/app/unchanged_module.py" "$VMS_PAYLOAD_DIR/app/unchanged_module.py"
CHOWN_STUB_DIR="$FIXTURE_ROOT/stub-bin"; CHOWN_LOG="$FIXTURE_ROOT/chown.log"
mkdir -p "$CHOWN_STUB_DIR"
printf '#!/usr/bin/env bash\nfor arg in "$@"; do printf "%%s\\n" "$arg"; done >> "%s"\n' "$CHOWN_LOG" > "$CHOWN_STUB_DIR/chown"
chmod +x "$CHOWN_STUB_DIR/chown"
PATH="$CHOWN_STUB_DIR:$PATH" deploy_vms repair >/dev/null 2>&1
assert_eq "the installed VMS tree is chowned to root:root" "1" "$(grep -cx 'root:root' "$CHOWN_LOG" 2>/dev/null | awk '{print ($1>0)}')"
assert_eq "the install root itself is included in the ownership repair" "1" "$(grep -cx "$VMS_INSTALL_ROOT" "$CHOWN_LOG" 2>/dev/null)"
assert_eq "an unchanged file an earlier install left user-owned is repaired too" "1" "$(grep -cx "$VMS_INSTALL_ROOT/app/unchanged_module.py" "$CHOWN_LOG" 2>/dev/null)"
assert_eq "the separately-installed MediaMTX binary is never re-owned" "0" "$(grep -c "$VMS_INSTALL_ROOT/mediamtx" "$CHOWN_LOG" 2>/dev/null)"
assert_exit "the mirror rsync no longer copies the payload's owner/group" 0 \
    grep -q 'rsync -a --no-owner --no-group --delete' "$INSTALLER_DIR/06-deploy-vms.sh"

echo
echo "== validate.sh startup wait (VMS still starting) =="
# 2026-09-24, confirmed live on Ryzen: install.sh restarts the VMS and
# validate.sh ran immediately, so /health, /ready and /version each got a
# single attempt while uvicorn was still starting and all three failed on
# a healthy install.
CURL_ATTEMPTS_FILE="$FIXTURE_ROOT/curl-attempts"
curl() {
    local attempts; attempts=$(( $(cat "$CURL_ATTEMPTS_FILE" 2>/dev/null || echo 0) + 1 ))
    echo "$attempts" > "$CURL_ATTEMPTS_FILE"
    (( attempts > CURL_FAILS_BEFORE_UP )) || return 7
    printf '%s' '{"ready":true,"self_test":{"ok":true,"checks":[]},"build_id":"'"$VMS_RELEASE_COMMIT"'"}'
}
reset_fixture
VMS_RELEASE_COMMIT="2222222222222222222222222222222222222b"
rm -f "$CURL_ATTEMPTS_FILE"; CURL_FAILS_BEFORE_UP=3
VMS_STARTUP_DEADLINE=""; VMS_STARTUP_WAIT_SECONDS=30; VMS_STARTUP_POLL_SECONDS=0
assert_exit "/health that answers after 3 refused connections passes (waits for startup)" 0 retry_until_vms_started vms_health_ok
assert_eq "it kept polling until the VMS answered" "4" "$(cat "$CURL_ATTEMPTS_FILE")"
rm -f "$CURL_ATTEMPTS_FILE"; CURL_FAILS_BEFORE_UP=2
VMS_STARTUP_DEADLINE=""
assert_exit "/ready self-test is also retried through startup" 0 retry_until_vms_started ready_endpoint_self_test_ok
rm -f "$CURL_ATTEMPTS_FILE"; CURL_FAILS_BEFORE_UP=2
VMS_STARTUP_DEADLINE=""
assert_exit "/version is also retried through startup" 0 retry_until_vms_started version_reports_release
rm -f "$CURL_ATTEMPTS_FILE"; CURL_FAILS_BEFORE_UP=1000000
VMS_STARTUP_DEADLINE=""; VMS_STARTUP_WAIT_SECONDS=1; VMS_STARTUP_POLL_SECONDS=0.2
started=$(date +%s)
assert_exit "a VMS that never comes up still FAILS once the startup deadline passes" 1 retry_until_vms_started vms_health_ok
assert_eq "the wait is bounded by VMS_STARTUP_WAIT_SECONDS" "1" "$(( $(date +%s) - started <= 5 ))"
unset -f curl
assert_exit "run_validate() wraps the /health check in the startup wait" 0 \
    grep -q 'check "VMS local health endpoint responds" retry_until_vms_started vms_health_ok' "$INSTALLER_DIR/validate.sh"
assert_exit "run_validate() wraps the /ready check in the startup wait" 0 \
    grep -q 'retry_until_vms_started ready_endpoint_self_test_ok' "$INSTALLER_DIR/validate.sh"
assert_exit "run_validate() wraps the /version check in the startup wait" 0 \
    grep -q 'check "VMS /version reports exact approved commit" retry_until_vms_started version_reports_release' "$INSTALLER_DIR/validate.sh"

echo
echo "== WebRTC media port restriction (runtime/anyaicam-webrtc-firewall) =="
# 2026-09-25: UDP 8189 is published for LAN WebRTC viewers. Docker-published
# ports bypass UFW, so the restriction lives in Docker's DOCKER-USER chain.
# A stub iptables records every call and remembers appended/inserted rules
# so -C (check) behaves like the real thing; nothing touches real netfilter.
FW="$INSTALLER_DIR/runtime/anyaicam-webrtc-firewall"
IPT_STATE="$FIXTURE_ROOT/ipt-rules"; IPT_STUB="$FIXTURE_ROOT/stub-iptables"
cat > "$IPT_STUB" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == "-w" ]] && shift
state="$IPT_STATE"; touch "$state"
op="$1"; shift
case "$op" in
  -N) grep -qx "chain $1" "$state" && exit 1; echo "chain $1" >> "$state" ;;
  -F) grep -v "^rule $1 " "$state" > "$state.tmp" || true; mv "$state.tmp" "$state" ;;
  -A|-I) chain="$1"; shift; [[ "$op" == "-I" && "$1" =~ ^[0-9]+$ ]] && shift; echo "rule $chain $*" >> "$state" ;;
  -C) chain="$1"; shift; grep -qxF "rule $chain $*" "$state" ;;
  *) exit 2 ;;
esac
STUB
chmod +x "$IPT_STUB"
export IPT_STATE
rm -f "$IPT_STATE"
assert_exit "check FAILS before any rule exists" 1 env ANYAICAM_IPTABLES="$IPT_STUB" bash "$FW" check
assert_exit "apply succeeds" 0 env ANYAICAM_IPTABLES="$IPT_STUB" bash "$FW" apply
assert_exit "apply is idempotent (second run)" 0 env ANYAICAM_IPTABLES="$IPT_STUB" bash "$FW" apply
assert_exit "check passes after apply" 0 env ANYAICAM_IPTABLES="$IPT_STUB" bash "$FW" check
assert_eq "the DOCKER-USER jump for udp/8189 exists exactly once" "1" "$(grep -c '^rule DOCKER-USER -p udp -m conntrack --ctorigdstport 8189 --ctdir ORIGINAL -j ANYAICAM-WEBRTC$' "$IPT_STATE")"
assert_eq "chain order: wg drop, 4 private/Tailscale allows, final drop"   "-i wg+ -j DROP|-s 10.0.0.0/8 -j RETURN|-s 172.16.0.0/12 -j RETURN|-s 192.168.0.0/16 -j RETURN|-s 100.64.0.0/10 -j RETURN|-j DROP"   "$(grep '^rule ANYAICAM-WEBRTC ' "$IPT_STATE" | sed 's/^rule ANYAICAM-WEBRTC //' | paste -sd'|')"
assert_eq "no public range is ever allowed" "0" "$(grep -c -- '-s 0.0.0.0/0' "$IPT_STATE")"
assert_exit "compose publishes the WebRTC port on IPv4, UDP only" 0 grep -q '^    - 0.0.0.0:8189:8189/udp$' "$INSTALLER_DIR/../docker-compose.yml"
assert_eq "compose publishes nothing else new (8000/tcp + 8189/udp only)" "2" "$(grep -cE '^    - (0\.0\.0\.0:)?[0-9]+:[0-9]+' "$INSTALLER_DIR/../docker-compose.yml")"
assert_exit "firewall unit runs Before=docker.service (no boot-time exposure window)" 0 grep -q '^Before=docker.service anyaicam-vms.service$' "$INSTALLER_DIR/runtime/anyaicam-webrtc-firewall.service"
assert_exit "VMS unit wants and orders after the firewall unit" 0 grep -q '^Wants=network-online.target anyaicam-webrtc-firewall.service$' "$INSTALLER_DIR/runtime/anyaicam-vms.service"
assert_exit "install.sh applies the restriction before (re)starting the VMS" 0   bash -c "grep -n 'install_webrtc_firewall\|^    systemd_setup' '$INSTALLER_DIR/install.sh' | tail -2 | head -1 | grep -q install_webrtc_firewall"
assert_exit "the installer refuses to continue if the restriction does not verify" 0 grep -q 'refusing to publish UDP 8189' "$INSTALLER_DIR/11-webrtc-firewall.sh"
assert_exit "validate.sh checks the restriction" 0 grep -q 'WebRTC media port (UDP 8189) is restricted to private/Tailscale sources' "$INSTALLER_DIR/validate.sh"
assert_exit "the release builder packages the firewall step" 0 grep -q '"11-webrtc-firewall.sh",' "$INSTALLER_DIR/build_release_installer.py"

echo
echo "== summary: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
