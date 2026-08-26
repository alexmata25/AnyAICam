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
    rm -rf "$FIXTURE_ROOT"
    mkdir -p "$FIXTURE_ROOT"
    # Redirect every path constant the two functions under test read or
    # write into the disposable fixture root.
    CONFIG_DIR="$FIXTURE_ROOT/etc/anyaicam"
    VMS_INSTALL_ROOT="$FIXTURE_ROOT/opt/anyaicam"
    VMS_SERVICE_FILE="$FIXTURE_ROOT/etc/systemd/system/anyaicam-vms.service"
    VERSION_MARKER="$CONFIG_DIR/installed_version"
    IDENTITY_FILE="$CONFIG_DIR/appliance_identity.json"
    mkdir -p "$(dirname "$VMS_SERVICE_FILE")"
    # id/docker mocks default to "absent" until a test overrides them.
    ID_MOCK_EXIT=1
    DOCKER_IMAGE_MOCK_EXIT=1
    DOCKER_COMPOSE_BUILD_MARKER="$FIXTURE_ROOT/.docker-compose-build-called"
    rm -f "$DOCKER_COMPOSE_BUILD_MARKER"
    DF_AVAIL_GB=999999
    DF_TOTAL_GB=999999
}

# A minimal fake source tree for deploy_vms() to sync from -- just
# enough for the regression test below, not a real app checkout.
make_fake_repo_root() {
    REPO_ROOT="$FIXTURE_ROOT/fake-repo"
    mkdir -p "$REPO_ROOT/app"
    echo 'print("fake app")' > "$REPO_ROOT/app/main.py"
    echo 'FROM python:3.12-slim' > "$REPO_ROOT/Dockerfile"
    echo 'FROM python:3.12-slim AS production' > "$REPO_ROOT/Dockerfile.production"
    echo 'services: {}' > "$REPO_ROOT/docker-compose.yml"
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
    command docker "$@"
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

# 14. Regression test for the clean-install release blocker found in
#     Phase 4 validation: docker-compose.yml's `build: .` resolves to a
#     file literally named Dockerfile, but the original deploy_vms()
#     only copied Dockerfile.production, leaving nothing for
#     `docker compose build` to read on a genuinely fresh install.
#     Both files must land in VMS_INSTALL_ROOT.
reset_fixture
make_fake_repo_root
deploy_vms clean >/dev/null 2>&1
assert_exit "plain Dockerfile is copied into VMS_INSTALL_ROOT" 0 test -f "$VMS_INSTALL_ROOT/Dockerfile"
assert_exit "Dockerfile.production is also copied into VMS_INSTALL_ROOT" 0 test -f "$VMS_INSTALL_ROOT/Dockerfile.production"
assert_exit "docker compose build was invoked" 0 test -f "$DOCKER_COMPOSE_BUILD_MARKER"

echo
echo "== summary: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
